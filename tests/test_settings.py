from datetime import date

import pytest

from ev_journal.settings import Settings


def test_defaults_without_env():
    s = Settings.from_env({})
    assert s.influx_db == "homy"
    assert s.influx_read_password is None
    assert s.api_token is None


def test_passwords_come_from_files(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("secret\n")
    tok = tmp_path / "tok"
    tok.write_text("t0ken")
    s = Settings.from_env(
        {
            "EVJ_INFLUX_READ_USER": "reader",
            "EVJ_INFLUX_READ_PASSWORD_FILE": str(pw),
            "EVJ_API_TOKEN_FILE": str(tok),
            "EVJ_DATA_DIR": str(tmp_path),
        }
    )
    assert s.influx_read_user == "reader"
    assert s.influx_read_password == "secret"
    assert s.api_token == "t0ken"
    assert s.data_dir == tmp_path


def test_departures_and_day_types():
    s = Settings.from_env(
        {
            "EVJ_DEPARTURE": "05:00",
            "EVJ_DEPARTURE_ALT": "05:40",
            "EVJ_DEPARTURE_WEEKEND": "none",
            "EVJ_DAY_TYPES": "alt:2026-01-05..2026-01-09, alt:2026-02-02..2026-02-06",
            "EVJ_TIMER_STEP_MIN": "10",
        }
    )
    assert s.departure == "05:00"
    assert s.departures == {"alt": "05:40", "weekend": None}
    assert s.day_ranges == (
        ("alt", date(2026, 1, 5), date(2026, 1, 9)),
        ("alt", date(2026, 2, 2), date(2026, 2, 6)),
    )
    assert s.timer_step_min == 10
    assert Settings.from_env({}).timer_step_min == 15
    with pytest.raises(ValueError):
        Settings.from_env({"EVJ_DAY_TYPES": "alt:2026-01-09..2026-01-05"})
    with pytest.raises(ValueError):
        Settings.from_env({"EVJ_DEPARTURE_ALT": "soon"})
