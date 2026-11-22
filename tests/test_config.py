"""Tests for the v1 data-folder layout."""
import pytest

from desk import config, store
from desk.errors import Refused


def test_the_data_folder_layout_is_one_place(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    assert config.db_path() == (tmp_path / "desk.db").resolve()
    assert config.bars_path() == (tmp_path / "bars.db").resolve()


def test_an_env_var_moves_the_db(tmp_path, monkeypatch):
    """A one-off `TRADING_DESK_DB=/tmp/x trading-desk integrity` must not read the real store."""
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    monkeypatch.setenv("TRADING_DESK_DB", str(tmp_path / "override.db"))
    assert config.db_path() == (tmp_path / "override.db").resolve()


def test_a_config_toml_is_not_read(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path))
    (tmp_path / "config.toml").write_text('db = "elsewhere.db"\n')
    assert config.db_path() == tmp_path / "desk.db"
    assert not hasattr(config, "config")


def test_icloud_leaves_a_dot_icloud_placeholder_beside_an_evicted_file(tmp_path):
    assert config.icloud_placeholder(tmp_path / "bars.db") == tmp_path / ".bars.db.icloud"


def _legacy(tmp_path, monkeypatch, text='db = "elsewhere.db"\n'):
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path))
    (tmp_path / "config.toml").write_text(text)


def test_a_legacy_db_setting_is_refused_and_creates_nothing(tmp_path, monkeypatch, capsys):
    from desk import server
    _legacy(tmp_path, monkeypatch)
    server.first_start()
    assert not (tmp_path / "desk.db").exists()
    assert 'sets db = "elsewhere.db", which the desk no longer reads' in capsys.readouterr().err
    assert 'sets db = "elsewhere.db"' in server.call("list_strategies", {}).content[0].text


def test_open_refuses_a_legacy_db_setting_beside_a_desk_db(tmp_path, monkeypatch):
    from desk import server, store
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path))
    store.create(tmp_path / "desk.db")
    _legacy(tmp_path, monkeypatch)
    r = server.call("list_strategies", {})
    assert r.is_error and "then remove the db line from config.toml." in r.content[0].text


def test_integrity_refuses_a_legacy_db_setting(tmp_path, monkeypatch, capsys):
    from desk import cli
    _legacy(tmp_path, monkeypatch)
    assert cli.main(["integrity"]) == 1
    assert 'sets db = "elsewhere.db"' in capsys.readouterr().err


def test_a_config_toml_without_db_changes_nothing(tmp_path, monkeypatch):
    from desk import server
    _legacy(tmp_path, monkeypatch, 'name = "x"\n')
    server.first_start()
    assert (tmp_path / "desk.db").exists()


def test_the_env_db_skips_the_legacy_check(tmp_path, monkeypatch):
    _legacy(tmp_path, monkeypatch)
    monkeypatch.setenv(config.ENV_DB, str(tmp_path / "x.db"))
    config.refuse_legacy_db()


def test_python_openers_refuse_a_config_toml_that_sets_db(tmp_path, monkeypatch):
    _legacy(tmp_path, monkeypatch)
    for opener in (store.open, store.create):
        with pytest.raises(Refused, match='sets db = "elsewhere.db"'):
            opener()
    assert not (tmp_path / "desk.db").exists()
    store.create(tmp_path / "chosen.db")        # an explicit path is the caller's own choice


def test_python_openers_refuse_a_desk_db_only_in_icloud():
    db = config.db_path()
    config.icloud_placeholder(db).write_bytes(b"an invented placeholder")
    for opener in (store.open, store.create):
        with pytest.raises(Refused, match="in iCloud but not downloaded"):
            opener()
    assert not db.exists()
