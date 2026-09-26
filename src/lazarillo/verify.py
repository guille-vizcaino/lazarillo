"""Build a model in the dev schema and compare it with production before anyone merges.

The same loop answers two questions:
- "What does my change do?"       edit the model, verify: dev has the new code, prod the old.
- "Is my incremental drifting?"   verify without edits: dev is a full rebuild, prod is the
                                  result of every incremental run so far.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass

from .config import Config
from .context import DataMap, Exposure, Model
from .diff import DiffReport, diff
from .warehouse import open_warehouse


@dataclass
class VerifyReport:
    model: str
    prod_relation: str
    dev_relation: str
    dbt_ok: bool
    dbt_log: str
    diff: DiffReport | None
    downstream: list[Model]
    exposures: list[Exposure]

    def to_markdown(self) -> str:
        out = [f"## verify `{self.model}`", ""]
        if not self.dbt_ok:
            return "\n".join(out + ["❌ dbt build failed in the dev schema:", "```", self.dbt_log, "```"])
        out.append(f"Built `{self.dev_relation}` and compared it with `{self.prod_relation}`.")
        out.append("")
        out.append(self.diff.to_markdown())
        out += ["", "### Blast radius"]
        out.append(f"- downstream models: {', '.join(m.name for m in self.downstream) or '—'}")
        for e in self.exposures:
            out.append(f"- **{e.name}** ({e.type}, owner: {e.owner}){f' — {e.url}' if e.url else ''}")
        if not self.exposures:
            out.append("- no exposures depend on this model")
        verdict = (
            "✅ Safe: dev and prod match."
            if self.diff.identical
            else "⚠️ Dev and prod differ. Review the diff above before merging"
            + (f"; {len(self.exposures)} exposure(s) will see the change." if self.exposures else ".")
        )
        return "\n".join(out + ["", verdict])


def build_dev(cfg: Config, select: str) -> tuple[bool, str]:
    local = Path(sys.executable).parent / "dbt"  # same virtualenv as lazarillo
    dbt = str(local) if local.exists() else shutil.which("dbt")
    if not dbt:
        return False, "dbt executable not found on PATH"
    d = cfg.dbt
    cmd = [
        dbt, "build", "--select", select, "--target", d.dev_target,
        "--project-dir", str(d.project_dir),
        # Keep the dev manifest apart so the data map keeps describing production.
        "--target-path", "target_dev",
    ]
    profiles = d.profiles_dir or (d.project_dir if (d.project_dir / "profiles.yml").exists() else None)
    if profiles:
        cmd += ["--profiles-dir", str(profiles)]
    proc = subprocess.run(cmd, cwd=d.project_dir, capture_output=True, text=True)
    log = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, "\n".join(log[-25:])


def verify(cfg: Config, model: str, key: list[str] | None = None, where: str | None = None) -> VerifyReport:
    if not cfg.dbt or not cfg.dbt.project_dir:
        raise ValueError("verify builds locally: set dbt.project_dir in lazarillo.yml")
    dm = DataMap.from_config(cfg)
    m = dm.model(model)
    key = key or ([m.unique_key] if isinstance(m.unique_key, str) else m.unique_key)
    if not key:
        raise ValueError(f"{m.name} has no unique_key; pass --key")

    table = m.relation.split(".")[-1]
    prod, dev = f"{cfg.dbt.prod_schema}.{table}", f"{cfg.dbt.dev_schema}.{table}"
    downstream, exposures = dm.downstream(m.name)

    ok, log = build_dev(cfg, f"+{m.name}")
    report = VerifyReport(m.name, prod, dev, ok, log, None, downstream, exposures)
    if ok:
        with open_warehouse(cfg) as wh:
            report.diff = diff(wh, prod, dev, key=key, where=where)
    return report
