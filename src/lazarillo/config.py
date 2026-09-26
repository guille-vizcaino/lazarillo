"""Load `lazarillo.yml`. Every relative path is resolved against the config file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_NAME = "lazarillo.yml"


@dataclass
class Guardrails:
    max_rows: int = 200
    pii_columns: list[str] = field(default_factory=lambda: ["email", "name", "phone"])


@dataclass
class DbtConfig:
    project_dir: Path
    prod_target: str = "prod"
    dev_target: str = "dev"
    prod_schema: str = "analytics"
    dev_schema: str = "dev"


@dataclass
class Config:
    root: Path
    warehouse: Path
    attach: dict[str, Path] = field(default_factory=dict)
    dbt: DbtConfig | None = None
    iceberg_catalog: dict[str, str] | None = None
    guardrails: Guardrails = field(default_factory=Guardrails)

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p).resolve()


def find_config(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        if (d / CONFIG_NAME).exists():
            return d / CONFIG_NAME
    raise FileNotFoundError(f"No {CONFIG_NAME} found in {here} or its parents")


def load_config(path: Path | None = None) -> Config:
    path = (path or find_config()).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    root = path.parent

    def resolve(v: str) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (root / p).resolve()

    cfg = Config(root=root, warehouse=resolve(raw["warehouse"]["path"]))
    cfg.attach = {name: resolve(p) for name, p in (raw.get("attach") or {}).items()}

    if dbt := raw.get("dbt"):
        cfg.dbt = DbtConfig(
            project_dir=resolve(dbt["project_dir"]),
            **{k: v for k, v in dbt.items() if k != "project_dir"},
        )

    if ice := (raw.get("landing") or {}).get("iceberg_catalog"):
        cfg.iceberg_catalog = {
            k: (f"sqlite:///{resolve(v.removeprefix('sqlite:///'))}" if k == "uri" else
                f"file://{resolve(v.removeprefix('file://'))}" if k == "warehouse" else v)
            for k, v in ice.items()
        }

    if g := raw.get("guardrails"):
        cfg.guardrails = Guardrails(**g)
    return cfg
