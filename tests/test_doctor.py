import duckdb
from click.testing import CliRunner

from lazarillo.cli import main
from lazarillo.doctor import doctor


def statuses(report):
    return {c.title: c.status for c in report.checks}


def test_healthy_duckdb(cfg):
    report = doctor(cfg.root / "lazarillo.yml")
    assert report.ok, report.to_markdown()
    s = statuses(report)
    assert s["Connection"] == s["Read-only"] == "ok"
    assert "`a` (1), `b` (1)" in report.to_markdown()
    assert s["dbt"] == "warn"  # no dbt section


def test_missing_warehouse_fails_with_the_fix(tmp_path):
    (tmp_path / "lazarillo.yml").write_text("warehouse:\n  path: nope.duckdb\n")
    report = doctor(tmp_path / "lazarillo.yml")
    assert not report.ok and "Point `warehouse.path`" in report.to_markdown()


def test_empty_production_schema_and_missing_manifest(tmp_path):
    duckdb.connect(str(tmp_path / "wh.duckdb")).execute("CREATE SCHEMA other; CREATE TABLE other.t (x int)").close()
    (tmp_path / "shop").mkdir()
    (tmp_path / "lazarillo.yml").write_text("warehouse:\n  path: wh.duckdb\ndbt:\n  project_dir: shop\n  prod_schema: marts\n")
    s = statuses(doctor(tmp_path / "lazarillo.yml"))
    assert s["dbt manifest"] == "fail" and s["Production schema"] == "warn"


def test_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(main, ["doctor"])
    assert out.exit_code == 1 and "lazarillo init" in out.output


def test_cli_and_mcp_print_the_same(cfg):
    import anyio
    from mcp import Client

    from lazarillo.mcp_server import build_server

    out = CliRunner().invoke(main, ["-c", str(cfg.root / "lazarillo.yml"), "doctor"])
    assert out.exit_code == 0 and "Ready for an agent." in out.output

    async def call() -> str:
        async with Client(build_server(cfg)) as client:
            return (await client.call_tool("doctor", {})).content[0].text

    assert anyio.run(call) == out.output.strip()
