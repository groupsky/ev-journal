"""Privacy guard: every latitude/longitude in the repo must lie in the test area.

The test area is within 1 degree of 0 N 0 E, which is open sea. Tests and
fixtures put their made-up places there. A real place anywhere else fails CI.

A line that ends with "privacy: ok" is skipped. Use it only for numbers that
are not coordinates, and say so in the PR.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_AREA_DEG = 1.0
ESCAPE = "privacy: ok"

NUM = r"[-+]?\d{1,3}(?:\.\d+)?"
# Two numbers with 3+ decimals, split by a comma or a semicolon.
# (3, so that version lists like 3.12, 3.13 pass.)
PAIR = re.compile(r"(?<![\w.])([-+]?\d{1,3}\.\d{3,})\s*[,;]\s*([-+]?\d{1,3}\.\d{3,})(?![\w.])")
# A number after a lat/lon key, also as home_lat or startLng.
KEYED = re.compile(
    r"(?:\b|_|(?<=[a-z])(?=[A-Z]))(?i:lat|latitude|lon|lng|longitude)\b"
    rf"[\"']?\s*[:=]\s*[\"']?({NUM})"
)


def _outside(lat: float, lon: float) -> bool:
    return abs(lat) > TEST_AREA_DEG or abs(lon) > TEST_AREA_DEG


def find_points(text: str) -> list[str]:
    """Return the coordinates in the text that lie outside the test area."""
    bad = []
    for line in text.splitlines():
        if line.rstrip().endswith(ESCAPE):
            continue
        for m in PAIR.finditer(line):
            a, b = float(m.group(1)), float(m.group(2))
            # Either (lat, lon) or GeoJSON (lon, lat) may be a valid point.
            valid = (abs(a) <= 90 and abs(b) <= 180) or (abs(b) <= 90 and abs(a) <= 180)
            if valid and _outside(a, b):
                bad.append(m.group(0))
        for m in KEYED.finditer(line):
            v = float(m.group(1))
            if TEST_AREA_DEG < abs(v) <= 180:
                bad.append(m.group(0))
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
    caught = [
        "at = (10.5000, -30.2500)",  # privacy: ok
        '{"lat": 0.0, "lon": -20.0}',  # privacy: ok
        "[-140.255, 10.755]",  # privacy: ok
        "5.505,-20.505",  # privacy: ok
        '{"lat":10,"lon":-30}',  # privacy: ok
        "home_lat = 10.5",  # privacy: ok
        "startLng: -30.25",  # privacy: ok
    ]
    for s in caught:
        assert find_points(s), s
    ok = [
        "home = (0.1234, -0.5678)",
        '{"lat": 0.25, "lon": 0.75}',
        "[-0.5, 0.25]",
        "python 3.12, ruff 0.6",
        "3.14159 2.71828",
        "python: [3.12, 3.13]",
        "latency = 30",
        "semicolon: 42",
        "xlon = 30",
        "x = 10.5, -30.25  # privacy: ok",
    ]
    for s in ok:
        assert not find_points(s), s


def test_no_real_coordinates_in_repo():
    found = {}
    for path in _repo_files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        pts = find_points(text)
        if pts:
            found[str(path.relative_to(ROOT))] = pts
    assert not found, f"coordinates outside the test area: {found}"
