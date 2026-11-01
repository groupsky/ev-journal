"""Privacy guard: every latitude/longitude in the repo must lie in the test area.

The test area is within 1 degree of 0 N 0 E, which is open sea. Tests and
fixtures put their made-up places there. A real place anywhere else fails CI.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_AREA_DEG = 1.0

NUM = r"[-+]?\d{1,3}\.\d+"
# Two decimals with 3+ places side by side: "42.123, 23.456" or "(42.123 23.456)".
PAIR = re.compile(rf"(?<![\w.])({NUM})\s*[,;\s]\s*({NUM})(?![\w.])")
# A number after a lat/lon key: lat=42.1, "lon": 23.4, lng: 23.4
KEYED = re.compile(rf"\b(lat|latitude|lon|lng|longitude)\b[\"']?\s*[:=]\s*[\"']?({NUM})", re.I)


def _decimals(s: str) -> int:
    return len(s.split(".")[1])


def find_points(text: str) -> list[tuple[float, float]]:
    """Return the (lat, lon)-like numbers in the text that lie outside the test area."""
    bad = []
    for m in PAIR.finditer(text):
        a, b = m.group(1), m.group(2)
        if _decimals(a) < 3 or _decimals(b) < 3:
            continue
        lat, lon = float(a), float(b)
        if abs(lat) > 90 or abs(lon) > 180:
            continue
        if abs(lat) > TEST_AREA_DEG or abs(lon) > TEST_AREA_DEG:
            bad.append((lat, lon))
    for m in KEYED.finditer(text):
        v = float(m.group(2))
        if abs(v) > TEST_AREA_DEG and abs(v) <= 180:
            bad.append((v, v))
    return bad


def _repo_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / p for p in out.splitlines() if (ROOT / p).is_file()]


def test_detector_catches_points_outside_the_area():
    assert find_points("at = (10.5000, -30.2500)")
    assert find_points('{"lat": 0.0, "lon": -20.0}')
    assert not find_points("home = (0.1234, -0.5678)")
    assert not find_points('{"lat": 0.25, "lon": 0.75}')
    assert not find_points("python 3.12, ruff 0.6")


def test_no_real_coordinates_in_repo():
    found = {}
    for path in _repo_files():
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        pts = find_points(text)
        if pts:
            found[str(path.relative_to(ROOT))] = pts
    assert not found, f"coordinates outside the test area: {found}"
