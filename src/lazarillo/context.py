"""Compile a dbt manifest into the map an agent needs before touching anything."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Model:
    unique_id: str
    name: str
    relation: str
    materialized: str
    description: str = ""
    incremental_strategy: str | None = None
    unique_key: str | list[str] | None = None
    columns: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    raw_code: str = ""


@dataclass
class Exposure:
    unique_id: str
    name: str
    type: str
    owner: str
    url: str | None
    description: str
    depends_on: list[str]


class DataMap:
    def __init__(self, manifest: dict):
        self.nodes: dict[str, Model] = {}
        self.sources: dict[str, str] = {}
        self.exposures: dict[str, Exposure] = {}

        for uid, s in manifest.get("sources", {}).items():
            self.sources[uid] = f"{s['schema']}.{s['identifier']}"

        for uid, n in manifest.get("nodes", {}).items():
            if n["resource_type"] == "model":
                cfg = n.get("config", {})
                self.nodes[uid] = Model(
                    unique_id=uid,
                    name=n["name"],
                    relation=f"{n['schema']}.{n.get('alias') or n['name']}",
                    materialized=cfg.get("materialized", "view"),
                    description=n.get("description", ""),
                    incremental_strategy=cfg.get("incremental_strategy"),
                    unique_key=cfg.get("unique_key"),
                    columns={c: v.get("description", "") for c, v in n.get("columns", {}).items()},
                    depends_on=n.get("depends_on", {}).get("nodes", []),
                    raw_code=n.get("raw_code", ""),
                )
        for n in manifest.get("nodes", {}).values():
            if n["resource_type"] == "test":
                for dep in n.get("depends_on", {}).get("nodes", []):
                    if dep in self.nodes:
                        meta = n.get("test_metadata") or {}
                        col = (meta.get("kwargs") or {}).get("column_name")
                        self.nodes[dep].tests.append(f"{meta.get('name', n['name'])}({col or ''})")

        for uid, e in manifest.get("exposures", {}).items():
            owner = e.get("owner") or {}
            self.exposures[uid] = Exposure(
                unique_id=uid, name=e.get("label") or e["name"], type=e.get("type", ""),
                owner=owner.get("name") or owner.get("email") or "",
                url=e.get("url"), description=e.get("description", ""),
                depends_on=e.get("depends_on", {}).get("nodes", []),
            )

    @classmethod
    def from_project(cls, project_dir: Path) -> "DataMap":
        path = project_dir / "target" / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found: run `dbt parse` (or `dbt build`) first")
        return cls(json.loads(path.read_text()))

    def model(self, name: str) -> Model:
        for m in self.nodes.values():
            if name in (m.name, m.unique_id, m.relation):
                return m
        raise KeyError(f"Unknown model {name!r}. Known: {', '.join(sorted(m.name for m in self.nodes.values()))}")

    def _label(self, uid: str) -> str:
        if uid in self.nodes:
            return self.nodes[uid].name
        return self.sources.get(uid, uid)

    def downstream(self, name: str) -> tuple[list[Model], list[Exposure]]:
        """Everything that breaks if `name` changes: models (in build order) and exposures."""
        start = self.model(name).unique_id
        seen, frontier, order = {start}, [start], []
        while frontier:
            current = frontier.pop(0)
            for m in self.nodes.values():
                if current in m.depends_on and m.unique_id not in seen:
                    seen.add(m.unique_id)
                    order.append(m)
                    frontier.append(m.unique_id)
        exposures = [e for e in self.exposures.values() if seen & set(e.depends_on)]
        return order, exposures

    def describe(self, name: str) -> str:
        m = self.model(name)
        lines = [f"## {m.name}", "", f"- relation: `{m.relation}`", f"- materialized: {m.materialized}"]
        if m.materialized == "incremental":
            lines.append(f"- incremental: strategy={m.incremental_strategy or 'default'}, unique_key={m.unique_key}")
        if m.description:
            lines.append(f"- description: {m.description}")
        lines.append(f"- upstream: {', '.join(self._label(d) for d in m.depends_on) or '—'}")
        models, exposures = self.downstream(name)
        lines.append(f"- downstream models: {', '.join(d.name for d in models) or '—'}")
        lines.append(f"- exposures: {', '.join(f'{e.name} ({e.type})' for e in exposures) or '—'}")
        if m.tests:
            lines.append(f"- tests: {', '.join(m.tests)}")
        if m.columns:
            lines += ["", "| column | description |", "|---|---|"]
            lines += [f"| {c} | {d} |" for c, d in m.columns.items()]
        if m.raw_code:
            lines += ["", "```sql", m.raw_code.strip(), "```"]
        return "\n".join(lines)

    def to_markdown(self) -> str:
        """The compact map handed to the agent at the start of a session."""
        lines = ["# Data map", "", "## Sources (landing)", ""]
        lines += [f"- `{r}`" for r in sorted(self.sources.values())]
        lines += ["", "## Models", "", "| model | relation | materialized | upstream |", "|---|---|---|---|"]
        for m in sorted(self.nodes.values(), key=lambda m: m.name):
            mat = m.materialized
            if mat == "incremental":
                mat += f" ({m.incremental_strategy or 'default'}, key={m.unique_key})"
            ups = ", ".join(self._label(d) for d in m.depends_on)
            lines.append(f"| {m.name} | `{m.relation}` | {mat} | {ups} |")
        if self.exposures:
            lines += ["", "## Exposures (who consumes this)", ""]
            for e in self.exposures.values():
                deps = ", ".join(self._label(d) for d in e.depends_on)
                lines.append(f"- **{e.name}** ({e.type}, owner: {e.owner}) ← {deps}")
        return "\n".join(lines)
