from ev_journal.settings import Settings


def main() -> None:
    s = Settings.from_env()
    print(f"ev-journal: influx={s.influx_url} db={s.influx_db} data={s.data_dir}")


if __name__ == "__main__":
    main()
