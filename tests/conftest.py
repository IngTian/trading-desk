"""Every test runs against a throwaway data root, whatever the shell exports.

The events table is append-only, enforced by triggers, so a test event written into a real
desk.db can never be deleted. The test fixtures used to set only TRADING_DESK_HOME, and
TRADING_DESK_DB beats it: with that variable exported at the real store, one `pytest` run
appended dozens of made-up trades to the owner's permanent record (reproduced 11-11 against
a throwaway file: 19 failed, 110 passed, and 37 undeletable events written).

So before every test: both overrides are removed, and the root points at a fresh temp dir.
Tests that need their own root still set TRADING_DESK_HOME on top of this.

The local zone is pinned to UTC as well: a trade id carries the trader's local date, so ids
asserted in tests differed by host (two failed under TZ=Pacific/Auckland).
"""
from __future__ import annotations

import time

import pytest

from desk import config


@pytest.fixture
def anyio_backend():
    """anyio's pytest plugin, installed with mcp, runs the tests marked @pytest.mark.anyio."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_data_root(monkeypatch, tmp_path_factory):
    monkeypatch.delenv(config.ENV_DB, raising=False)
    # A directory of its own, not under tmp_path: some tests assert what tmp_path contains.
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path_factory.mktemp("isolated-home")))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()  # puts TZ back now, so tzset below restores the host's zone
    time.tzset()
