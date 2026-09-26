"""`lazarillo` command line. Every command prints Markdown so humans and agents read the same thing."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import click
import duckdb

from . import __version__
from .config import load_config
from .context import DataMap
from .dbt_cloud import DbtCloudError
from .diff import diff as run_diff
from .guardrails import GuardrailViolation
from .engine import WarehouseError
from .lake import LakeError
from .warehouse import open_warehouse


def _cfg(ctx: click.Context):
    return load_config(ctx.obj.get("config"))


@contextmanager
def _warehouse(ctx: click.Context):
    try:
        with open_warehouse(_cfg(ctx)) as wh:
            yield wh
    except (FileNotFoundError, LakeError, WarehouseError) as e:
        raise click.ClickException(str(e))


def _datamap(ctx: click.Context) -> DataMap:
    cfg = _cfg(ctx)
    if not cfg.dbt:
        raise click.UsageError("No `dbt:` section in lazarillo.yml")
    try:
        return DataMap.from_config(cfg)
    except (FileNotFoundError, DbtCloudError) as e:
        raise click.ClickException(str(e))


@click.group()
@click.version_option(__version__)
@click.option("--config", "-c", type=click.Path(path_type=Path, exists=True), help="Path to lazarillo.yml")
@click.pass_context
def main(ctx: click.Context, config: Path | None) -> None:
    """Lazarillo guides AI agents through your lakehouse: context, guardrails, verification."""
    ctx.obj = {"config": config}


@main.command()
@click.argument("directory", type=click.Path(path_type=Path, file_okay=False), default=".")
@click.option("--warehouse", "-w", help="duckdb or redshift (a path to a DuckDB file works too). "
              "Default: the prod target of the dbt profile, else you are asked")
@click.option("--path", "duckdb_path", help="DuckDB file, relative to DIRECTORY")
@click.option("--host", help="Redshift host")
@click.option("--database", help="Redshift database")
@click.option("--port", type=int, help="Redshift port (default 5439)")
@click.option("--user", help="Redshift user")
@click.option("--iam/--no-iam", default=None, help="Sign in to Redshift with IAM instead of a password")
@click.option("--cluster", "cluster_identifier", help="Provisioned cluster identifier, for IAM")
@click.option("--workgroup", help="Serverless workgroup, for IAM")
@click.option("--aws-profile", "profile", help="AWS profile for IAM (default: the AWS default chain)")
@click.option("--password-env", help="Env var holding the Redshift password (default: PGPASSWORD or ~/.pgpass)")
@click.option("--dbt-project", type=click.Path(path_type=Path, file_okay=False, exists=True),
              help="dbt project dir (default: the first dbt_project.yml found under DIRECTORY)")
@click.option("--mcp", "mcp", multiple=True, type=click.Choice(["claude-code", "cursor", "vscode", "none"]),
              help="Register the MCP server for this agent (repeatable). Default: ask in a terminal, "
              "else just print the config. With an existing lazarillo.yml, only this is done")
@click.option("--no-input", is_flag=True, help="Never ask; fail if a warehouse setting is missing")
@click.option("--force", is_flag=True, help="Overwrite an existing lazarillo.yml")
def init(directory, warehouse, duckdb_path, dbt_project, mcp, no_input, force, **redshift):
    """Write a starter lazarillo.yml for your project.

    \b
    The warehouse comes from the flags, then from the production target in dbt's
    profiles.yml, then from a few questions. Passwords are never written to the file.
    Then it registers the MCP server for your agent: Claude Code, Cursor or VS Code.
    """
    from .init import init_project

    given = {k: v for k, v in redshift.items() if v is not None}
    if warehouse in ("duckdb", "redshift"):
        given["type"] = warehouse
    elif warehouse:
        given["path"] = warehouse
    if duckdb_path:
        given["path"] = duckdb_path
    if "type" not in given and "path" not in given and given:
        given["type"] = "redshift"
    ask = None if no_input or not _interactive() else _ask
    try:
        clients = [c for c in mcp if c != "none"] if mcp else None
        click.echo(init_project(directory, given, dbt_project, force, prompt=ask, mcp=clients))
    except (FileExistsError, ValueError) as e:
        raise click.ClickException(str(e))


def _interactive() -> bool:
    return sys.stdin.isatty()


def _ask(text: str, default: str | None = None, choices: list[str] | None = None) -> str:
    kind = click.Choice(choices) if choices else None
    return click.prompt(text, default=default, type=kind, show_default=bool(default))


@main.command()
@click.pass_context
def doctor(ctx):
    """Check the config, the connection, the read-only guard and the dbt manifest."""
    from .doctor import doctor as run_doctor

    report = run_doctor(ctx.obj.get("config"))
    click.echo(report.to_markdown())
    if not report.ok:
        ctx.exit(1)


@main.command("map")
@click.pass_context
def map_(ctx):
    """Print the data map: sources, models, materializations and exposures."""
    click.echo(_datamap(ctx).to_markdown())


@main.command()
@click.argument("model")
@click.pass_context
def describe(ctx, model):
    """Everything about one model: columns, tests, code, upstream and downstream."""
    click.echo(_datamap(ctx).describe(model))


@main.command()
@click.argument("model")
@click.pass_context
def impact(ctx, model):
    """Which models and dashboards are affected if MODEL changes."""
    models, exposures = _datamap(ctx).downstream(model)
    click.echo(f"## Impact of `{model}`\n")
    click.echo("Models: " + (", ".join(m.name for m in models) or "—"))
    for e in exposures:
        click.echo(f"- {e.name} ({e.type}, owner: {e.owner})")


@main.command()
@click.argument("sql")
@click.option("--max-rows", type=int)
@click.pass_context
def query(ctx, sql, max_rows):
    """Run one read-only SQL statement (row-capped, PII masked)."""
    with _warehouse(ctx) as wh:
        try:
            click.echo(wh.query(sql, max_rows).to_markdown())
        except GuardrailViolation as e:
            raise click.ClickException(f"Guardrail: {e}")
        except (duckdb.Error, WarehouseError) as e:
            raise click.ClickException(str(e))


@main.command()
@click.argument("left")
@click.argument("right")
@click.option("--key", "-k", required=True, multiple=True, help="Key column(s); repeat for composite keys")
@click.option("--where", help="Restrict both sides, e.g. \"ordered_at >= '2026-09-01'\"")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def diff(ctx, left, right, key, where, as_json):
    """Compare two relations: source vs landing, Delta vs Iceberg, prod vs dev...

    \b
    lazarillo diff src.orders landing.orders -k order_id
    lazarillo diff delta:data/landing/delta/orders iceberg:landing.orders -k order_id
    lazarillo diff delta:s3://my-lake/landing/orders iceberg:landing.orders -k order_id
    """
    with _warehouse(ctx) as wh:
        try:
            report = run_diff(wh, left, right, list(key), where)
        except GuardrailViolation as e:
            raise click.ClickException(f"Guardrail: {e}")
        except (LakeError, duckdb.Error, WarehouseError) as e:
            raise click.ClickException(str(e))
    click.echo(json.dumps(report.to_dict(), default=str, indent=2) if as_json else report.to_markdown())


@main.command()
@click.argument("model")
@click.option("--key", "-k", multiple=True, help="Defaults to the model's unique_key")
@click.option("--where", help="Only compare a window, e.g. the last 7 days")
@click.pass_context
def verify(ctx, model, key, where):
    """Build MODEL in the dev schema and diff it against production."""
    from .verify import verify as run_verify

    try:
        report = run_verify(_cfg(ctx), model, list(key) or None, where)
    except WarehouseError as e:
        raise click.ClickException(str(e))
    click.echo(report.to_markdown())
    if not report.dbt_ok:
        ctx.exit(1)


@main.command()
@click.pass_context
def mcp(ctx):
    """Serve the harness over MCP (stdio) for Claude Code, Cursor and friends."""
    from .mcp_server import build_server

    build_server(_cfg(ctx), ctx.obj.get("config")).run("stdio")


@main.command()
def checkride():
    """Evaluate how well an agent does real data-engineering tasks (coming soon)."""
    click.echo(
        "checkride is on the roadmap: a benchmark of data tasks (fix a drifting incremental,\n"
        "explain a revenue drop, validate a Delta → Iceberg migration) scored with and without\n"
        "the harness. See docs/checkride.md."
    )
