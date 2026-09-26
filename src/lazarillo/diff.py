"""Compare two relations row by row: the verification step of the harness."""

from __future__ import annotations

from dataclasses import dataclass, field

from .guardrails import MASK, check_identifier, is_pii
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


def diff(
    wh: Warehouse,
    left_ref: str,
    right_ref: str,
    key: list[str],
    where: str | None = None,
    sample: int = 5,
) -> DiffReport:
    key = [check_identifier(k) for k in key]
    left, right = wh.relation(left_ref), wh.relation(right_ref)
    if where:
        # `where` is agent-supplied, so run it through the same read-only check.
        from .guardrails import check_read_only

        check_read_only(f"SELECT 1 WHERE {where}")
        left = f"(SELECT * FROM {left} WHERE {where})"
        right = f"(SELECT * FROM {right} WHERE {where})"

    lcols, rcols = dict(wh.columns(left)), dict(wh.columns(right))
    common = [c for c in lcols if c in rcols and c not in key]
    pii = wh.cfg.guardrails.pii_columns

    q = wh.con.execute
    report = DiffReport(
        left=left_ref, right=right_ref, key=key,
        left_rows=q(f"SELECT count(*) FROM {left}").fetchone()[0],
        right_rows=q(f"SELECT count(*) FROM {right}").fetchone()[0],
        only_left=0, only_right=0, changed=0,
        columns_only_left=[c for c in lcols if c not in rcols],
        columns_only_right=[c for c in rcols if c not in lcols],
        type_changes={c: (lcols[c], rcols[c]) for c in common if lcols[c] != rcols[c]},
    )

    on = " AND ".join(f"l.{k} = r.{k}" for k in key)
    keys = ", ".join(f"l.{k}" for k in key)
    rkeys = ", ".join(f"r.{k}" for k in key)
    anti = f"FROM {left} l ANTI JOIN {right} r ON {on}"
    anti_r = f"FROM {right} r ANTI JOIN {left} l ON {on}"
    report.only_left = q(f"SELECT count(*) {anti}").fetchone()[0]
    report.only_right = q(f"SELECT count(*) {anti_r}").fetchone()[0]

    def rows(sql: str) -> list[dict]:
        cur = q(sql)
        cols = [d[0] for d in cur.description]
        return [
            {c: (MASK if is_pii(c, pii) and v is not None else v) for c, v in zip(cols, r)}
            for r in cur.fetchall()
        ]

    report.samples["only in left"] = rows(f"SELECT {keys} {anti} ORDER BY {keys} LIMIT {sample}")
    report.samples["only in right"] = rows(f"SELECT {rkeys} {anti_r} ORDER BY {rkeys} LIMIT {sample}")

    if common:
        joined = f"FROM {left} l JOIN {right} r ON {on}"
        flags = ", ".join(f"count(*) FILTER (l.{c} IS DISTINCT FROM r.{c}) AS {c}" for c in common)
        any_diff = " OR ".join(f"l.{c} IS DISTINCT FROM r.{c}" for c in common)
        counts = q(f"SELECT count(*) FILTER ({any_diff}), {flags} {joined}").fetchone()
        report.changed = counts[0]
        report.changed_by_column = {c: n for c, n in zip(common, counts[1:]) if n}
        changed_cols = list(report.changed_by_column)
        if changed_cols:
            pairs = ", ".join(f"l.{c} AS {c}__left, r.{c} AS {c}__right" for c in changed_cols)
            report.samples["changed"] = rows(
                f"SELECT {keys}, {pairs} {joined} WHERE {any_diff} ORDER BY {keys} LIMIT {sample}"
            )
    return report
