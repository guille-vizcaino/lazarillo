import duckdb
import pytest

from lazarillo.diff import diff
from lazarillo.warehouse import open_warehouse


def test_diff_finds_everything(cfg):
    with open_warehouse(cfg) as wh:
        r = diff(wh, "a.t", "b.t", key=["id"])
    assert (r.left_rows, r.right_rows) == (3, 3)
    assert (r.only_left, r.only_right, r.changed) == (1, 1, 1)
    assert r.columns_only_left == ["email"] and r.columns_only_right == ["extra"]
    assert r.changed_by_column == {"val": 1}
    assert not r.identical
    assert "Schema drift" in r.to_markdown()


def test_identical(cfg):
    with open_warehouse(cfg) as wh:
        r = diff(wh, "a.t", "a.t", key=["id"])
    assert r.identical


def test_where_window(cfg):
    with open_warehouse(cfg) as wh:
        r = diff(wh, "a.t", "b.t", key=["id"], where="id = 1")
    assert (r.only_left, r.only_right, r.changed) == (0, 0, 0)


@pytest.fixture
def daily(cfg):
    """A delete+insert model by date: `date` is the unique_key, not the row key."""
    con = duckdb.connect(str(cfg.root / "wh.duckdb"))
    rows = "('2026-09-25', '/', 10), ('2026-09-25', '/labs', 4), ('2026-09-26', '/', 12), ('2026-09-26', '/labs', 5)"
    con.execute(f"CREATE TABLE a.daily AS SELECT * FROM (VALUES {rows}) v(date, path, pageloads)")
    con.execute(f"CREATE TABLE b.daily AS SELECT * FROM (VALUES {rows}) v(date, path, pageloads)")
    con.execute("CREATE TABLE b.daily_late AS SELECT * FROM b.daily")
    con.execute("UPDATE b.daily_late SET pageloads = 6 WHERE date = '2026-09-26' AND path = '/labs'")
    con.close()
    return cfg


def test_repeated_key_compares_whole_rows(daily):
    with open_warehouse(daily) as wh:
        r = diff(wh, "a.daily", "b.daily", key=["date"])
    assert not r.by_key and (r.repeated_keys_left, r.repeated_keys_right) == (2, 2)
    assert r.identical, "joining on a repeated key must not invent changes"
    assert "doesn't identify a row" in r.to_markdown()


def test_repeated_key_reports_differing_values(daily):
    with open_warehouse(daily) as wh:
        r = diff(wh, "a.daily", "b.daily_late", key=["date"])
    assert (r.only_left, r.only_right, r.changed, r.differing_keys) == (1, 1, 0, 1)
    assert r.samples["differing date"] == [{"date": "2026-09-26", "only_left": 1, "only_right": 1}]
    assert r.samples["only in right"] == [{"date": "2026-09-26", "path": "/labs", "pageloads": 6}]

    with open_warehouse(daily) as wh:
        r = diff(wh, "a.daily", "b.daily_late", key=["date", "path"])
    assert r.by_key and (r.only_left, r.only_right, r.changed) == (0, 0, 1)
    assert r.changed_by_column == {"pageloads": 1}


def test_repeated_rows_count_as_a_multiset(daily):
    con = duckdb.connect(str(daily.root / "wh.duckdb"))
    con.execute("CREATE TABLE b.daily_dup AS SELECT * FROM b.daily UNION ALL SELECT * FROM b.daily WHERE path = '/'")
    con.close()
    with open_warehouse(daily) as wh:
        r = diff(wh, "a.daily", "b.daily_dup", key=["date"])
    assert (r.only_left, r.only_right, r.differing_keys) == (0, 2, 2)
