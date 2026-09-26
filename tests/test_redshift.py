"""The Redshift warehouse, without a Redshift cluster.

Unit tests cover config, IAM credentials (stubbed AWS API) and the guardrails that run
before any SQL leaves the machine. The `postgres` tests run the real code path against
Postgres, which speaks the same protocol, when LAZARILLO_TEST_PG_DSN is set:

    docker run -d -p 5439:5432 -e POSTGRES_USER=lazarillo -e POSTGRES_PASSWORD=lazarillo \\
        -e POSTGRES_DB=dev postgres:16-alpine
    LAZARILLO_TEST_PG_DSN=postgresql://lazarillo:lazarillo@127.0.0.1:5439/dev pytest -m postgres
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from lazarillo.cli import main
from lazarillo.config import RedshiftConfig, load_config
from lazarillo.diff import diff
from lazarillo.engine import Engine, WarehouseError
from lazarillo.guardrails import GuardrailViolation
from lazarillo.redshift import credentials, region_from_host
from lazarillo.warehouse import Warehouse, open_warehouse

HOST = "my-cluster.abc123.eu-west-1.redshift.amazonaws.com"


def write_config(root: Path, body: str):
    (root / "lazarillo.yml").write_text(body)
    return load_config(root / "lazarillo.yml")


def test_config(tmp_path):
    cfg = write_config(tmp_path, f"warehouse:\n  type: redshift\n  host: {HOST}\n  database: analytics\n")
    assert cfg.warehouse is None
    assert cfg.redshift == RedshiftConfig(host=HOST, database="analytics")
    assert cfg.redshift.sslmode == "require" and cfg.redshift.port == 5439


def test_config_rejects_iam_without_target(tmp_path):
    with pytest.raises(ValueError, match="cluster_identifier"):
        write_config(tmp_path, f"warehouse:\n  type: redshift\n  host: {HOST}\n  database: a\n  iam: true\n")


def test_config_rejects_unknown_warehouse(tmp_path):
    with pytest.raises(ValueError, match="snowflake"):
        write_config(tmp_path, "warehouse:\n  type: snowflake\n")


@pytest.mark.parametrize("host, region", [
    (HOST, "eu-west-1"),
    ("wg.123456789012.us-east-2.redshift-serverless.amazonaws.com", "us-east-2"),
    ("localhost", None),
])
def test_region_from_host(host, region):
    assert region_from_host(host) == region


@pytest.fixture
def aws(monkeypatch):
    """Stub the Redshift APIs and record what was asked."""
    import boto3
    from botocore.stub import Stubber

    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    stubs, real_client = [], boto3.Session.client

    def client(self, name, *a, **kw):
        c = real_client(self, name, *a, **kw)
        stub = Stubber(c)
        for method, params, response in expected.get(name, []):
            if isinstance(response, str):
                stub.add_client_error(method, response, expected_params=params)
            else:
                stub.add_response(method, response, params)
        stub.activate()
        stubs.append((name, c.meta.region_name, stub))
        return c

    expected: dict[str, list] = {}
    monkeypatch.setattr(boto3.Session, "client", client)
    return expected, stubs


def test_iam_serverless(aws):
    expected, stubs = aws
    expected["redshift-serverless"] = [(
        "get_credentials", {"workgroupName": "analytics-wg", "dbName": "analytics"},
        {"dbUser": "IAMR:analyst", "dbPassword": "temp"},
    )]
    rs = RedshiftConfig(host="wg.1.us-east-2.redshift-serverless.amazonaws.com", database="analytics",
                        iam=True, workgroup="analytics-wg")
    assert credentials(rs) == ("IAMR:analyst", "temp")
    assert stubs[0][1] == "us-east-2"  # region taken from the host
    stubs[0][2].assert_no_pending_responses()


def test_iam_provisioned_with_and_without_user(aws):
    expected, stubs = aws
    expected["redshift"] = [
        ("get_cluster_credentials", {"DbUser": "lazarillo_ro", "DbName": "analytics",
                                     "ClusterIdentifier": "my-cluster", "AutoCreate": False},
         {"DbUser": "IAM:lazarillo_ro", "DbPassword": "temp1"}),
    ]
    rs = RedshiftConfig(host=HOST, database="analytics", iam=True, cluster_identifier="my-cluster",
                        user="lazarillo_ro", region="eu-central-1")
    assert credentials(rs) == ("IAM:lazarillo_ro", "temp1")
    assert stubs[-1][1] == "eu-central-1"  # an explicit region wins over the host

    expected["redshift"] = [
        ("get_cluster_credentials_with_iam", {"DbName": "analytics", "ClusterIdentifier": "my-cluster"},
         {"DbUser": "IAMR:analyst", "DbPassword": "temp2"}),
    ]
    rs.user = None
    assert credentials(rs) == ("IAMR:analyst", "temp2")


def test_iam_errors_are_explained(aws):
    expected, _ = aws
    expected["redshift"] = [
        ("get_cluster_credentials_with_iam", {"DbName": "analytics", "ClusterIdentifier": "my-cluster"},
         "AccessDenied"),
    ]
    rs = RedshiftConfig(host=HOST, database="analytics", iam=True, cluster_identifier="my-cluster")
    with pytest.raises(WarehouseError, match="IAM.*AccessDenied"):
        credentials(rs)


def test_password_is_left_to_libpq():
    assert credentials(RedshiftConfig(host=HOST, database="a", user="ro")) == ("ro", None)


def test_password_from_a_named_env_var(monkeypatch):
    rs = RedshiftConfig(host=HOST, database="a", user="ro", password_env="RS_PASSWORD")
    with pytest.raises(WarehouseError, match="RS_PASSWORD"):
        credentials(rs)
    monkeypatch.setenv("RS_PASSWORD", "s3cret")
    assert credentials(rs) == ("ro", "s3cret")


class FakeRedshift(Engine):
    remote = True

    def __init__(self, rows: int = 10):
        self.sql: list[str] = []
        self.rows = rows

    def run(self, sql):
        self.sql.append(sql)
        return ["count"], [(self.rows,)]


@pytest.fixture
def fake(tmp_path):
    cfg = write_config(tmp_path, f"warehouse:\n  type: redshift\n  host: {HOST}\n  database: a\n"
                                 "guardrails:\n  max_transfer_rows: 5\n")
    engine = FakeRedshift(rows=10)
    return Warehouse(cfg, duckdb.connect(), engine=engine), engine


@pytest.mark.parametrize("sql", ["delete from analytics.t", "select 1; drop table analytics.t",
                                 "unload ('select 1') to 's3://x/'", "select * into t2 from analytics.t"])
def test_writes_never_reach_redshift(fake, sql):
    wh, engine = fake
    with pytest.raises(GuardrailViolation):
        wh.query(sql)
    assert engine.sql == []


def test_query_runs_on_redshift_capped(fake):
    wh, engine = fake
    wh.query("select * from analytics.t", max_rows=3)
    assert engine.sql == ["SELECT * FROM (select * from analytics.t) AS _lazarillo LIMIT 4"]


def test_transfer_budget(fake):
    wh, engine = fake
    with pytest.raises(GuardrailViolation, match="max_transfer_rows"):
        wh.localize("analytics.t", engine)


# --- against Postgres standing in for Redshift ------------------------------------------

@pytest.fixture(scope="module")
def pg():
    dsn = os.environ.get("LAZARILLO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("LAZARILLO_TEST_PG_DSN is not set")
    import psycopg2

    schema = f"lz_{uuid.uuid4().hex[:8]}"
    con = psycopg2.connect(dsn)
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute(f"""
            CREATE SCHEMA {schema}_a; CREATE SCHEMA {schema}_b;
            CREATE TABLE {schema}_a.t (id int, val varchar(10), email varchar(50), amount numeric(10,2));
            INSERT INTO {schema}_a.t VALUES (1, 'x', 'ana@example.com', 1.50), (2, 'y', NULL, 2.00),
                                           (3, 'z', NULL, 3.25);
            CREATE TABLE {schema}_b.t (id int, val varchar(10), extra int, amount numeric(10,2));
            INSERT INTO {schema}_b.t VALUES (1, 'x', 10, 1.50), (2, 'CHANGED', 20, 2.00), (4, 'w', 40, 4.00);
            CREATE SEQUENCE {schema}_a.seq;
        """)
    yield urlparse(dsn), schema
    with con.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema}_a CASCADE; DROP SCHEMA {schema}_b CASCADE")
    con.close()


@pytest.fixture
def pg_cfg(pg, tmp_path):
    url, schema = pg
    # The same rows as schema a, as a lake file, to diff across engines.
    pq.write_table(pa.table({
        "id": pa.array([1, 2, 3], pa.int32()), "val": ["x", "y", "z"],
        "email": ["ana@example.com", None, None],
        "amount": pa.array([1.5, 2, 3.25], pa.float64()).cast(pa.decimal128(10, 2)),
    }), tmp_path / "t.parquet")
    cfg = write_config(tmp_path, f"""\
