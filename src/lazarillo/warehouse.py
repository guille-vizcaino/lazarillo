"""A read-only window onto the warehouse and the landing zone.

Relations are addressed with short references:

    analytics.fct_orders             a table or view in the warehouse
    src.orders                       a table in an attached database (e.g. the source system)
    delta:landing/delta/orders       a Delta Lake table (path relative to lazarillo.yml)
    iceberg:landing.orders           an Iceberg table in the configured catalog
    parquet:landing/raw/*.parquet    a set of Parquet files
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import duckdb

from .config import Config
from .guardrails import GuardrailViolation, check_identifier, check_read_only, mask_rows, with_row_limit

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
    def __init__(self, cfg: Config, con: duckdb.DuckDBPyConnection):
        self.cfg = cfg
        self.con = con

    def relation(self, ref: str) -> str:
        """Turn a reference into something that can follow FROM."""
        kind, _, target = ref.partition(":")
        if not target:
            return check_identifier(ref)
        if kind == "delta":
            from deltalake import DeltaTable

            return self._register(DeltaTable(str(self.cfg.path(target))).to_pyarrow_dataset())
        if kind == "iceberg":
            from pyiceberg.catalog.sql import SqlCatalog

            if not self.cfg.iceberg_catalog:
                raise ValueError("No landing.iceberg_catalog configured in lazarillo.yml")
            props = dict(self.cfg.iceberg_catalog)
            catalog = SqlCatalog(props.pop("name", "default"), **props)
            return self._register(catalog.load_table(target).scan().to_arrow())
        if kind == "parquet":
            import glob

            import pyarrow.dataset as ds

            return self._register(ds.dataset(sorted(glob.glob(str(self.cfg.path(target))))))
        raise ValueError(f"Unknown relation kind {kind!r} in {ref!r}")

    def _register(self, arrow_obj) -> str:
        name = f"_lz_{next(_counter)}"
        self.con.register(name, arrow_obj)
        return name

    def query(self, sql: str, max_rows: int | None = None) -> QueryResult:
        """Run agent-supplied SQL: one read-only statement, row-capped, PII masked."""
        limit = max_rows or self.cfg.guardrails.max_rows
        try:
            cur = self.con.execute(with_row_limit(check_read_only(sql), limit + 1))
        except duckdb.PermissionException as e:
            raise GuardrailViolation("SQL cannot touch the filesystem; use a relation ref instead.") from e
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return QueryResult(
            columns=columns,
            rows=mask_rows(columns, rows[:limit], self.cfg.guardrails.pii_columns),
            truncated=len(rows) > limit,
        )

    def columns(self, relation: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()]


@contextmanager
def open_warehouse(cfg: Config) -> Iterator[Warehouse]:
    """Open the warehouse read-only. The connection is short-lived so dbt can take the lock."""
    con = duckdb.connect(str(cfg.warehouse), read_only=True)
    try:
        for name, path in cfg.attach.items():
            con.execute(f"ATTACH '{path}' AS {check_identifier(name)} (READ_ONLY)")
        # From here on SQL cannot touch the filesystem (read_csv('/etc/...'), COPY, ATTACH...).
        # Lake tables are read by Python and handed to DuckDB as Arrow instead.
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        yield Warehouse(cfg, con)
    finally:
        con.close()
