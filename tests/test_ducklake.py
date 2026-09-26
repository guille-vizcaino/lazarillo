"""A local DuckLake attached next to the warehouse, behind the same guardrails."""

import duckdb
import pytest

from lazarillo.config import load_config
from lazarillo.diff import diff
from lazarillo.guardrails import GuardrailViolation
from lazarillo.warehouse import open_warehouse


def ducklake_available() -> bool:
    try:
        duckdb.connect().execute("LOAD ducklake")
        return True
    except duckdb.Error:
        return False


pytestmark = pytest.mark.skipif(not ducklake_available(), reason="DuckDB ducklake extension not installed")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    lake_dir = tmp_path / "lake"
    lake_dir.mkdir()
    # Create the lake from its own folder so the stored data path is relative, as users do.
    monkeypatch.chdir(lake_dir)
    con = duckdb.connect()
    con.execute("ATTACH 'ducklake:meta.ducklake' AS lk (DATA_PATH 'files/', DATA_INLINING_ROW_LIMIT 0)")
    con.execute("CREATE TABLE lk.orders AS SELECT range AS order_id, range * 2.5 AS amount FROM range(500)")
    con.execute("COPY (SELECT 'secret' AS s) TO '../private.csv'")
    con.close()
    monkeypatch.chdir(tmp_path.parent)

    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("CREATE SCHEMA analytics")
    con.execute("CREATE TABLE analytics.orders AS SELECT range AS order_id, range * 2.5 AS amount FROM range(501)")
    con.close()
    (tmp_path / "lazarillo.yml").write_text("warehouse: {path: wh.duckdb}\nattach:\n  lake: ducklake:lake/meta.ducklake\n")
    return load_config(tmp_path / "lazarillo.yml")


def test_query_and_diff_a_ducklake(cfg):
    with open_warehouse(cfg) as wh:
        assert wh.query("select sum(amount) from lake.orders").rows == [(311875.0,)]
        r = diff(wh, "analytics.orders", "lake.orders", key=["order_id"])
    assert (r.only_left, r.only_right, r.changed) == (1, 0, 0)


@pytest.mark.parametrize("sql", [
    "insert into lake.orders values (1000, 1)",
    "create table lake.x as select 1",
    "select * from read_csv('{root}/private.csv')",
    "select * from read_csv('/etc/hosts')",
])
def test_the_lake_stays_read_only_and_fenced(cfg, sql):
    with open_warehouse(cfg) as wh, pytest.raises((GuardrailViolation, duckdb.Error)):
        wh.query(sql.format(root=cfg.root))