warehouse:
  type: redshift
  host: {url.hostname}
  port: {url.port or 5432}
  database: {url.path.lstrip('/')}
  user: {url.username}
  password: {url.password}
  sslmode: disable
  timeout_seconds: 1
guardrails:
  max_rows: 2
  pii_columns: [email]
""")
    return cfg, schema


@pytest.mark.postgres
def test_pg_query_is_capped_and_masked(pg_cfg):
    cfg, s = pg_cfg
    with open_warehouse(cfg) as wh:
        result = wh.query(f"select id, email from {s}_a.t order by id")
    assert result.truncated and result.rows == [(1, "•••"), (2, None)]


@pytest.mark.postgres
def test_pg_session_is_read_only(pg_cfg):
    cfg, s = pg_cfg
    with open_warehouse(cfg) as wh:
        # Passes the parser, but nextval writes: the server refuses it.
        with pytest.raises(WarehouseError, match="read-only"):
            wh.query(f"select nextval('{s}_a.seq')")
        assert wh.query("select 1 as ok").rows == [(1,)]  # the session recovers


@pytest.mark.postgres
def test_pg_statement_timeout(pg_cfg):
    cfg, _ = pg_cfg
    with open_warehouse(cfg) as wh, pytest.raises(WarehouseError, match="timeout"):
        wh.query("select pg_sleep(3)")


@pytest.mark.postgres
def test_pg_diff_runs_in_the_warehouse(pg_cfg):
    cfg, s = pg_cfg
    with open_warehouse(cfg) as wh:
        r = diff(wh, f"{s}_a.t", f"{s}_b.t", key=["id"])
        w = diff(wh, f"{s}_a.t", f"{s}_b.t", key=["id"], where="id = 1")
    assert (r.left_rows, r.right_rows) == (3, 3)
    assert (r.only_left, r.only_right, r.changed) == (1, 1, 1)
    assert r.columns_only_left == ["email"] and r.columns_only_right == ["extra"]
    assert r.changed_by_column == {"val": 1}
    assert r.samples["changed"] == [{"id": 2, "val__left": "y", "val__right": "CHANGED"}]
    assert not r.type_changes
    assert (w.only_left, w.only_right, w.changed) == (0, 0, 0)


@pytest.mark.postgres
def test_pg_diff_against_a_lake_file(pg_cfg):
    cfg, s = pg_cfg
    with open_warehouse(cfg) as wh:
        r = diff(wh, "parquet:t.parquet", f"{s}_a.t", key=["id"])
        assert r.identical, r.to_markdown()
        cfg.guardrails.max_transfer_rows = 2
        with pytest.raises(GuardrailViolation, match="max_transfer_rows"):
            diff(wh, "parquet:t.parquet", f"{s}_a.t", key=["id"])
        # A where clause runs in the warehouse first, so less crosses the wire.
        assert diff(wh, "parquet:t.parquet", f"{s}_a.t", key=["id"], where="id <= 2").left_rows == 2


@pytest.mark.postgres
def test_pg_cli(pg_cfg):
    cfg, s = pg_cfg
    config = str(cfg.root / "lazarillo.yml")
    out = CliRunner().invoke(main, ["-c", config, "query", f"select val from {s}_a.t order by id"])
    assert out.exit_code == 0 and "| val |" in out.output
    out = CliRunner().invoke(main, ["-c", config, "query", f"select nope from {s}_a.t"])
    assert out.exit_code == 1 and "nope" in out.output


@pytest.mark.postgres
def test_pg_doctor(pg_cfg):
    from lazarillo.doctor import doctor

    cfg, s = pg_cfg
    report = doctor(cfg.root / "lazarillo.yml")
    text = report.to_markdown()
    assert report.ok, text
    assert f"as `{cfg.redshift.user}`" in text and f"`{s}_a` (1)" in text
    assert {c.title: c.status for c in report.checks}["Read-only"] == "ok"
