"""Tests for the v1 data-folder layout."""
import pytest

from desk import config, store
from desk.errors import Refused


def test_the_data_folder_layout_is_one_place(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    assert config.db_path() == (tmp_path / "desk.db").resolve()


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
    assert config.icloud_placeholder(tmp_path / "desk.db") == tmp_path / ".desk.db.icloud"


def test_python_openers_refuse_a_desk_db_only_in_icloud():
    db = config.db_path()
    config.icloud_placeholder(db).write_bytes(b"an invented placeholder")
    for opener in (store.open, store.create):
        with pytest.raises(Refused, match="in iCloud but not downloaded"):
            opener()
    assert not db.exists()
