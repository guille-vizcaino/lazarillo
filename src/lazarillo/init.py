"""`lazarillo init`: write a starter `lazarillo.yml` for an existing project.

The warehouse comes from, in order: the flags, the production target in dbt's
profiles.yml, and questions for whatever is still missing. Secrets never go in the file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

import yaml

from .config import CONFIG_NAME
from .profiles import DbtTarget, read_target, workgroup_from_host

SKIP_DIRS = {".venv", "venv", "node_modules", "dbt_packages", "target", "target_dev", ".git"}

# A prompt asks one question: prompt(text, default=None, choices=None) -> answer.
Prompt = Callable[..., str]

WAREHOUSE_KEYS = ("type", "path", "host", "port", "database", "user", "iam", "cluster_identifier",
                  "workgroup", "profile", "region", "password_env")

DUCKDB_SECTION = """\
warehouse:                            # DuckDB database the agent reads (read-only)
{fields}
# warehouse:                          # or Redshift, see docs/redshift.md
#   type: redshift
#   host: my-cluster.abc123.eu-west-1.redshift.amazonaws.com
#   database: analytics
#   user: lazarillo_ro                # password from PGPASSWORD or ~/.pgpass, or iam: true
"""

REDSHIFT_SECTION = """\
warehouse:                            # Redshift, one read-only transaction per session
{fields}
{auth}"""

DBT_SECTION = """\
dbt:
  project_dir: {project_dir}
{targets}
  # profiles_dir: ~/.dbt              # default: the project dir if it has profiles.yml, else dbt's own
  # cloud:                            # read the production manifest from a dbt Cloud job
  #   account_id: 12345
  #   job_id: 67890                   # the job that builds production
  #   host: cloud.getdbt.com          # or emea.dbt.com, au.dbt.com, ACCOUNT_PREFIX.us1.dbt.com
  #   token_env: DBT_CLOUD_API_TOKEN  # env var holding the token; never put it here
"""

DBT_PLACEHOLDER = """\
# dbt:                                # enables map, describe, impact and verify
#   project_dir: path/to/dbt_project
#   prod_schema: analytics
#   dev_schema: dev
"""

TEMPLATE = """\
# Lazarillo config. Relative paths are resolved against this file.
{warehouse}
# attach:                             # extra DuckDB files, queried as <name>.<table>
#   src: data/source.duckdb

# landing:
#   iceberg_catalog:                  # enables iceberg:<namespace.table> refs
#     name: lake
#     uri: sqlite:///data/iceberg/catalog.db
#     warehouse: file://data/iceberg

{dbt}
guardrails:
  max_rows: 200                       # rows returned per query
  pii_columns: [email, name, phone]   # masked in every result
