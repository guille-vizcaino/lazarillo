"""End to end against S3 without an AWS account.

Every test runs against moto's S3 + Glue server. Set LAZARILLO_TEST_S3_ENDPOINT (plus
LAZARILLO_TEST_S3_KEY and LAZARILLO_TEST_S3_SECRET) to run them against a real
S3-compatible server such as MinIO too; Glue is then replaced by a SQL catalog whose
data lives in that server.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from click.testing import CliRunner

from lazarillo.cli import main
from lazarillo.config import load_config
from lazarillo.diff import diff
from lazarillo.guardrails import GuardrailViolation
from lazarillo.lake import LakeError
from lazarillo.warehouse import open_warehouse

REGION = "us-east-1"
ROWS = 120


@pytest.fixture(scope="module", params=["moto", pytest.param("minio", marks=pytest.mark.minio)])
def server(request):
    if request.param == "moto":
        from moto.server import ThreadedMotoServer

        srv = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        srv.start()
        host, port = srv.get_host_and_port()
        yield {"endpoint": f"http://{host}:{port}", "key": "testing", "secret": "testing", "glue": True}
        srv.stop()
    else:
        if not os.environ.get("LAZARILLO_TEST_S3_ENDPOINT"):
            pytest.skip("LAZARILLO_TEST_S3_ENDPOINT is not set")
        yield {"endpoint": os.environ["LAZARILLO_TEST_S3_ENDPOINT"], "glue": False,
               "key": os.environ["LAZARILLO_TEST_S3_KEY"], "secret": os.environ["LAZARILLO_TEST_S3_SECRET"]}


def orders(shift_hours: int = 0) -> pa.Table:
    start = datetime(2026, 9, 1, 20, 0)
    return pa.table({
        "order_id": pa.array(range(1, ROWS + 1), pa.int64()),
        "amount": pa.array([float(i % 7) * 10 for i in range(ROWS)]),
        # The Iceberg copy stores some timestamps two hours early, like demo problem 2.
        "ordered_at": pa.array([
            start + timedelta(minutes=i) - timedelta(hours=shift_hours if i % 3 == 0 else 0)
            for i in range(ROWS)
        ], pa.timestamp("us")),
    })


@pytest.fixture(scope="module")
def lake(server, tmp_path_factory):
    """A bucket with the same orders as Delta, partitioned Parquet and Iceberg, plus a warehouse."""
    import boto3
    import pyarrow.parquet as pq
    from deltalake import write_deltalake
    from pyarrow.fs import S3FileSystem

    root = tmp_path_factory.mktemp("s3lake")
    bucket = f"lake-{uuid.uuid4().hex[:8]}"
    ep, key, secret = server["endpoint"], server["key"], server["secret"]
    boto3.client("s3", endpoint_url=ep, region_name=REGION,
                 aws_access_key_id=key, aws_secret_access_key=secret).create_bucket(Bucket=bucket)

    data = orders()
    write_deltalake(f"s3://{bucket}/landing/delta/orders", data, storage_options={
        "AWS_ENDPOINT_URL": ep, "AWS_ALLOW_HTTP": "true", "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret,
    })
    fs = S3FileSystem(endpoint_override=ep.split("://")[1], scheme="http", region=REGION,
                      access_key=key, secret_key=secret)
    pq.write_table(data.slice(0, 60), f"{bucket}/landing/raw/day=1/part-0.parquet", filesystem=fs)
    pq.write_table(data.slice(60), f"{bucket}/landing/raw/day=2/part-0.parquet", filesystem=fs)
    # Outside the allowed prefix: the harness must refuse to read it.
    pq.write_table(data, f"{bucket}/finance/salaries.parquet", filesystem=fs)

    s3_props = {"s3.endpoint": ep, "s3.region": REGION, "s3.access-key-id": key, "s3.secret-access-key": secret}
    if server["glue"]:
        catalog_cfg = {"name": "glue", "type": "glue", "glue.endpoint": ep}
        from pyiceberg.catalog.glue import GlueCatalog

        catalog = GlueCatalog("glue", **s3_props, **{
            "glue.endpoint": ep, "glue.region": REGION,
            "glue.access-key-id": key, "glue.secret-access-key": secret,
        })
        catalog.create_namespace("landing", {"location": f"s3://{bucket}/landing/iceberg"})
    else:
        catalog_cfg = {"name": "local", "uri": "sqlite:///catalog.db", "warehouse": f"s3://{bucket}/landing/iceberg"}
        from pyiceberg.catalog.sql import SqlCatalog

        catalog = SqlCatalog("local", uri=f"sqlite:///{root}/catalog.db",
                             warehouse=f"s3://{bucket}/landing/iceberg", **s3_props)
        catalog.create_namespace("landing")
    catalog.create_table("landing.orders", schema=data.schema).append(orders(shift_hours=2))

    con = duckdb.connect(str(root / "warehouse.duckdb"))
    con.execute("CREATE SCHEMA landing")
    con.register("data", data)
    con.execute("CREATE TABLE landing.orders AS SELECT * FROM data")
    con.close()

    import yaml

    config = {
        "warehouse": {"path": "warehouse.duckdb"},
        "landing": {
            "locations": [f"s3://{bucket}/landing/"],
            "s3": {"endpoint": ep, "region": REGION, "access_key_id": key, "secret_access_key": secret},
            "iceberg_catalog": catalog_cfg,
        },
    }
    (root / "lazarillo.yml").write_text(yaml.safe_dump(config))
    return {"root": root, "bucket": bucket, "config": root / "lazarillo.yml", **server}


def test_delta_in_s3_matches_the_warehouse(lake):
    with open_warehouse(load_config(lake["config"])) as wh:
        r = diff(wh, f"delta:s3://{lake['bucket']}/landing/delta/orders", "landing.orders", key=["order_id"])
    assert r.identical, r.to_markdown()
    assert r.left_rows == ROWS


@pytest.mark.parametrize("pattern", ["landing/raw/**/*.parquet", "landing/raw/day=*/part-*.parquet", "landing/raw/"])
def test_parquet_in_s3_globs_and_prefixes(lake, pattern):
    with open_warehouse(load_config(lake["config"])) as wh:
        r = diff(wh, f"parquet:s3://{lake['bucket']}/{pattern}", "landing.orders", key=["order_id"])
    assert r.identical, r.to_markdown()


def test_iceberg_catalog_catches_the_timestamp_shift(lake):
    with open_warehouse(load_config(lake["config"])) as wh:
        r = diff(wh, f"delta:s3://{lake['bucket']}/landing/delta/orders", "iceberg:landing.orders",
                 key=["order_id"])
    assert (r.left_rows, r.right_rows, r.only_left, r.only_right) == (ROWS, ROWS, 0, 0)
    assert r.changed_by_column == {"ordered_at": ROWS // 3}


def test_locations_outside_the_allowlist_are_refused(lake):
    with open_warehouse(load_config(lake["config"])) as wh:
        for ref in (f"parquet:s3://{lake['bucket']}/finance/salaries.parquet",
                    f"parquet:s3://{lake['bucket']}/landing/../finance/salaries.parquet",
                    f"delta:s3://another-{lake['bucket']}/landing/delta/orders"):
            with pytest.raises(GuardrailViolation):
                wh.relation(ref)


def test_sql_cannot_read_the_bucket_directly(lake):
    with open_warehouse(load_config(lake["config"])) as wh, pytest.raises(GuardrailViolation):
        wh.query(f"select * from read_parquet('s3://{lake['bucket']}/finance/salaries.parquet')")


def test_missing_tables_give_a_readable_error(lake):
    with open_warehouse(load_config(lake["config"])) as wh:
        with pytest.raises(LakeError, match="Could not read"):
            wh.relation(f"delta:s3://{lake['bucket']}/landing/delta/nope")
        with pytest.raises(LakeError, match="Could not read"):
            wh.relation("iceberg:landing.nope")
        with pytest.raises(LakeError, match="Could not read"):
            wh.relation(f"parquet:s3://{lake['bucket']}/landing/raw/*.csv")


def test_credentials_from_the_environment(lake, monkeypatch, tmp_path):
    """No keys in lazarillo.yml: the AWS default chain supplies them."""
    import yaml

    raw = yaml.safe_load(lake["config"].read_text())
    for k in ("access_key_id", "secret_access_key"):
        raw["landing"]["s3"].pop(k)
    raw["warehouse"]["path"] = str(lake["root"] / "warehouse.duckdb")
    if raw["landing"]["iceberg_catalog"].get("uri"):
        raw["landing"]["iceberg_catalog"]["uri"] = f"sqlite:///{lake['root']}/catalog.db"
    (tmp_path / "lazarillo.yml").write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", lake["key"])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", lake["secret"])
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with open_warehouse(load_config(tmp_path / "lazarillo.yml")) as wh:
        r = diff(wh, f"delta:s3://{lake['bucket']}/landing/delta/orders", "iceberg:landing.orders",
                 key=["order_id"])
    assert r.changed == ROWS // 3


def test_wrong_credentials_are_reported(lake, tmp_path):
    if lake["glue"]:
        pytest.skip("moto accepts any credentials")
    text = lake["config"].read_text().replace(lake["secret"], "wrong-secret")
    (tmp_path / "lazarillo.yml").write_text(text.replace("warehouse.duckdb", str(lake["root"] / "warehouse.duckdb")))
    with open_warehouse(load_config(tmp_path / "lazarillo.yml")) as wh, pytest.raises(LakeError):
        wh.relation(f"delta:s3://{lake['bucket']}/landing/delta/orders")


def test_cli(lake):
    delta = f"delta:s3://{lake['bucket']}/landing/delta/orders"
    runner = CliRunner()
    out = runner.invoke(main, ["-c", str(lake["config"]), "diff", delta, "iceberg:landing.orders", "-k", "order_id"])
    assert out.exit_code == 0, out.output
    assert f"- `ordered_at`: {ROWS // 3} rows" in out.output
    refused = runner.invoke(main, ["-c", str(lake["config"]), "diff",
                                   f"parquet:s3://{lake['bucket']}/finance/salaries.parquet",
                                   "landing.orders", "-k", "order_id"])
    assert refused.exit_code == 1 and "Guardrail:" in refused.output and "Traceback" not in refused.output


def _call(server_or_params, tool: str, args: dict) -> str:
    import anyio
    from mcp import Client

    async def run() -> str:
        async with Client(server_or_params) as client:
            result = await client.call_tool(tool, args)
            return result.content[0].text

    return anyio.run(run)


def test_mcp_in_process(lake):
    from lazarillo.mcp_server import build_server

    server = build_server(load_config(lake["config"]))
    delta = f"delta:s3://{lake['bucket']}/landing/delta/orders"
    text = _call(server, "diff", {"left": delta, "right": "iceberg:landing.orders", "key": ["order_id"]})
    assert f"- `ordered_at`: {ROWS // 3} rows" in text
    text = _call(server, "diff", {"left": f"parquet:s3://{lake['bucket']}/finance/salaries.parquet",
                                  "right": "landing.orders", "key": ["order_id"]})
    assert text.startswith("Refused by guardrail")


def test_mcp_over_stdio(lake):
    """The same call through a real `lazarillo mcp` subprocess, as an agent would make it."""
    from mcp import StdioServerParameters

    params = StdioServerParameters(
        command=str(Path(sys.executable).parent / "lazarillo"),
        args=["-c", str(lake["config"]), "mcp"],
        env={**os.environ},
    )
    delta = f"delta:s3://{lake['bucket']}/landing/delta/orders"
    text = _call(params, "diff", {"left": delta, "right": "landing.orders", "key": ["order_id"]})
    assert "Identical: 120 rows" in text


def ducklake_available() -> bool:
    try:
        duckdb.connect().execute("LOAD ducklake; LOAD httpfs")
        return True
    except duckdb.Error:
        return False


@pytest.mark.skipif(not ducklake_available(), reason="DuckDB ducklake/httpfs extensions not installed")
def test_ducklake_with_files_in_s3(lake, tmp_path):
    from lazarillo.lake import S3Access

    s3 = S3Access(region=REGION, endpoint=lake["endpoint"],
                  access_key_id=lake["key"], secret_access_key=lake["secret"])
    con = duckdb.connect()
    con.execute(s3.duckdb_secret())
    con.execute(f"ATTACH 'ducklake:{tmp_path}/meta.ducklake' AS lk "
                f"(DATA_PATH 's3://{lake['bucket']}/landing/ducklake/', DATA_INLINING_ROW_LIMIT 0)")
    con.register("data", orders())
    con.execute("CREATE TABLE lk.orders AS SELECT * FROM data")
    con.close()

    text = lake["config"].read_text().replace("warehouse.duckdb", str(lake["root"] / "warehouse.duckdb"))
    text += f"attach:\n  lake: ducklake:{tmp_path}/meta.ducklake\n"
    (tmp_path / "lazarillo.yml").write_text(text.replace("sqlite:///catalog.db", f"sqlite:///{lake['root']}/catalog.db"))
    with open_warehouse(load_config(tmp_path / "lazarillo.yml")) as wh:
        assert wh.query("select count(*) from lake.orders").rows == [(ROWS,)]
        assert diff(wh, "lake.orders", "landing.orders", key=["order_id"]).identical
        # The lake's own folder is readable (DuckLake needs it); the rest of the bucket is not.
        assert wh.query(f"select count(*) from 's3://{lake['bucket']}/landing/ducklake/**/*.parquet'").rows == [(ROWS,)]
        with pytest.raises(GuardrailViolation):
            wh.query(f"select * from read_parquet('s3://{lake['bucket']}/finance/salaries.parquet')")
        with pytest.raises(GuardrailViolation):
            wh.query("create table lake.hack as select 1")
