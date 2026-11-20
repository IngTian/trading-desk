"""Tests for the v1 data-folder layout."""
from pathlib import Path

from desk import config


def test_the_data_folder_layout_is_one_place(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    assert config.db_path() == (tmp_path / "desk.db").resolve()
    assert config.bars_dir() == (tmp_path / "bars").resolve()
    assert config.reports_dir() == (tmp_path / "reports").resolve()
    assert config.imports_dir() == (tmp_path / "imports").resolve()
    assert config.legacy_book() == (tmp_path / "hub.db").resolve()


def test_the_vault_path_is_optional_and_comes_from_config_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    assert config.vault_path() is None
    (tmp_path / "config.toml").write_text('vault = "/some/vault"\n')
    config._config_cache = None  # clear cache after writing config.toml
    assert config.vault_path() == Path("/some/vault")


def test_an_env_var_beats_config_toml_for_the_db(tmp_path, monkeypatch):
    """A one-off `TRADING_DESK_DB=/tmp/x trading-desk integrity` must not read the real store."""
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('db = "configured.db"\n')
    config._config_cache = None  # clear cache after writing config.toml
    # Without the env var, config.toml is used
    assert config.db_path() == (tmp_path / "configured.db").resolve()
    # With the env var, it overrides config.toml
    monkeypatch.setenv("TRADING_DESK_DB", str(tmp_path / "override.db"))
    assert config.db_path() == (tmp_path / "override.db").resolve()


def test_legacy_book_honours_trading_desk_book_env_var(tmp_path, monkeypatch):
    """legacy_book() must respect TRADING_DESK_BOOK like the old book_path() does."""
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    # Default: hub.db under home
    assert config.legacy_book() == (tmp_path / "hub.db").resolve()
    # With env var: the specified path
    custom_book = tmp_path / "custom.db"
    monkeypatch.setenv("TRADING_DESK_BOOK", str(custom_book))
    assert config.legacy_book() == custom_book.resolve()


def test_vault_path_resolves_relative_paths_against_home(tmp_path, monkeypatch):
    """A relative vault path in config.toml must resolve against home(), not cwd."""
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    # Absolute path: returned as-is
    (tmp_path / "config.toml").write_text('vault = "/absolute/vault"\n')
    config._config_cache = None
    assert config.vault_path() == Path("/absolute/vault").resolve()
    # Relative path: resolved against home(), not cwd
    (tmp_path / "config.toml").write_text('vault = "my-vault"\n')
    config._config_cache = None
    assert config.vault_path() == (tmp_path / "my-vault").resolve()
