"""A small InfluxDB 1.x client over plain HTTP."""

import base64
import json
import urllib.parse
import urllib.request


class Influx:
    def __init__(self, url: str, db: str, user: str | None, password: str | None):
        self.url, self.db = url.rstrip("/"), db
        self.auth = None
        if user:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            self.auth = f"Basic {token}"

    def _req(self, path: str, params: dict, body: bytes | None = None) -> bytes:
        req = urllib.request.Request(
            f"{self.url}{path}?{urllib.parse.urlencode(params)}", data=body
        )
        if self.auth:
            req.add_header("Authorization", self.auth)
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def query(self, q: str) -> list[dict]:
        """Run a SELECT and return its rows as dicts. Times are epoch ms."""
        data = json.loads(self._req("/query", {"db": self.db, "q": q, "epoch": "ms"}))
        rows = []
        for res in data.get("results", []):
            if "error" in res:
                raise RuntimeError(res["error"])
            if res.get("partial") or any(x.get("partial") for x in res.get("series", [])):
                raise RuntimeError("InfluxDB returned a partial result (row limit)")
            for s in res.get("series", []):
                tags = s.get("tags", {})
                for v in s["values"]:
                    rows.append({**tags, **dict(zip(s["columns"], v, strict=True))})
        return rows

    def execute(self, statements: list[str], batch: int = 100) -> None:
        """Run statements that change data (DELETE, DROP SERIES). Needs the write user."""
        for i in range(0, len(statements), batch):
            body = urllib.parse.urlencode({"q": "; ".join(statements[i : i + batch])}).encode()
            data = json.loads(self._req("/query", {"db": self.db}, body))
            for res in data.get("results", []):
                if "error" in res:
                    raise RuntimeError(res["error"])

    def write(self, lines: list[str], batch: int = 5000) -> None:
        for i in range(0, len(lines), batch):
            body = "\n".join(lines[i : i + batch]).encode()
            self._req("/write", {"db": self.db, "precision": "ms"}, body)


def _tag(v: str) -> str:
    return v.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _field(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(float(v))
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def line(measurement: str, tags: dict, fields: dict, t: int) -> str:
    """One line of line protocol. Fields that are None are left out. Numbers are floats."""
    tag_s = "".join(f",{_tag(k)}={_tag(str(v))}" for k, v in sorted(tags.items()) if v is not None)
    field_s = ",".join(f"{_tag(k)}={_field(v)}" for k, v in fields.items() if v is not None)
    return f"{_tag(measurement)}{tag_s} {field_s} {t}"
