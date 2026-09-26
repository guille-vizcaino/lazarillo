"""Read the warehouse connection from dbt's profiles.yml, so `init` asks as little as possible.

Only what Lazarillo needs is copied, and never a secret: a password written as
`{{ env_var('NAME') }}` becomes `password_env: NAME`, and a password written in the file
is left there. dbt Cloud keeps connections in its own UI, so there is nothing to read.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ENV_VAR = re.compile(r"""^\s*\{\{\s*env_var\(\s*['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]*)['"]\s*)?\)\s*\}\}\s*$""")
_SERVERLESS = re.compile(r"^([^.]+)\.\d+\.[a-z0-9-]+\.redshift-serverless\.amazonaws\.com$")


@dataclass
class DbtTarget:
    """The dbt target Lazarillo should read, translated into a `warehouse:` block."""

    profiles_path: Path
    profile: str
    target: str
    type: str
    # The `warehouse:` block for lazarillo.yml (possibly incomplete), or None if the
    # adapter is not one Lazarillo reads.
    warehouse: dict | None
    schema: str | None = None
    dev_target: str | None = None
    dev_schema: str | None = None
    notes: list[str] = field(default_factory=list)


def workgroup_from_host(host: str) -> str | None:
    """`analytics.123456789012.eu-west-1.redshift-serverless.amazonaws.com` -> `analytics`."""
    m = _SERVERLESS.match(host or "")
    return m.group(1) if m else None


def find_profiles(project_dir: Path, profiles_dir: Path | None = None) -> Path | None:
    """Where dbt would look: --profiles-dir, the project dir, DBT_PROFILES_DIR, ~/.dbt."""
    dirs = [profiles_dir] if profiles_dir else [project_dir, os.environ.get("DBT_PROFILES_DIR"), Path.home() / ".dbt"]
    for d in dirs:
        if d and (p := Path(d).expanduser() / "profiles.yml").is_file():
            return p
    return None


def _value(raw) -> tuple[object, str | None]:
    """A profile value and, for `{{ env_var('NAME') }}`, the variable's name.

    Other Jinja is left unresolved (None), so `init` asks for it instead of guessing.
    """
    if not isinstance(raw, str):
        return raw, None
    if m := _ENV_VAR.match(raw):
        return os.environ.get(m.group(1), m.group(2)), m.group(1)
    return (None, None) if "{{" in raw else (raw, None)


def _redshift(out: dict, notes: list[str]) -> dict:
    host = _value(out.get("host"))[0]
    port = _value(out.get("port"))[0]
    wh = {
        "type": "redshift",
        "host": host,
        "port": port if isinstance(port, int) and port != 5439 else None,
        "database": _value(out.get("dbname") or out.get("database"))[0],
        "user": _value(out.get("user"))[0],
    }
    if out.get("method") == "iam":
        wh["iam"] = True
        if workgroup := workgroup_from_host(host):
            wh["workgroup"] = workgroup
        else:
            wh["cluster_identifier"] = _value(out.get("cluster_id"))[0]
        wh["profile"] = _value(out.get("iam_profile"))[0]
        wh["region"] = _value(out.get("region"))[0]
    else:
        wh["iam"] = False
        _, env = _value(out.get("password"))
        if env:
            wh["password_env"] = env
        elif out.get("password"):
            notes.append("profiles.yml has the password written in; it was not copied. "
                         "Lazarillo reads it from PGPASSWORD, ~/.pgpass or `password_env`.")
    # `iam: False` stays, so `init` knows how to sign in without asking.
    return {k: v for k, v in wh.items() if v not in (None, "")}


def _duckdb(out: dict, project_dir: Path, notes: list[str]) -> dict | None:
    path = _value(out.get("path"))[0]
    if not path or path == ":memory:" or str(path).startswith("md:"):
        notes.append(f"The dbt target's DuckDB path is {path or 'missing'!s}; Lazarillo needs a database file.")
        return None
    p = Path(str(path)).expanduser()
    # dbt runs from the project dir, so that is what a relative path is relative to.
    return {"path": str(p if p.is_absolute() else (project_dir / p).resolve())}


def read_target(project_dir: Path, profiles_dir: Path | None = None, prefer: str = "prod",
                dev: str = "dev") -> DbtTarget | None:
    """The production target of the project's dbt profile, or None if there is no profile."""
    project_dir = Path(project_dir).resolve()
    try:
        project = yaml.safe_load((project_dir / "dbt_project.yml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    path = find_profiles(project_dir, profiles_dir)
    if not path or not (name := project.get("profile")):
        return None
    try:
        profile = (yaml.safe_load(path.read_text()) or {}).get(name)
    except yaml.YAMLError:
        return None
    outputs = (profile or {}).get("outputs") or {}
    if not outputs:
        return None

    # Lazarillo reads production, so the `prod` target wins over the profile's default.
    default = _value(profile.get("target"))[0]
    target = prefer if prefer in outputs else default if default in outputs else next(iter(outputs))
    out = outputs[target] or {}
    kind = str(_value(out.get("type"))[0] or "").lower()
    notes: list[str] = []
    if kind == "redshift":
        warehouse = _redshift(out, notes)
    elif kind == "duckdb":
        warehouse = _duckdb(out, project_dir, notes)
    else:
        warehouse = None
        notes.append(f"The dbt target `{target}` uses {kind or 'an unknown adapter'}; "
                     "Lazarillo reads DuckDB and Redshift.")
    dev_out = outputs.get(dev) if dev != target else None
    return DbtTarget(
        profiles_path=path, profile=name, target=target, type=kind, warehouse=warehouse,
        schema=_value(out.get("schema"))[0],
        dev_target=dev if dev_out else None,
        dev_schema=_value((dev_out or {}).get("schema"))[0],
        notes=notes,
    )
