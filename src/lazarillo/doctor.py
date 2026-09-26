"""`lazarillo doctor`: check that the harness can do its job before an agent relies on it.

Every check goes through the same door as the agent (`open_warehouse` and its guardrails),
plus one probe the agent never gets: a write, which the warehouse itself must refuse.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

from .config import Config, find_config, load_config
from .context import DataMap
from .dbt_cloud import DbtCloudError
from .engine import Engine, WarehouseError
from .guardrails import GuardrailViolation
from .lake import LakeError
from .warehouse import open_warehouse

ICONS = {"ok": "✅", "warn": "⚠️", "fail": "❌"}

# Schemas with tables, as the warehouse's own catalog reports them. Runs on DuckDB and Redshift.
SCHEMAS_SQL = """\
SELECT table_schema, count(*) AS tables
FROM information_schema.tables
WHERE table_catalog = current_database()
  AND table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_internal')
GROUP BY table_schema
ORDER BY table_schema"""


@dataclass
class Check:
    status: str  # ok, warn or fail
    title: str
    detail: str = ""


@dataclass
class DoctorReport:
    config: Path | None
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def add(self, status: str, title: str, detail: str = "") -> None:
        self.checks.append(Check(status, title, detail))

    def to_markdown(self) -> str:
        out = ["## Lazarillo doctor", ""]
        out += [f"- {ICONS[c.status]} **{c.title}**" + (f": {c.detail}" if c.detail else "") for c in self.checks]
        out += ["", "Ready for an agent." if self.ok else "Fix the ❌ items above, then run `lazarillo doctor` again."]
        return "\n".join(out)


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _where(cfg: Config) -> str:
    if cfg.redshift:
        rs = cfg.redshift
        return f"Redshift `{rs.host}:{rs.port}`, database `{rs.database}`"
    return f"DuckDB `{cfg.warehouse}`"


def _write_is_refused(engine: Engine) -> bool:
    """Try a write the guardrails would never let through, straight at the warehouse.

    On Redshift it is a temporary table, so even a failed guard would leave nothing behind.
    """
    sql = "CREATE TEMP TABLE _lazarillo_doctor (x int)" if engine.remote else "CREATE TABLE _lazarillo_doctor (x int)"
    try:
        engine.run(sql)
    except (duckdb.Error, WarehouseError):
        return True
    try:
        engine.run("DROP TABLE _lazarillo_doctor")
    except (duckdb.Error, WarehouseError):
        pass
    return False


def _warehouse(cfg: Config, report: DoctorReport) -> dict[str, int] | None:
    """Connection, read-only guard and visible tables. Returns tables per schema."""
    try:
        with open_warehouse(cfg) as wh:
            wh.query("SELECT 1")
            who = f" as `{wh.engine.scalar('SELECT current_user')}`" if wh.engine.remote else ""
            report.add("ok", "Connection", _where(cfg) + who)
            if _write_is_refused(wh.engine):
                report.add("ok", "Read-only", "the warehouse itself refuses writes, on top of the SQL guardrails")
            else:
                report.add("fail", "Read-only", "a test write was accepted; check the connection settings")
            schemas = {s: n for s, n in wh.engine.run(SCHEMAS_SQL)[1]}
    except (FileNotFoundError, GuardrailViolation, LakeError, WarehouseError, duckdb.Error) as e:
        hint = (" Check the host, network access (VPN, security group) and credentials "
                "(IAM profile, password_env, PGPASSWORD or ~/.pgpass)." if cfg.redshift else "")
        report.add("fail", "Connection", f"{_where(cfg)}: {str(e).strip().rstrip('.')}.{hint}")
        return None
    if schemas:
        listed = ", ".join(f"`{s}` ({n})" for s, n in list(schemas.items())[:10])
        more = f" and {len(schemas) - 10} more schemas" if len(schemas) > 10 else ""
        report.add("ok", "Tables", listed + more)
    else:
        report.add("warn", "Tables", "none visible to this user; grant it SELECT on the schemas the agent should read")
    return schemas


def _dbt(cfg: Config, schemas: dict[str, int] | None, report: DoctorReport) -> None:
    d = cfg.dbt
    try:
        dm = DataMap.from_config(cfg)
    except FileNotFoundError as e:
        report.add("fail", "dbt manifest", f"{e}. Run `dbt parse` (or a build) so target/manifest.json exists.")
    except (DbtCloudError, ValueError) as e:
        report.add("fail", "dbt manifest", str(e))
    else:
        origin = f" from {dm.origin}" if dm.origin else ""
        report.add("ok", "dbt manifest", f"{_n(len(dm.nodes), 'model')}, {_n(len(dm.exposures), 'exposure')}{origin}")
    if schemas is not None:
        # Redshift folds unquoted names to lower case, dbt configs often don't.
        tables = {s.lower(): n for s, n in schemas.items()}.get(d.prod_schema.lower())
        if tables:
            report.add("ok", "Production schema", f"`{d.prod_schema}` has {_n(tables, 'table')}")
        else:
            report.add("warn", "Production schema",
                       f"no tables in `{d.prod_schema}` (dbt.prod_schema); `diff` and `verify` compare against it")
    if d.project_dir:
        if shutil.which("dbt"):
            report.add("ok", "dbt", "found, so `verify` can build models in dev")
        else:
            report.add("warn", "dbt", "not on PATH; `verify` needs it (pip install dbt-duckdb or dbt-redshift)")


def doctor(config: Path | None = None) -> DoctorReport:
    try:
        path = (config or find_config()).resolve()
    except FileNotFoundError as e:
        report = DoctorReport(None)
        report.add("fail", "Config", f"{e}. Create one with `lazarillo init`.")
        return report
    report = DoctorReport(path)
    try:
        cfg = load_config(path)
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as e:
        report.add("fail", "Config", f"cannot read it: {type(e).__name__}: {e}")
        return report
    report.add("ok", "Config", f"`{path}`")
    schemas = _warehouse(cfg, report)
    if cfg.dbt:
        _dbt(cfg, schemas, report)
    else:
        report.add("warn", "dbt", "no `dbt:` section, so map, describe, impact and verify are off")
    g = cfg.guardrails
    report.add("ok", "Guardrails", f"{g.max_rows} rows per query, PII masked in {', '.join(g.pii_columns) or 'no columns'}")
    return report
