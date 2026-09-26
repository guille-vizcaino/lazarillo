import pytest

from lazarillo.config import load_config
from lazarillo.init import init_project


def test_init_finds_dbt_project_and_writes_a_loadable_config(tmp_path):
    (tmp_path / "transform" / "shop").mkdir(parents=True)
    (tmp_path / "transform" / "shop" / "dbt_project.yml").write_text("name: shop\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "dbt_project.yml").write_text("name: ignored\n")

    out = init_project(tmp_path, "data/wh.duckdb", None, force=False)

    cfg = load_config(tmp_path / "lazarillo.yml")
    assert cfg.warehouse == tmp_path / "data" / "wh.duckdb"
    assert cfg.dbt.project_dir == tmp_path / "transform" / "shop"
    assert "dbt project: `transform/shop`" in out and "(not created yet)" in out
    assert '"mcp"' in out


def test_init_without_dbt_leaves_it_commented(tmp_path):
    init_project(tmp_path, "warehouse.duckdb", None, force=False)
    assert load_config(tmp_path / "lazarillo.yml").dbt is None


def test_init_does_not_overwrite_without_force(tmp_path):
    init_project(tmp_path, "a.duckdb", None, force=False)
    with pytest.raises(FileExistsError):
        init_project(tmp_path, "b.duckdb", None, force=False)
    init_project(tmp_path, "b.duckdb", None, force=True)
    assert load_config(tmp_path / "lazarillo.yml").warehouse.name == "b.duckdb"
