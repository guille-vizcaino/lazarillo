"""A read-only window onto the warehouse and the landing zone.

Relations are addressed with short references:

    analytics.fct_orders             a table or view in the warehouse
    src.orders                       a table in an attached database (e.g. the source system)
    delta:landing/delta/orders       a Delta Lake table (path relative to lazarillo.yml)
    delta:s3://bucket/landing/orders a Delta Lake table in S3
    iceberg:landing.orders           an Iceberg table in the configured catalog (SQL, Glue, REST...)
    parquet:landing/raw/*.parquet    a set of Parquet files, local or s3://
    lake.orders                      a table in an attached DuckLake

Paths must sit inside `landing.locations`; Iceberg tables are reached through the catalog.

The warehouse is a DuckDB file or Redshift. With Redshift, plain `schema.table` refs and
agent SQL run there, while lake tables and attachments stay in a local DuckDB.
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import duckdb

from .config import Attachment, Config, is_uri
from .engine import DuckDBEngine, Engine
from .guardrails import GuardrailViolation, check_identifier, check_read_only, mask_rows, with_row_limit
from .lake import Lake, LakeError

_counter = itertools.count()


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool

    def to_markdown(self) -> str:
        if not self.columns:
            return "_(no columns)_"
        head = "| " + " | ".join(self.columns) + " |"
        sep = "|" + "---|" * len(self.columns)
        body = ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in self.rows]
        tail = ["", f"_Truncated to {len(self.rows)} rows._"] if self.truncated else []
        return "\n".join([head, sep, *body, *tail])


class Warehouse:
    def __init__(self, cfg: Config, con: duckdb.DuckDBPyConnection, lake: Lake | None = None,
                 engine: Engine | None = None):
        self.cfg = cfg
        self.con = con  # the local DuckDB: lake tables and attachments
        self.lake = lake or Lake(cfg)
        self.local = DuckDBEngine(con)
        # Where warehouse tables live and agent SQL runs: the same DuckDB, or Redshift.
        self.engine = engine or self.local

    def relation(self, ref: str) -> str:
        """Turn a reference into something that can follow FROM."""
        kind, _, target = ref.partition(":")
        if not target:
            return check_identifier(ref)
        readers = {"delta": self.lake.delta, "iceberg": self.lake.iceberg, "parquet": self.lake.parquet}
        if kind not in readers:
            raise GuardrailViolation(f"Unknown relation kind {kind!r} in {ref!r}")
        try:
            return self._register(readers[kind](target))
        except GuardrailViolation:
            raise
        except ImportError as e:
            extra = "glue" if (self.cfg.iceberg_catalog or {}).get("type") == "glue" and kind == "iceberg" else kind
            raise LakeError(f"Reading {kind} tables needs pip install 'lazarillo[{extra}]' ({e})") from e
        except Exception as e:
            raise LakeError(f"Could not read {ref!r}: {type(e).__name__}: {e}") from e

    def resolve(self, ref: str) -> tuple[str, Engine]:
        """Like `relation`, plus the engine that can read it."""
        kind, _, target = ref.partition(":")
        local = bool(target) or ref.split(".")[0] in self.cfg.attach
        return self.relation(ref), (self.local if local else self.engine)

    def localize(self, relation: str, engine: Engine) -> str:
        """Copy a warehouse relation into the local DuckDB, so it can meet a lake table."""
        if engine is self.local:
            return relation
        cap = self.cfg.guardrails.max_transfer_rows
        rows = engine.scalar(f"SELECT count(*) FROM {relation} AS _lz")
        if rows > cap:
            raise GuardrailViolation(
                f"{rows:,} warehouse rows would be copied locally to compare with a lake table; "
                f"the limit is {cap:,} (guardrails.max_transfer_rows). Narrow it with a `where`."
            )
        return self._register(engine.fetch_arrow(f"SELECT * FROM {relation} AS _lz"))

    def _register(self, arrow_obj) -> str:
        name = f"_lz_{next(_counter)}"
        self.con.register(name, arrow_obj)
        return name

    def query(self, sql: str, max_rows: int | None = None) -> QueryResult:
        """Run agent-supplied SQL: one read-only statement, row-capped, PII masked."""
        limit = max_rows or self.cfg.guardrails.max_rows
        try:
            columns, rows = self.engine.run(with_row_limit(check_read_only(sql), limit + 1))
        except duckdb.PermissionException as e:
            raise GuardrailViolation("SQL cannot touch the filesystem; use a relation ref instead.") from e
        return QueryResult(
            columns=columns,
            rows=mask_rows(columns, rows[:limit], self.cfg.guardrails.pii_columns),
            truncated=len(rows) > limit,
        )

    def columns(self, relation: str) -> list[tuple[str, str]]:
        return self.local.columns(relation)


@contextmanager
def open_warehouse(cfg: Config) -> Iterator[Warehouse]:
    """Open the warehouse read-only. The connection is short-lived so dbt can take the lock."""
    if cfg.redshift:
        # Lake tables and attachments still need a DuckDB; an empty one in memory will do.
        con = duckdb.connect(":memory:")
    elif not cfg.warehouse.exists():
        raise FileNotFoundError(
            f"No warehouse at {cfg.warehouse}. Point `warehouse.path` in lazarillo.yml at a DuckDB file."
        )
    else:
        con = duckdb.connect(str(cfg.warehouse), read_only=True)
    lake = Lake(cfg)
    remote = None
    try:
        attached = [_attach(con, lake, check_identifier(name), att) for name, att in cfg.attach.items()]
        if lake_dirs := [d for d in attached if d]:
            # DuckLake reads its Parquet files through DuckDB, so those folders, and only
            # those, stay readable once external access is off.
            current = con.execute("SELECT current_setting('allowed_directories')").fetchone()[0]
            dirs = ", ".join(_sql_str(d) for d in [*current, *lake_dirs])
            con.execute(f"SET allowed_directories = [{dirs}]")
        # From here on SQL cannot touch the filesystem (read_csv('/etc/...'), COPY, ATTACH...).
        # Other lake tables are read by Python and handed to DuckDB as Arrow instead.
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        if cfg.redshift:
            from .redshift import connect

            remote = connect(cfg.redshift)
        yield Warehouse(cfg, con, lake, remote)
    finally:
        if remote:
            remote.close()
        con.close()


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _attach(con: duckdb.DuckDBPyConnection, lake: Lake, name: str, att: Attachment) -> str | None:
    """Attach read-only, before external access is switched off. Returns a DuckLake's data folder."""
    if att.type == "duckdb":
        con.execute(f"ATTACH {_sql_str(att.path)} AS {name} (READ_ONLY)")
        return None

    def attach(data_path: str | None) -> str:
        # Read-only, so overriding the data path only affects this connection.
        options = "READ_ONLY" + (f", DATA_PATH {_sql_str(data_path)}, OVERRIDE_DATA_PATH true" if data_path else "")
        try:
            con.execute(f"ATTACH {_sql_str('ducklake:' + att.path)} AS {name} ({options})")
        except duckdb.Error as e:
            if "ducklake" in str(e).lower() and "extension" in str(e).lower():
                raise LakeError(
                    "Attaching a DuckLake needs DuckDB's ducklake extension. Run once, with network: "
                    "python -c \"import duckdb; duckdb.install_extension('ducklake')\""
                ) from e
            raise
        return con.execute(f"SELECT data_path FROM ducklake_settings({_sql_str(name)})").fetchone()[0]

    data_path = attach(att.data_path)
    if not is_uri(data_path) and not Path(data_path).is_absolute():
        # DuckLake resolves a relative data path against the working directory; anchor it
        # to the catalog file instead so lazarillo works from any folder.
        con.execute(f"DETACH {name}")
        data_path = attach(str(Path(att.path).parent / data_path))
    if is_uri(data_path):
        # DuckLake reads S3 through DuckDB's httpfs, which takes credentials from a secret.
        con.execute(lake.s3.duckdb_secret())
    return data_path.rstrip("/") + "/"
