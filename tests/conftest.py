"""A test client that checks every response against docs/openapi.json."""

import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from ev_journal import places
from ev_journal.api import create_app

SPEC_FILE = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
SPEC = json.loads(SPEC_FILE.read_text())
REGISTRY = Registry().with_resource(
    "urn:openapi", Resource(contents=SPEC, specification=DRAFT202012)
)
TOKEN = "test-token"


def _escape(s: str) -> str:
    return s.replace("~", "~0").replace("/", "~1")


def _operation(method: str, path: str) -> tuple[str, dict]:
    for template, ops in SPEC["paths"].items():
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), path) and method in ops:
            # A literal path wins over a template (/places/suggestions vs /places/{id}).
            if "{" in template and path in SPEC["paths"]:
                continue
            return template, ops[method]
    raise AssertionError(f"{method.upper()} {path} is not in the OpenAPI file")


class CheckedClient(TestClient):
    def request(self, method, url, *args, **kwargs):
        resp = super().request(method, url, *args, **kwargs)
        path = resp.request.url.path
        template, op = _operation(method.lower(), path)
        status = str(resp.status_code)
        assert status in op["responses"], f"{method} {path}: status {status} not documented"
        content = op["responses"][status].get("content", {}).get("application/json")
        if content:
            pointer = "/".join(
                [
                    "#",
                    "paths",
                    _escape(template),
                    method.lower(),
                    "responses",
                    status,
                    "content",
                    _escape("application/json"),
                    "schema",
                ]
            )
            Draft202012Validator({"$ref": "urn:openapi" + pointer}, registry=REGISTRY).validate(
                resp.json()
            )
        return resp


@pytest.fixture
def data_dir(tmp_path):
    places.open_store(tmp_path).close()
    return tmp_path


@pytest.fixture
def stops():
    return []


@pytest.fixture
def journeys():
    return []


@pytest.fixture
def client(data_dir, stops, journeys):
    app = create_app(
        places.store_path(data_dir), TOKEN, ZoneInfo("UTC"), lambda: stops, lambda: journeys
    )
    c = CheckedClient(app)
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c
