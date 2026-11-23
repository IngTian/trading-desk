"""Tests for the `trading-desk` commands, `integrity` and `mcp`. Figures are invented."""
from datetime import UTC, datetime

import pytest

from desk import api, cli, config, server, store

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
TEXT = "---\nname: Dip buyer\n---\n\n# Dip buyer\n\n```rules\ntimeframes = [\"1h\"]\n```\n"


def _store_with_a_trade():
    store.create(config.db_path())
    with store.open() as con:
        api.save_strategy(con, "dip-buyer", TEXT, now=NOW)
        api.start_run(con, "dip-buyer", mode="replay", currency="USD", at=NOW, now=NOW)
        api.trade(con, "dip-buyer-r1", ticker="ACME", side="long", entry_type="limit",
                  entry=100.0, stop=98.5, target=103.0, size=10,
                  instrument="stock", reasons="Dip into support.", at=NOW, now=NOW)


def _damage(n):
    """n trades with no entered event: raw SQL, as a damaged file holds them."""
    with store.open() as con:
        con.execute("PRAGMA foreign_keys = OFF")
        for i in range(n):
            con.execute(
                "INSERT INTO trades (id, run_id, ticker, side, instrument, point_value)"
                " VALUES (?, 'dip-buyer-r1', 'ZZZ', 'long', 'stock', 1)", (f"bad-{i}",))
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
    assert "trading-desk mcp" in err and "store.create" not in err


def test_help_lists_mcp(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "mcp" in capsys.readouterr().out


def test_mcp_returns_what_the_server_returns(monkeypatch):
    monkeypatch.setattr(server, "serve", lambda: 7)
    assert cli.main(["mcp"]) == 7

    def interrupted():
        raise KeyboardInterrupt
    monkeypatch.setattr(server, "serve", interrupted)
    assert cli.main(["mcp"]) == 0


def test_integrity_names_a_desk_db_that_is_only_in_icloud(capsys):
    (config.db_path().parent / ".desk.db.icloud").write_bytes(b"an invented placeholder")
    assert cli.main(["integrity"]) == 1
    err = capsys.readouterr().err
    assert "is in iCloud but not downloaded" in err and "first starts" not in err
    assert not config.db_path().exists()
