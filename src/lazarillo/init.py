"""`lazarillo init`: write a starter `lazarillo.yml` for an existing project."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .config import CONFIG_NAME

SKIP_DIRS = {".venv", "venv", "node_modules", "dbt_packages", "target", "target_dev", ".git"}

DBT_SECTION = """\
dbt:
  project_dir: {project_dir}
  prod_target: prod                   # profile target that builds production
  dev_target: dev                     # profile target `verify` builds into
  prod_schema: analytics
  dev_schema: dev
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
warehouse:                            # DuckDB database the agent reads (read-only)
  path: {warehouse}
# warehouse:                          # or Redshift, see docs/redshift.md
#   type: redshift
#   host: my-cluster.abc123.eu-west-1.redshift.amazonaws.com
#   database: analytics
#   user: lazarillo_ro                # password from PGPASSWORD or ~/.pgpass, or iam: true

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


def render_config(warehouse: str, dbt_project_dir: str | None) -> str:
    dbt = DBT_SECTION.format(project_dir=dbt_project_dir) if dbt_project_dir else DBT_PLACEHOLDER
    return TEMPLATE.format(warehouse=warehouse, dbt=dbt)


def _lazarillo_command() -> str:
    local = Path(sys.executable).parent / "lazarillo"  # same virtualenv as this process
    return str(local) if local.exists() else (shutil.which("lazarillo") or "lazarillo")


def init_project(directory: Path, warehouse: str, dbt_project: Path | None, force: bool) -> str:
    """Write lazarillo.yml into directory and return next steps as Markdown."""
    directory = directory.resolve()
    target = directory / CONFIG_NAME
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists; pass --force to overwrite it")

    dbt_dir = dbt_project.resolve() if dbt_project else find_dbt_project(directory)
    dbt_rel = Path(os.path.relpath(dbt_dir, directory)).as_posix() if dbt_dir else None
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(warehouse, dbt_rel))

    mcp = {"mcpServers": {"lazarillo": {"command": _lazarillo_command(), "args": ["-c", str(target), "mcp"]}}}
    warehouse_path = Path(warehouse) if Path(warehouse).is_absolute() else directory / warehouse
    out = [f"## Wrote `{target}`", ""]
    out.append(f"- warehouse: `{warehouse}`" + ("" if warehouse_path.exists() else " (not created yet)"))
    out.append(f"- dbt project: `{dbt_rel}`" if dbt_rel else "- dbt project: none found (map, describe and verify stay off)")
    out += [
        "",
        "### Next steps",
        "1. Review the file: attach extra databases, set PII columns and the row cap.",
        "2. Try it: `lazarillo query \"select 42\"`",
        "3. Give it to your agent, e.g. in `.mcp.json` for Claude Code:",
        "",
        "```json",
        json.dumps(mcp, indent=2),
        "```",
    ]
    return "\n".join(out)
