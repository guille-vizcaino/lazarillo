import pytest

from lazarillo.config import load_config
from lazarillo.guardrails import GuardrailViolation, check_identifier, check_read_only
from lazarillo.warehouse import open_warehouse


@pytest.mark.parametrize("sql", [
    "delete from a.t",
    "drop table a.t",
    "select 1; drop table a.t",
    "copy a.t to 'out.csv'",
    "attach 'other.duckdb'",
])
def test_writes_are_refused(sql):
    with pytest.raises(GuardrailViolation):
        check_read_only(sql)


def test_select_passes():
    assert check_read_only("select 1;") == "select 1"


def test_identifiers_cannot_inject():
    assert check_identifier("analytics.fct_orders") == "analytics.fct_orders"
    with pytest.raises(GuardrailViolation):
        check_identifier("a.t; drop table a.t")


def test_query_is_capped_and_masked(cfg):
    with open_warehouse(cfg) as wh:
        result = wh.query("select * from a.t order by id")
    assert result.truncated and len(result.rows) == 2
    assert result.rows[0][2] == "•••"
    assert result.rows[1][2] is None  # nulls stay visible: "no email" is information


def test_filesystem_is_off_limits(cfg):
    with open_warehouse(cfg) as wh, pytest.raises(GuardrailViolation):
        wh.query("select * from read_csv('/etc/hosts')")


def test_missing_warehouse_is_explained(tmp_path):
    (tmp_path / "lazarillo.yml").write_text("warehouse:\n  path: nope.duckdb\n")
    with pytest.raises(FileNotFoundError, match="warehouse.path"):
        with open_warehouse(load_config(tmp_path / "lazarillo.yml")):
            pass
