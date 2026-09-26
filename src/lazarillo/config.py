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
    pii_columns: list[str] = field(default_factory=lambda: ["email", "name", "phone"])


@dataclass
class DbtConfig:
    project_dir: Path
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
class Attachment:
    """A database attached next to the warehouse: another DuckDB file or a DuckLake."""

    path: str
    type: str = "duckdb"
    # DuckLake only: where the Parquet files live. Optional once the lake exists.
    data_path: str | None = None


@dataclass
class Config:
    root: Path
    warehouse: Path
    attach: dict[str, Attachment] = field(default_factory=dict)
    dbt: DbtConfig | None = None
    iceberg_catalog: dict[str, str] | None = None
    s3: S3Config | None = None
    # Where `delta:` and `parquet:` refs may read from. Defaults to the folder holding
    # lazarillo.yml, so a stray ref cannot wander around the disk or other buckets.
    locations: list[str] = field(default_factory=list)
    guardrails: Guardrails = field(default_factory=Guardrails)

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

    cfg = Config(root=root, warehouse=resolve(raw["warehouse"]["path"]))
    cfg.attach = {name: _attachment(cfg, v) for name, v in (raw.get("attach") or {}).items()}

    if dbt := raw.get("dbt"):
        cfg.dbt = DbtConfig(
            project_dir=resolve(dbt["project_dir"]),
            **{k: v for k, v in dbt.items() if k not in ("project_dir", "profiles_dir")},
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
