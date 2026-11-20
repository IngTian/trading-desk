"""Tests for the `trading-desk integrity` command. Figures are invented."""
from datetime import UTC, datetime

from desk import api, cli, config, store

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
TEXT = "---\nname: Dip buyer\n---\n\n# Dip buyer\n\n```rules\ntimeframes: 1h\n```\n"


def _store_with_a_trade():
    store.create(config.db_path())
    with store.open() as con:
        api.save_strategy(con, "dip-buyer", TEXT, now=NOW)
        api.start_run(con, "dip-buyer", mode="replay", at=NOW, now=NOW)
        api.trade(con, "dip-buyer-r1", ticker="ACME", side="long", entry_type="limit",
                  entry=100.0, stop=98.5, target=103.0, size=10, currency="USD",
                  reasons="Dip into support.", at=NOW, now=NOW)


def _damage(n):
    """n trades with no entered event: raw SQL, as a damaged file holds them."""
    with store.open() as con:
        con.execute("PRAGMA foreign_keys = OFF")
        for i in range(n):
            con.execute(
                "INSERT INTO trades (id, run_id, ticker, side, instrument, point_value, currency)"
                " VALUES (?, 'dip-buyer-r1', 'ZZZ', 'long', 'stock', 1, 'USD')", (f"bad-{i}",))
        con.commit()


def test_a_healthy_store_exits_0(capsys):
    _store_with_a_trade()
    assert cli.main(["integrity"]) == 0
    assert "0 integrity violation(s)" in capsys.readouterr().out


def test_a_damaged_store_exits_1_whatever_the_count(capsys):
    _store_with_a_trade()
    _damage(256)  # a raw count of 256 would wrap to exit status 0
    assert cli.main(["integrity"]) == 1
    assert "256 integrity violation(s)" in capsys.readouterr().out


def test_no_store_is_a_message_and_a_non_zero_exit(capsys):
    assert cli.main(["integrity"]) != 0
    err = capsys.readouterr().err
    assert "desk.db" in err and "Traceback" not in err
