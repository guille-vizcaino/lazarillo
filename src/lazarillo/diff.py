"""Compare two relations row by row: the verification step of the harness."""

from __future__ import annotations

from dataclasses import dataclass, field

from .guardrails import MASK, check_identifier, check_read_only, is_pii
from .warehouse import Warehouse


@dataclass
class DiffReport:
    left: str
    right: str
    key: list[str]
    left_rows: int
    right_rows: int
    only_left: int
    only_right: int
    changed: int
    columns_only_left: list[str] = field(default_factory=list)
    columns_only_right: list[str] = field(default_factory=list)
    type_changes: dict[str, tuple[str, str]] = field(default_factory=dict)
    changed_by_column: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def identical(self) -> bool:
        return not (
            self.only_left or self.only_right or self.changed
            or self.columns_only_left or self.columns_only_right or self.type_changes
        )

    def to_dict(self) -> dict:
        return {**self.__dict__, "identical": self.identical}

    def to_markdown(self) -> str:
        out = [f"### Diff `{self.left}` → `{self.right}` (key: {', '.join(self.key)})", ""]
        if self.identical:
            out.append(f"✅ Identical: {self.left_rows:,} rows, same schema, same values.")
            return "\n".join(out)
        out += [
            "| | left | right |", "|---|---:|---:|",
            f"| rows | {self.left_rows:,} | {self.right_rows:,} |",
            f"| only here | {self.only_left:,} | {self.only_right:,} |",
            f"| changed (same key) | {self.changed:,} | |", "",
        ]
        if self.columns_only_left or self.columns_only_right or self.type_changes:
            out.append("**Schema drift**")
            out += [f"- `{c}` only in left" for c in self.columns_only_left]
            out += [f"- `{c}` only in right" for c in self.columns_only_right]
            out += [f"- `{c}`: {a} → {b}" for c, (a, b) in self.type_changes.items()]
            out.append("")
        if self.changed_by_column:
            out.append("**Changed values by column**")
            out += [f"- `{c}`: {n:,} rows" for c, n in self.changed_by_column.items()]
            out.append("")
        for title, rows in self.samples.items():
            if rows:
                cols = list(rows[0])
                out.append(f"**Sample: {title}**")
                out += ["", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
                out += ["| " + " | ".join("∅" if r[c] is None else str(r[c]) for c in cols) + " |" for r in rows]
                out.append("")
        return "\n".join(out).rstrip()


def _differs(c: str) -> str:
    """`l.c IS DISTINCT FROM r.c`, spelled so Redshift accepts it too."""
    return f"(l.{c} <> r.{c} OR (l.{c} IS NULL AND r.{c} IS NOT NULL) OR (l.{c} IS NOT NULL AND r.{c} IS NULL))"


def diff(
    wh: Warehouse,
    left_ref: str,
    right_ref: str,
    key: list[str],
    where: str | None = None,
    sample: int = 5,
) -> DiffReport:
    key = [check_identifier(k) for k in key]
    (left, lengine), (right, rengine) = wh.resolve(left_ref), wh.resolve(right_ref)
    if where:
        # `where` is agent-supplied, so run it through the same read-only check.
        check_read_only(f"SELECT 1 WHERE {where}")
        left = f"(SELECT * FROM {left} WHERE {where})"
        right = f"(SELECT * FROM {right} WHERE {where})"

    # Both sides in the same place: the diff runs there (e.g. entirely inside Redshift).
    # Otherwise the warehouse side, already filtered, is copied next to the lake table.
    engine = lengine
    if lengine is not rengine:
        left, right, engine = wh.localize(left, lengine), wh.localize(right, rengine), wh.local

    lcols, rcols = dict(engine.columns(left)), dict(engine.columns(right))
    common = [c for c in lcols if c in rcols and c not in key]
    pii = wh.cfg.guardrails.pii_columns

    report = DiffReport(
        left=left_ref, right=right_ref, key=key,
        left_rows=engine.scalar(f"SELECT count(*) FROM {left} AS _lz"),
        right_rows=engine.scalar(f"SELECT count(*) FROM {right} AS _lz"),
        only_left=0, only_right=0, changed=0,
        columns_only_left=[c for c in lcols if c not in rcols],
        columns_only_right=[c for c in rcols if c not in lcols],
        type_changes={c: (lcols[c], rcols[c]) for c in common if lcols[c] != rcols[c]},
    )

    on = " AND ".join(f"l.{k} = r.{k}" for k in key)
    keys = ", ".join(f"l.{k}" for k in key)
    rkeys = ", ".join(f"r.{k}" for k in key)
    anti = f"FROM {left} l WHERE NOT EXISTS (SELECT 1 FROM {right} r WHERE {on})"
    anti_r = f"FROM {right} r WHERE NOT EXISTS (SELECT 1 FROM {left} l WHERE {on})"
    report.only_left = engine.scalar(f"SELECT count(*) {anti}")
    report.only_right = engine.scalar(f"SELECT count(*) {anti_r}")

    def rows(sql: str) -> list[dict]:
        cols, data = engine.run(sql)
        return [
            {c: (MASK if is_pii(c, pii) and v is not None else v) for c, v in zip(cols, r)}
            for r in data
        ]

    report.samples["only in left"] = rows(f"SELECT {keys} {anti} ORDER BY {keys} LIMIT {sample}")
    report.samples["only in right"] = rows(f"SELECT {rkeys} {anti_r} ORDER BY {rkeys} LIMIT {sample}")

    if common:
        joined = f"FROM {left} l JOIN {right} r ON {on}"
        flags = ", ".join(f"count(CASE WHEN {_differs(c)} THEN 1 END) AS {c}" for c in common)
        any_diff = " OR ".join(_differs(c) for c in common)
        counts = engine.run(f"SELECT count(CASE WHEN {any_diff} THEN 1 END), {flags} {joined}")[1][0]
        report.changed = counts[0]
        report.changed_by_column = {c: n for c, n in zip(common, counts[1:]) if n}
        changed_cols = list(report.changed_by_column)
        if changed_cols:
            pairs = ", ".join(f"l.{c} AS {c}__left, r.{c} AS {c}__right" for c in changed_cols)
            report.samples["changed"] = rows(
                f"SELECT {keys}, {pairs} {joined} WHERE {any_diff} ORDER BY {keys} LIMIT {sample}"
            )
    return report
