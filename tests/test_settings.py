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
