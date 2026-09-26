"""What the agent is allowed to do, enforced in code rather than in the prompt."""

from __future__ import annotations

import re

import duckdb

ALLOWED = {duckdb.StatementType.SELECT, duckdb.StatementType.EXPLAIN}
MASK = "•••"


class GuardrailViolation(Exception):
    """Raised when a request crosses one of the harness's hard limits."""


def check_read_only(sql: str) -> str:
    """Accept exactly one read-only statement and return it."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        raise GuardrailViolation(f"SQL does not parse: {e}") from e
    if len(statements) != 1:
        raise GuardrailViolation("Exactly one statement per call, please.")
    if statements[0].type not in ALLOWED:
        raise GuardrailViolation(
            f"{statements[0].type.name} is not allowed: Lazarillo only reads. "
            "Propose changes as dbt code and let `verify` build them in the dev schema."
        )
    return sql.strip().rstrip(";")


def with_row_limit(sql: str, max_rows: int) -> str:
    return f"SELECT * FROM ({sql}) AS _lazarillo LIMIT {int(max_rows)}"


def is_pii(column: str, patterns: list[str]) -> bool:
    return any(p.lower() in column.lower() for p in patterns)


def mask_rows(columns: list[str], rows: list[tuple], patterns: list[str]) -> list[tuple]:
    hidden = [is_pii(c, patterns) for c in columns]
    return [tuple(MASK if h and v is not None else v for v, h in zip(r, hidden)) for r in rows]


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")


def check_identifier(name: str) -> str:
    """Relation names are interpolated into SQL, so they must be plain identifiers."""
    if not _IDENT.match(name):
        raise GuardrailViolation(f"Not a valid relation name: {name!r}")
    return name