"""


def find_dbt_project(root: Path) -> Path | None:
    """The shallowest dbt_project.yml under root, looking at most two levels down."""
    for pattern in ("dbt_project.yml", "*/dbt_project.yml", "*/*/dbt_project.yml"):
        for hit in sorted(root.glob(pattern)):
            if not SKIP_DIRS & set(hit.relative_to(root).parts):
                return hit.parent
    return None


def _kind(wh: dict) -> str:
    return wh.get("type") or "duckdb"


def _clean(wh: dict) -> dict:
    """Drop empty values and the implicit `type: duckdb`, in a stable key order."""
    out = {k: wh[k] for k in WAREHOUSE_KEYS if wh.get(k) not in (None, "", False)}
    if out.get("type") == "duckdb":
        del out["type"]
    return out


def missing(wh: dict) -> list[str]:
    """Settings the warehouse cannot work without."""
    if _kind(wh) == "duckdb":
        return [] if wh.get("path") else ["path"]
    need = [k for k in ("host", "database") if not wh.get(k)]
    if wh.get("iam") and not (wh.get("cluster_identifier") or wh.get("workgroup")):
        need.append("cluster_identifier")
    if not wh.get("iam") and not wh.get("user"):
        need.append("user")
    return need


def ask_warehouse(wh: dict, prompt: Prompt) -> dict:
    """Ask for what is missing, and only that. Passwords are never asked for."""
    wh = dict(wh)
    if not wh.get("type") and not wh.get("path"):
        wh["type"] = prompt("Warehouse", default="duckdb", choices=["duckdb", "redshift"])
    if _kind(wh) == "duckdb":
        if not wh.get("path"):
            wh["path"] = prompt("DuckDB file", default="warehouse.duckdb")
        return wh

    if not wh.get("host"):
        wh["host"] = prompt("Redshift host (e.g. my-cluster.abc123.eu-west-1.redshift.amazonaws.com)")
    if not wh.get("database"):
        wh["database"] = prompt("Database")
    if "iam" not in wh and not wh.get("password_env"):
        wh["iam"] = prompt("Sign in with", default="iam", choices=["iam", "password"]) == "iam"
    if wh.get("iam"):
        if not (wh.get("cluster_identifier") or wh.get("workgroup")):
            if workgroup := workgroup_from_host(wh["host"]):
                wh["workgroup"] = workgroup
            else:
                wh["cluster_identifier"] = prompt("Cluster identifier", default=wh["host"].split(".")[0])
        if "profile" not in wh:
            wh["profile"] = prompt("AWS profile (blank for the default chain)", default="")
    else:
        if not wh.get("user"):
            wh["user"] = prompt("Database user")
        if "password_env" not in wh:
            wh["password_env"] = prompt("Env var holding the password (blank for PGPASSWORD or ~/.pgpass)",
                                        default="")
    return wh


def _yaml(value) -> str:
    return yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).splitlines()[0]


def _commented(line: str, comment: str) -> str:
    return f"{line.ljust(37)} # {comment}"


def render_config(warehouse: dict | str, dbt: dict | None = None) -> str:
    wh = _clean({"path": warehouse} if isinstance(warehouse, str) else warehouse)
    fields = "\n".join(f"  {k}: {_yaml(v)}" for k, v in wh.items())
    if _kind(wh) == "duckdb":
        section = DUCKDB_SECTION.format(fields=fields)
    else:
        auth = ("  # no password stored: a temporary one comes from the Redshift API through IAM\n" if wh.get("iam")
                else "" if wh.get("password_env")
                else "  # password: from PGPASSWORD or ~/.pgpass, or set password_env: NAME_OF_ENV_VAR\n")
        section = REDSHIFT_SECTION.format(fields=fields, auth=auth)
    if dbt:
        targets = "\n".join([
            _commented(f"  prod_target: {_yaml(dbt['prod_target'])}", "profile target that builds production"),
            _commented(f"  dev_target: {_yaml(dbt['dev_target'])}", "profile target `verify` builds into"),
            f"  prod_schema: {_yaml(dbt['prod_schema'])}",
            f"  dev_schema: {_yaml(dbt['dev_schema'])}",
        ])
        dbt_text = DBT_SECTION.format(project_dir=_yaml(dbt["project_dir"]), targets=targets)
    else:
        dbt_text = DBT_PLACEHOLDER
    return TEMPLATE.format(warehouse=section, dbt=dbt_text)


def _lazarillo_command() -> str:
    local = Path(sys.executable).parent / "lazarillo"  # same virtualenv as this process
    return str(local) if local.exists() else (shutil.which("lazarillo") or "lazarillo")


def _describe(wh: dict, directory: Path) -> str:
    if _kind(wh) == "duckdb":
        path = Path(wh["path"]) if Path(wh["path"]).is_absolute() else directory / wh["path"]
        return f"DuckDB `{wh['path']}`" + ("" if path.exists() else " (not created yet)")
    if wh.get("iam"):
        target = f"workgroup `{wh['workgroup']}`" if wh.get("workgroup") else f"cluster `{wh['cluster_identifier']}`"
        auth = f"IAM ({target}" + (f", AWS profile `{wh['profile']}`" if wh.get("profile") else "") + ")"
    else:
        source = f"`${wh['password_env']}`" if wh.get("password_env") else "PGPASSWORD or ~/.pgpass"
        auth = f"user `{wh['user']}`, password from {source}"
    return f"Redshift `{wh['host']}`, database `{wh['database']}`, {auth}"


def init_project(directory: Path, warehouse: dict | str | None = None, dbt_project: Path | None = None,
                 force: bool = False, prompt: Prompt | None = None) -> str:
    """Write lazarillo.yml into directory and return what was found and next steps as Markdown.

    `warehouse` holds the settings given as flags (a string is a DuckDB path). Whatever
    they leave out comes from the dbt profile, then from `prompt` if there is one.
    """
    directory = directory.resolve()
    target = directory / CONFIG_NAME
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists; pass --force to overwrite it")

    given = {"path": warehouse} if isinstance(warehouse, str) else dict(warehouse or {})
    given = {k: v for k, v in given.items() if v not in (None, "")}
    dbt_dir = dbt_project.resolve() if dbt_project else find_dbt_project(directory)
    found: DbtTarget | None = read_target(dbt_dir) if dbt_dir else None

    wh = given
    from_profile = bool(found and found.warehouse and (not given or _kind(given) == _kind(found.warehouse)))
    if from_profile:
        wh = {**found.warehouse, **given}
    if (missing(wh) or not wh) and prompt:
        wh = ask_warehouse(wh, prompt)
    if not wh:
        wh = {"path": "warehouse.duckdb"}
    if gaps := missing(wh):
        flags = ", ".join("--" + g.replace("_identifier", "").replace("_", "-") for g in gaps)
        raise ValueError(f"The warehouse is missing {', '.join(gaps)}. Pass {flags}, or run init in a terminal to be asked.")
    if _kind(wh) == "duckdb" and Path(wh["path"]).is_absolute():
        wh["path"] = Path(os.path.relpath(wh["path"], directory)).as_posix()

    dbt = None
    if dbt_dir:
        dbt = {
            "project_dir": Path(os.path.relpath(dbt_dir, directory)).as_posix(),
            "prod_target": found.target if found else "prod",
            "dev_target": (found.dev_target if found and found.dev_target else "dev"),
            "prod_schema": (found.schema if found and found.schema else "analytics"),
            "dev_schema": (found.dev_schema if found and found.dev_schema else "dev"),
        }
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(wh, dbt))

    mcp = {"mcpServers": {"lazarillo": {"command": _lazarillo_command(), "args": ["-c", str(target), "mcp"]}}}
    out = [f"## Wrote `{target}`", "", f"- warehouse: {_describe(wh, directory)}"]
    if from_profile:
        out.append(f"- read from `{found.profiles_path}` (profile `{found.profile}`, target `{found.target}`)")
    out.append(f"- dbt project: `{dbt['project_dir']}`" if dbt else "- dbt project: none found (map, describe and verify stay off)")
    out += [f"- note: {n}" for n in (found.notes if found else [])]
    out += [
        "",
        "### Next steps",
        "1. Check the connection, the read-only guard and the dbt manifest: `lazarillo doctor`",
        "2. Review the file: attach extra databases, set PII columns and the row cap.",
        "3. Give it to your agent, e.g. in `.mcp.json` for Claude Code:",
        "",
        "```json",
        json.dumps(mcp, indent=2),
        "```",
    ]
    return "\n".join(out)
