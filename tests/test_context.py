from pathlib import Path


from lazarillo.context import DataMap

MANIFEST = {
    "sources": {"source.p.landing.orders": {"schema": "landing", "identifier": "orders"}},
    "nodes": {
        "model.p.stg": {"resource_type": "model", "name": "stg", "schema": "analytics",
                        "config": {"materialized": "view"},
                        "depends_on": {"nodes": ["source.p.landing.orders"]}},
        "model.p.fct": {"resource_type": "model", "name": "fct", "schema": "analytics",
                        "config": {"materialized": "incremental", "unique_key": "id"},
                        "depends_on": {"nodes": ["model.p.stg"]}},
        "test.p.unique": {"resource_type": "test", "name": "unique_fct_id",
                          "test_metadata": {"name": "unique", "kwargs": {"column_name": "id"}},
                          "depends_on": {"nodes": ["model.p.fct"]}},
    },
    "exposures": {"exposure.p.dash": {"name": "dash", "label": "Sales dashboard", "type": "dashboard",
                                      "owner": {"name": "BI"}, "depends_on": {"nodes": ["model.p.fct"]}}},
}


def test_downstream_reaches_exposures():
    dm = DataMap(MANIFEST)
    models, exposures = dm.downstream("stg")
    assert [m.name for m in models] == ["fct"]
    assert [e.name for e in exposures] == ["Sales dashboard"]


def test_describe_and_map():
    dm = DataMap(MANIFEST)
    assert "unique(id)" in dm.describe("fct")
    text = dm.to_markdown()
    assert "incremental (default, key=id)" in text and "landing.orders" in text


def test_profiles_dir_is_optional(tmp_path):
    from lazarillo.config import load_config

    (tmp_path / "lazarillo.yml").write_text(
        "warehouse: {path: wh.duckdb}\ndbt: {project_dir: proj}\n"
    )
    assert load_config(tmp_path / "lazarillo.yml").dbt.profiles_dir is None
    (tmp_path / "lazarillo.yml").write_text(
        "warehouse: {path: wh.duckdb}\ndbt: {project_dir: proj, profiles_dir: ~/.dbt}\n"
    )
    assert load_config(tmp_path / "lazarillo.yml").dbt.profiles_dir == Path.home() / ".dbt"
