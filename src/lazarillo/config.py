"""Load `lazarillo.yml`. Every relative path is resolved against the config file."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_NAME = "lazarillo.yml"

# Object-store URIs are kept as they are; everything else is a local path.
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def is_uri(value: str) -> bool:
    return bool(_URI.match(value)) and not value.startswith("file://")


@dataclass
class Guardrails:
    max_rows: int = 200
    # Diffs between the remote warehouse and a local table copy the warehouse side
    # locally; refuse above this many rows and ask for a narrower --where.
    max_transfer_rows: int = 1_000_000
    pii_columns: list[str] = field(default_factory=lambda: ["email", "name", "phone"])


@dataclass
class DbtCloudConfig:
    """Read the production manifest from a dbt Cloud job instead of the local target/."""

    account_id: int
    # The job that builds production; its latest successful run provides the manifest.
    job_id: int
    # Your access URL: cloud.getdbt.com, emea.dbt.com, au.dbt.com or
    # ACCOUNT_PREFIX.us1.dbt.com. A full http(s):// URL works too.
    host: str = "cloud.getdbt.com"
    # Environment variable holding a token that can read job artifacts. The token itself
    # never goes in lazarillo.yml.
    token_env: str = "DBT_CLOUD_API_TOKEN"
    # Downloads are cached in .lazarillo/ next to lazarillo.yml for this long.
    cache_minutes: int = 10


@dataclass
class DbtConfig:
    # Needed by verify, which builds locally. map, describe and impact only need a manifest,
    # which can come from dbt Cloud instead.
    project_dir: Path | None = None
    cloud: DbtCloudConfig | None = None
    prod_target: str = "prod"
    dev_target: str = "dev"
    prod_schema: str = "analytics"
    dev_schema: str = "dev"
    # Where profiles.yml lives. None means: the project dir if it has one, else dbt's
    # own default (DBT_PROFILES_DIR or ~/.dbt).
    profiles_dir: Path | None = None


@dataclass
class S3Config:
    """How to reach S3. Anything left out falls back to the AWS default chain
    (environment variables, ~/.aws, SSO, instance or task role)."""

    region: str | None = None
    # A custom endpoint such as MinIO or LocalStack, e.g. http://localhost:9000.
    endpoint: str | None = None
    profile: str | None = None
    # Static keys work, but prefer a profile or environment variables so secrets stay
    # out of the repository.
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None


@dataclass
class RedshiftConfig:
    """How to reach a Redshift warehouse (provisioned or Serverless).

    With `iam: true` a short-lived password comes from the Redshift API, using the same
    AWS chain as the lake. Otherwise it comes from the `password_env` variable, or libpq
    falls back to PGPASSWORD or ~/.pgpass.
    """

    host: str
    database: str
    port: int = 5439
    user: str | None = None
    # Prefer PGPASSWORD, ~/.pgpass or IAM so secrets stay out of the repository.
    password: str | None = None
    # Name of an environment variable holding the password, e.g. the one dbt's profile uses.
    password_env: str | None = None
    iam: bool = False
    cluster_identifier: str | None = None  # provisioned cluster, for IAM
    workgroup: str | None = None           # Serverless workgroup, for IAM
    region: str | None = None
    profile: str | None = None
    sslmode: str = "require"
    # Every statement is cancelled after this long, so one query cannot hog the cluster.
    timeout_seconds: int = 300


@dataclass
class Attachment:
    """A database attached next to the warehouse: another DuckDB file or a DuckLake."""

    path: str
    type: str = "duckdb"
    # DuckLake only: where the Parquet files live. Optional once the lake exists.
    data_path: str | None = None


@dataclass
class Config:
    root: Path
    # The DuckDB file, or None when the warehouse is Redshift.
    warehouse: Path | None
    attach: dict[str, Attachment] = field(default_factory=dict)
    dbt: DbtConfig | None = None
    iceberg_catalog: dict[str, str] | None = None
    s3: S3Config | None = None
    # Where `delta:` and `parquet:` refs may read from. Defaults to the folder holding
    # lazarillo.yml, so a stray ref cannot wander around the disk or other buckets.
    locations: list[str] = field(default_factory=list)
    guardrails: Guardrails = field(default_factory=Guardrails)
    redshift: RedshiftConfig | None = None

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p).resolve()

    def location(self, value: str) -> str:
        """A lake location: an object-store URI as given, or an absolute local path."""
        if is_uri(value):
            return value
        return str(self.path(str(Path(value.removeprefix("file://")).expanduser())))


def find_config(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        if (d / CONFIG_NAME).exists():
            return d / CONFIG_NAME
    raise FileNotFoundError(f"No {CONFIG_NAME} found in {here} or its parents")


def _attachment(cfg: Config, value: str | dict) -> Attachment:
    if isinstance(value, str):
        kind, sep, rest = value.partition(":")
        value = {"type": "ducklake", "path": rest} if sep and kind == "ducklake" else {"path": value}
    att = Attachment(**value)
    if att.type not in ("duckdb", "ducklake"):
        raise ValueError(f"Unknown attach type {att.type!r}; use duckdb or ducklake")
    # A DuckLake catalog can live in Postgres or MySQL too; only plain files are resolved.
    if att.type == "duckdb" or not re.match(r"^(postgres|mysql|sqlite|duckdb):", att.path):
        att.path = cfg.location(att.path)
    if att.data_path:
        att.data_path = cfg.location(att.data_path)
    return att


def load_config(path: Path | None = None) -> Config:
    path = (path or find_config()).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    root = path.parent

    def resolve(v: str) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (root / p).resolve()

    wh = dict(raw["warehouse"])
    kind = wh.pop("type", "duckdb")
    if kind == "duckdb":
        cfg = Config(root=root, warehouse=resolve(wh["path"]))
    elif kind == "redshift":
        cfg = Config(root=root, warehouse=None, redshift=RedshiftConfig(**wh))
        if cfg.redshift.iam and not (cfg.redshift.cluster_identifier or cfg.redshift.workgroup):
            raise ValueError("Redshift IAM auth needs `cluster_identifier` or `workgroup` in lazarillo.yml")
    else:
        raise ValueError(f"Unknown warehouse type {kind!r}; use duckdb or redshift")
    cfg.attach = {name: _attachment(cfg, v) for name, v in (raw.get("attach") or {}).items()}

    if dbt := raw.get("dbt"):
        if not dbt.get("project_dir") and not dbt.get("cloud"):
            raise ValueError("The `dbt:` section needs project_dir, cloud, or both")
        cfg.dbt = DbtConfig(
            project_dir=resolve(dbt["project_dir"]) if dbt.get("project_dir") else None,
            cloud=DbtCloudConfig(**dbt["cloud"]) if dbt.get("cloud") else None,
            **{k: v for k, v in dbt.items() if k not in ("project_dir", "profiles_dir", "cloud")},
        )
        if dbt.get("profiles_dir"):
            cfg.dbt.profiles_dir = resolve(str(Path(dbt["profiles_dir"]).expanduser()))

    landing = raw.get("landing") or {}
    if ice := landing.get("iceberg_catalog"):
        cfg.iceberg_catalog = {
            k: (f"sqlite:///{resolve(v.removeprefix('sqlite:///'))}" if k == "uri" and v.startswith("sqlite:///") else
                f"file://{cfg.location(v)}" if k == "warehouse" and not is_uri(v) else v)
            for k, v in ice.items()
        }
    if (s3 := landing.get("s3")) is not None:
        cfg.s3 = S3Config(**s3)
    cfg.locations = [cfg.location(v) for v in landing.get("locations") or ["."]]

    if g := raw.get("guardrails"):
        cfg.guardrails = Guardrails(**g)
    return cfg
