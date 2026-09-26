"""Lake settings and the location allowlist. No network, no AWS account."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lazarillo.config import S3Config, load_config
from lazarillo.guardrails import GuardrailViolation
from lazarillo.lake import Lake, S3Access
from lazarillo.warehouse import open_warehouse


def write_config(tmp_path: Path, body: str):
    import duckdb

    duckdb.connect(str(tmp_path / "wh.duckdb")).close()
    (tmp_path / "lazarillo.yml").write_text("warehouse: {path: wh.duckdb}\n" + body)
    return load_config(tmp_path / "lazarillo.yml")


def test_defaults_keep_the_lake_next_to_the_config(tmp_path):
    cfg = write_config(tmp_path, "")
    assert cfg.s3 is None
    assert cfg.locations == [str(tmp_path)]


def test_landing_section(tmp_path):
    cfg = write_config(tmp_path, """
attach:
  src: source.duckdb
  lake: ducklake:meta.ducklake
  lake2: {type: ducklake, path: "postgres:dbname=lake", data_path: s3://bucket/lake/}
landing:
  locations: [raw, s3://bucket/landing/]
  s3: {region: eu-west-1, endpoint: "http://localhost:9000"}
  iceberg_catalog: {type: glue, warehouse: s3://bucket/iceberg}
""")
    assert cfg.attach["src"].type == "duckdb" and cfg.attach["src"].path == str(tmp_path / "source.duckdb")
    assert (cfg.attach["lake"].type, cfg.attach["lake"].path) == ("ducklake", str(tmp_path / "meta.ducklake"))
    assert cfg.attach["lake2"].path == "postgres:dbname=lake"  # not a file, left alone
    assert cfg.attach["lake2"].data_path == "s3://bucket/lake/"
    assert cfg.locations == [str(tmp_path / "raw"), "s3://bucket/landing/"]
    assert cfg.s3 == S3Config(region="eu-west-1", endpoint="http://localhost:9000")
    assert cfg.iceberg_catalog["warehouse"] == "s3://bucket/iceberg"  # not turned into a local path


def test_local_iceberg_catalog_still_resolves(tmp_path):
    cfg = write_config(tmp_path, """
landing:
  iceberg_catalog: {uri: "sqlite:///cat.db", warehouse: "file://ice"}
""")
    assert cfg.iceberg_catalog == {"uri": f"sqlite:///{tmp_path}/cat.db", "warehouse": f"file://{tmp_path}/ice"}


def test_unknown_attach_type(tmp_path):
    with pytest.raises(ValueError, match="attach type"):
        write_config(tmp_path, "attach: {x: {type: postgres, path: db}}\n")


@pytest.mark.parametrize("location, allowed", [
    ("s3://lake/landing/orders", True),
    ("s3a://lake/landing/orders", True),
    ("s3://lake/landing", True),
    ("s3://lake/landing-archive/orders", False),  # prefix match stops at a folder boundary
    ("s3://lake/landing/../finance/salaries", False),
    ("s3://other-bucket/landing/orders", False),
    ("gs://lake/landing/orders", False),
    ("raw/orders", True),
    ("raw/../../etc", False),
    ("/etc", False),
])
def test_location_allowlist(tmp_path, location, allowed):
    cfg = write_config(tmp_path, "landing: {locations: [raw, s3://lake/landing]}\n")
    lake = Lake(cfg)
    if allowed:
        lake.check_location(location)
    else:
        with pytest.raises(GuardrailViolation):
            lake.check_location(location)


def test_symlinks_cannot_escape(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    pq.write_table(pa.table({"id": [1]}), outside / "secret.parquet")
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "link.parquet").symlink_to(outside / "secret.parquet")
    pq.write_table(pa.table({"id": [2]}), tmp_path / "raw" / "ok.parquet")
    cfg = write_config(tmp_path, "landing: {locations: [raw]}\n")
    with pytest.raises(GuardrailViolation):
        Lake(cfg).parquet("raw/link.parquet")
    with pytest.raises(GuardrailViolation):
        Lake(cfg).parquet("raw/*.parquet")  # the glob picks up the link too


def test_local_parquet_glob_recurses(tmp_path):
    for d in ("raw/day=1", "raw/day=2"):
        (tmp_path / d).mkdir(parents=True)
        pq.write_table(pa.table({"id": [1, 2]}), tmp_path / d / "part.parquet")
    cfg = write_config(tmp_path, "")
    with open_warehouse(cfg) as wh:
        rel = wh.relation("parquet:raw/**/*.parquet")
        assert wh.con.execute(f"select count(*) from {rel}").fetchone()[0] == 4


def test_unknown_ref_kind_is_refused(tmp_path):
    with open_warehouse(write_config(tmp_path, "")) as wh, pytest.raises(GuardrailViolation):
        wh.relation("s3://lake/orders")


def test_sql_cannot_reach_s3_even_with_s3_configured(tmp_path):
    cfg = write_config(tmp_path, "landing: {s3: {region: eu-west-1, access_key_id: a, secret_access_key: b}}\n")
    with open_warehouse(cfg) as wh, pytest.raises(GuardrailViolation):
        wh.query("select * from read_parquet('s3://lake/landing/*.parquet')")


def test_s3_access_translates_for_each_reader():
    s3 = S3Access(region="eu-west-1", endpoint="http://localhost:9000",
                  access_key_id="AKIA", secret_access_key="it's-secret", session_token="tok")
    assert s3.delta_options() == {
        "AWS_REGION": "eu-west-1", "AWS_ENDPOINT_URL": "http://localhost:9000", "AWS_ALLOW_HTTP": "true",
        "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "it's-secret", "AWS_SESSION_TOKEN": "tok",
    }
    props = s3.iceberg_properties("glue")
    assert props["s3.endpoint"] == "http://localhost:9000" and props["glue.region"] == "eu-west-1"
    assert props["glue.access-key-id"] == props["s3.access-key-id"] == "AKIA"
    assert "glue.region" not in s3.iceberg_properties("sql")
    secret = s3.duckdb_secret()
    assert "SECRET 'it''s-secret'" in secret  # quotes are escaped
    assert "ENDPOINT 'localhost:9000'" in secret and "USE_SSL false" in secret
    fs = s3.pyarrow_fs()
    assert fs.region == "eu-west-1"


def test_https_endpoint_without_scheme():
    s3 = S3Access(endpoint="s3.eu-west-1.amazonaws.com")
    assert s3.delta_options() == {"AWS_ENDPOINT_URL": "https://s3.eu-west-1.amazonaws.com"}
    assert "USE_SSL true" in s3.duckdb_secret()


@pytest.fixture
def aws_files(tmp_path, monkeypatch):
    """A throwaway ~/.aws with one profile, and no credentials anywhere else."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config").write_text("[profile lake-readonly]\nregion = eu-south-2\n")
    (tmp_path / "credentials").write_text(
        "[lake-readonly]\naws_access_key_id = AKIDPROFILE\naws_secret_access_key = from-profile\n"
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return tmp_path


def test_profile_is_resolved_once_for_every_reader(aws_files):
    s3 = S3Access.resolve(S3Config(profile="lake-readonly"))
    assert (s3.access_key_id, s3.secret_access_key, s3.region) == ("AKIDPROFILE", "from-profile", "eu-south-2")


def test_explicit_keys_win_over_the_default_chain(aws_files, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FROM_ENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
    assert S3Access.resolve(S3Config()).access_key_id == "FROM_ENV"
    assert S3Access.resolve(S3Config(access_key_id="EXPLICIT", secret_access_key="x")).access_key_id == "EXPLICIT"


def test_no_credentials_is_not_an_error(aws_files):
    # Public buckets and instance roles resolved later by each library still work.
    assert S3Access.resolve(None).access_key_id is None
