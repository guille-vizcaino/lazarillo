"""Where SQL runs: the local DuckDB, or a remote warehouse such as Redshift.

`diff` and `query` only speak to an `Engine`, and they generate SQL that DuckDB and
Redshift both accept, so the same checks run wherever the data lives.
"""

from __future__ import annotations

import duckdb


class WarehouseError(Exception):
    """The warehouse could not be reached or rejected a query."""


class Engine:
    remote = False

    def run(self, sql: str) -> tuple[list[str], list[tuple]]:
        raise NotImplementedError

    def columns(self, relation: str) -> list[tuple[str, str]]:
        raise NotImplementedError

    def scalar(self, sql: str):
        return self.run(sql)[1][0][0]


class DuckDBEngine(Engine):
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con

    def run(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self.con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()

    def columns(self, relation: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()]
