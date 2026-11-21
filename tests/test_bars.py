"""bars.db: completed price bars, insert-only with the first write winning, and the spans each
fetch covered, so a closed market is told apart from missing history. Every figure is invented."""
import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from desk import bars, judge, store

B = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)   # bar n opens at B + n hours; 1h bars


def h(n):
    return B + timedelta(hours=n)


AT = h(24)   # the write time


def b(n, o=100.0, hi=101.0, lo=99.0, c=100.5):
    return judge.Bar(h(n), o, hi, lo, c, None)


def md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


@pytest.fixture
def con(tmp_path):
    with closing(bars.open(tmp_path / "bars.db")) as c:
        yield c


def test_bars_db_is_made_on_first_use_with_a_rollback_journal(tmp_path):
    with closing(bars.open(tmp_path / "bars.db")) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("PRAGMA user_version").fetchone()[0] == bars.VERSION
    with closing(bars.open(tmp_path / "bars.db")):
        pass


def test_a_desk_db_at_the_bars_path_is_refused_untouched(tmp_path):
    p = tmp_path / "bars.db"
    store.create(p)
    before = md5(p)
    # F2: the path holds "bars.db" anyway, so the match names the refusal itself
    with pytest.raises(store.NotADeskDatabase, match="is not a bars.db"):
        bars.open(p)
    assert md5(p) == before


def test_the_first_write_wins_and_a_repeat_is_ignored(con):
    assert bars.add(con, "yahoo", "ACME", 60, [b(1)], h(1), h(2), AT) == 1
    assert bars.add(con, "yahoo", "ACME", 60, [b(1, c=100.75)], h(1), h(2), AT) == 0
    assert bars.read(con, "yahoo", "ACME", 60, h(0)) == [b(1)]


def test_bars_and_spans_refuse_an_update_and_a_delete(con):
    bars.add(con, "yahoo", "ACME", 60, [b(1)], h(1), h(2), AT)
    for sql in ("UPDATE bars SET volume = 1", "DELETE FROM bars",   # updates no CHECK refuses
                "UPDATE spans SET recorded_at = 'x'", "DELETE FROM spans"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(sql)


def test_an_inconsistent_bar_raises_and_nothing_of_the_call_is_stored(con):
    with pytest.raises(sqlite3.IntegrityError):   # b(2)'s high is below its open
        bars.add(con, "yahoo", "ACME", 60, [b(1), b(2, hi=99.5)], h(1), h(3), AT)
    assert bars.read(con, "yahoo", "ACME", 60, h(0)) == []
    assert bars.covered(con, "yahoo", "ACME", 60) == []


def test_read_keeps_to_one_source_symbol_and_timeframe_within_its_range(con):
    bars.add(con, "yahoo", "ACME", 60, [b(1), b(2)], h(1), h(3), AT)
    bars.add(con, "other", "ACME", 60, [b(2, c=100.75), b(3)], h(2), h(4), AT)
    assert bars.read(con, "yahoo", "ACME", 60, h(0)) == [b(1), b(2)]
    assert bars.read(con, "yahoo", "ACME", 60, h(2)) == [b(2)]
    assert bars.read(con, "yahoo", "ACME", 60, h(0), h(2)) == [b(1)]
    assert bars.read(con, "yahoo", "WXYZ", 60, h(0)) == []
    assert bars.read(con, "yahoo", "ACME", 15, h(0)) == []


def test_covered_merges_touching_and_overlapping_spans(con):
    bars.add(con, "yahoo", "ACME", 60, [b(1)], h(1), h(2), AT)
    bars.add(con, "yahoo", "ACME", 60, [b(2)], h(2), h(3), AT)
    bars.add(con, "yahoo", "ACME", 60, [], h(1.5), h(2.5), AT)
    bars.add(con, "yahoo", "ACME", 60, [b(5)], h(5), h(6), AT)
    assert bars.covered(con, "yahoo", "ACME", 60) == [(h(1), h(3)), (h(5), h(6))]
    bars.add(con, "yahoo", "ACME", 60, [], h(3), h(5), AT)   # F6: an empty add writes its span
    assert bars.covered(con, "yahoo", "ACME", 60) == [(h(1), h(6))]
    assert bars.covered(con, "other", "ACME", 60) == []
