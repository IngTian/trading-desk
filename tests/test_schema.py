"""book/schema.sql is now the source of truth, so it has to be tested like code.

The risk this file exists for is drift. Until 2026-09-07 the schema lived only in
the database, and the argument for that was sound: SQLite keeps DDL verbatim, so a
.sql file beside it was a second copy. Moving the data out made the file necessary
and re-introduced exactly that risk. These tests are the cost of the trade.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed  # noqa: E402

from desk import config, migrate  # noqa: E402


def objects(con):
    """Structural DDL per object. DELEGATES to migrate.fingerprint, deliberately.

    It was a copy of that function, and the copy went stale the moment the real one learned
    to strip comments: `--check` reported the book clean while this test reported DDL drift
    on the same two databases. Two definitions of "the same object" is one too many, and the
    tool's own is the one that must be tested.
    """
    return migrate.fingerprint(con)


def test_schema_sql_exists_and_is_substantial():
    p = config.schema_sql()
    assert p.exists(), f"no schema at {p} — the tool cannot create a book without it"
    text = p.read_text()
    assert "CREATE TABLE" in text and "CREATE TRIGGER" in text
    # The comments ARE the design record: every constraint carries the story of the
    # bug that produced it. A schema stripped of them would still work and would
    # have lost the only reason anyone can tell which walls are load-bearing.
    assert text.count("--") > 100, "the schema lost its comments"


def test_a_fresh_book_can_be_created_and_checks_out(tmp_path):
    path = tmp_path / "fresh.db"
    assert migrate.create(path) == 0
    assert migrate.check(path) == 0, "a book just built from schema.sql must match it"


def test_migrate_refuses_to_touch_an_existing_book(tmp_path, capsys):
    path = tmp_path / "fresh.db"
    assert migrate.create(path) == 0
    assert migrate.create(path) == 1, "must not overwrite a book that is already there"
    assert "already exists" in capsys.readouterr().out


def test_schema_version_constant_matches_what_a_fresh_book_records(tmp_path):
    path = tmp_path / "fresh.db"
    migrate.create(path)
    con = sqlite3.connect(path)
    got = con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    con.close()
    assert got == migrate.SCHEMA_VERSION


@pytest.mark.skipif(not os.path.exists(os.fspath(config.book_path())),
                    reason="no live book on this machine to compare against")
def test_the_live_book_matches_the_schema_in_the_repo():
    """THE drift test. The data and the code live apart now, so they can diverge.

    If this fails, one of the two moved: re-extract schema.sql if the book is right,
    migrate the book if the file is.
    """
    live = sqlite3.connect(f"{config.book_path().as_uri()}?mode=ro", uri=True)
    ref = sqlite3.connect(":memory:")
    ref.executescript(config.schema_sql().read_text())
    a, b = objects(live), objects(ref)
    live_version = live.execute(
        "SELECT MAX(version) FROM schema_version").fetchone()[0]
    live.close()
    assert sorted(set(a) - set(b)) == [], "in the book but not in schema.sql"
    assert sorted(set(b) - set(a)) == [], "in schema.sql but not in the book"
    assert [k for k in set(a) & set(b) if a[k] != b[k]] == [], "DDL differs"
    assert live_version == migrate.SCHEMA_VERSION, (
        f"the book says version {live_version}, migrate.py says "
        f"{migrate.SCHEMA_VERSION}")


def test_drift_means_structure_not_wording():
    """A REWORDED COMMENT IS NOT A MIGRATION, and a changed constraint still is.

    SQLite stores CREATE statements verbatim, so every comment in schema.sql is copied into
    the book -- which means a string compare cannot tell a reworded comment from a reworded
    CHECK. Rewording the comments on 2026-09-27 duly reported drift on 15 untouched objects.

    Both directions are asserted, because stripping comments is only safe if the structural
    half still fails: a check that cannot report a real change is worse than a noisy one.
    """
    base = "CREATE TABLE t (x INTEGER -- why x is an integer\n , y TEXT)"
    reworded = "CREATE TABLE t (x INTEGER -- a different sentence entirely\n , y TEXT)"
    blocky = "CREATE TABLE t (x INTEGER /* why x\n is an integer */, y TEXT)"
    changed = "CREATE TABLE t (x INTEGER, y REAL)"
    dropped = "CREATE TABLE t (x INTEGER)"

    assert migrate._structure(base) == migrate._structure(reworded), (
        "a reworded line comment must not read as drift")
    assert migrate._structure(base) == migrate._structure(blocky), (
        "a block comment must not read as drift either")
    for other, what in ((changed, "a column type change"), (dropped, "a dropped column")):
        assert migrate._structure(base) != migrate._structure(other), (
            f"{what} MUST read as drift")


def test_the_synthetic_fixture_satisfies_every_invariant(tmp_path):
    """If the fixture cannot pass check.py, it is not a book and proves nothing."""
    from desk import check as bookcheck
    con = seed.build(tmp_path / "hub.db")
    for name, sql, why in bookcheck.INVARIANTS:
        assert con.execute(sql).fetchall() == [], f"fixture violates {name}: {why}"
    con.close()


def test_the_fixture_residual_is_exactly_zero(tmp_path, capsys):
    """The fixture's numbers are chosen so the attribution closes to the cent.

    That makes the residual a real assertion about the arithmetic: any drift shows
    up as a nonzero number rather than as a slightly different wrong one.
    """
    from desk import check as bookcheck
    con = seed.build(tmp_path / "hub.db")
    bookcheck.attribution(con)
    out = capsys.readouterr().out
    con.close()
    gap = float(out.split("UNEXPLAINED")[1].split()[0].replace(",", ""))
    assert gap == pytest.approx(0.0, abs=0.005), out


def test_a_usd_deposit_does_not_fabricate_an_unexplained_figure(tmp_path, capsys):
    """attribution() summed flows.amount CURRENCY-BLIND into contributed capital.

    So a USD deposit was added to a CAD balance at par and the checker invented a residual of
    exactly the FX difference -- then printed no noise-floor caveat, because usd_exposure only
    counted money that went through to_cad and flows did not. It said "all N hold" and exited
    0. A withdrawal inverted it into the false "the legs claim more profit than the account
    holds" alarm.

    The fixture closes to the cent, so this adds a USD deposit AND the matching CAD balance:
    the truth stays zero, and the old code turned that into the size of the FX spread.

    Turns red if the flows SUM stops grouping by currency, or stops routing through to_cad.
    """
    from desk import check as bookcheck
    con = seed.build(tmp_path / "hub.db")
    usd = 10_000.0
    con.execute("INSERT INTO flows(on_date,amount,kind,account_id,currency) "
                "VALUES (?,?,'deposit','rrsp','USD')", (seed.AS_OF, usd))
    # The same money, in the statement the attribution is measured against.
    con.execute("UPDATE account_snapshots SET total = total + ? "
                "WHERE account_id='rrsp'", (usd * seed.FX_USDCAD,))

    bookcheck.attribution(con)
    out = capsys.readouterr().out
    con.close()
    gap = float(out.split("UNEXPLAINED")[1].split()[0].replace(",", ""))
    assert gap == pytest.approx(0.0, abs=0.005), (
        f"a USD deposit fabricated {gap:+,.2f} of unexplained P&L -- it was added to a CAD "
        f"balance at par, so the gap is {usd:,.0f} x (FX - 1)\n{out}")
    # AND THE NOISE FLOOR MUST KNOW THE SLEEVE IS THERE, or a real gap reads as significant.
    assert "of USDCAD on" in out, (
        "no noise-floor line: usd_exposure stayed 0, so the deposit never went through "
        "to_cad\n" + out)


# --------------------------------------------------------------------------- #
# where the book comes from
# --------------------------------------------------------------------------- #
def test_a_fresh_install_needs_no_configuration_at_all(tmp_path, monkeypatch):
    """THE POINT of the rewrite. No shell export, no file to create.

    The first attempt put a desk.toml in the REPO -- a file about where the data is,
    living in the tool, which is the category error this project spent a day removing
    from notes_ref and the vault path. Borrowed from daylogs instead: a real default,
    and configuration that lives WITH the data.
    """
    from desk import config
    monkeypatch.delenv(config.ENV_HOME, raising=False)
    monkeypatch.delenv(config.ENV_BOOK, raising=False)
    monkeypatch.setattr(config, "_config_cache", None)
    assert config.home() == Path.home() / "Documents" / "trading-desk"
    assert config.book_path() == (config.home() / "hub.db").resolve()
    assert "default" in config.book_source()


def test_the_resolution_order_is_explicit_then_env_then_config_then_default(
        tmp_path, monkeypatch):
    """$TRADING_DESK_BOOK must beat config.toml, or a one-off
    `TRADING_DESK_BOOK=/tmp/x desk positions` silently reads the configured book."""
    from desk import config
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path))
    monkeypatch.delenv(config.ENV_BOOK, raising=False)
    monkeypatch.setattr(config, "_config_cache", None)

    assert config.book_path() == (tmp_path / "hub.db").resolve(), "the default"

    (tmp_path / "config.toml").write_text('book = "/from/file.db"\n')
    monkeypatch.setattr(config, "_config_cache", None)
    assert config.book_path() == Path("/from/file.db")

    monkeypatch.setenv(config.ENV_BOOK, "/from/env.db")
    assert config.book_path() == Path("/from/env.db"), "the env var must win"
    assert config.book_path("/explicit.db") == Path("/explicit.db"), "explicit wins all"


def test_a_relative_book_in_config_is_relative_to_the_root_not_the_cwd(
        tmp_path, monkeypatch):
    """Otherwise one config means different books from different directories."""
    from desk import config
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path))
    monkeypatch.delenv(config.ENV_BOOK, raising=False)
    (tmp_path / "config.toml").write_text('book = "nested/other.db"\n')
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.chdir(tmp_path.parent)
    assert config.book_path() == (tmp_path / "nested" / "other.db").resolve()


def test_a_malformed_config_is_an_error_not_a_shrug(tmp_path, monkeypatch):
    """Swallowing a syntax error sends someone hunting for a missing book with the
    answer being an unclosed quote in a file they forgot they wrote."""
    from desk import config
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path))
    monkeypatch.delenv(config.ENV_BOOK, raising=False)
    (tmp_path / "config.toml").write_text('book = "unclosed\n')
    monkeypatch.setattr(config, "_config_cache", None)
    with pytest.raises(RuntimeError, match="not valid TOML"):
        config.book_path()


def test_there_is_no_in_repo_fallback_left(tmp_path, monkeypatch):
    """It existed while the data was being moved out and would now be a trap:
    silently preferring a stale book in a checkout over the real one."""
    from desk import config
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path))
    monkeypatch.delenv(config.ENV_BOOK, raising=False)
    monkeypatch.setattr(config, "_config_cache", None)
    assert config.REPO not in config.book_path().parents
    assert not hasattr(config, "_LEGACY_BOOK")


def test_nothing_in_the_repo_configures_where_the_data_is():
    """A config file about the data, living in the tool, is the error this replaced."""
    assert not (config.REPO / "desk.toml").exists()
    assert not (config.REPO / "desk.toml.example").exists()


def test_the_cli_entry_point_actually_imports():
    """87 tests once passed with a SYNTAX ERROR in desk/__main__.py.

    Nothing imported it: the suite reaches desk.book, desk.parse, desk.config and
    desk.tui.*, and never the entry point -- the one module a user hits first. An
    import is the cheapest possible assertion that `desk` runs at all.
    """
    import importlib
    mod = importlib.import_module("desk.__main__")
    assert callable(mod.main)


def test_desk_takes_no_arguments_and_says_where_each_one_went(tmp_path, monkeypatch,
                                                              capsys):
    """THERE ARE NO SUBCOMMANDS. The author: "i would only evern type desk and settle things
    inside the tui."

    Seven of them existed and each is now a key. A tool that answers a removed subcommand
    with "unrecognised arguments" teaches nothing -- muscle memory outlives a release --
    so every one names where it went, and this pins that so a rename cannot silently make
    the guidance false.
    """
    import importlib
    mod = importlib.import_module("desk.__main__")
    book_path = tmp_path / "hub.db"
    migrate.create(book_path)
    monkeypatch.setenv(config.ENV_BOOK, str(book_path))
    monkeypatch.setattr(config, "_config_cache", {})

    for gone in ("positions", "balance", "marks", "where", "account", "instrument",
                 "asof"):
        assert mod.main([gone]) == 2, f"desk {gone} should be refused, not run"
        out = capsys.readouterr().err
        assert "takes no arguments" in out, out
        assert gone in out, f"it must name the subcommand it is answering about: {out}"
        # And it must say what to do instead, not merely that the thing is gone.
        assert any(w in out for w in ("tab", "dashboard", "positions", "m", "?", "a", "i",
                                      "k")), out


def test_a_refused_invocation_still_names_the_book(tmp_path, monkeypatch, capsys):
    """The one thing `desk where` was for: which book, when something looks wrong."""
    import importlib
    mod = importlib.import_module("desk.__main__")
    book_path = tmp_path / "hub.db"
    migrate.create(book_path)
    monkeypatch.setenv(config.ENV_BOOK, str(book_path))
    monkeypatch.setattr(config, "_config_cache", {})
    assert mod.main(["positions"]) == 2
    assert str(book_path) in capsys.readouterr().err


def test_the_help_overlay_says_which_book_and_why_that_one(tmp_path, monkeypatch):
    """`desk where` moved into `?`. Four things can decide the path, and when a figure
    looks wrong the first question is which book produced it."""
    import importlib
    book_path = tmp_path / "hub.db"
    migrate.create(book_path)
    monkeypatch.setenv(config.ENV_BOOK, str(book_path))
    monkeypatch.setattr(config, "_config_cache", {})
    app_mod = importlib.import_module("desk.tui.app")
    out = app_mod._book_lines()
    assert str(book_path) in out
    assert config.ENV_BOOK in out, "it must say WHY this book, not only which"
    assert "bytes" in out, "and whether it is actually there"
