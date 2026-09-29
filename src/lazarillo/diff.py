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
    # Key values that repeat on each side. Any repeat means the key is not a row key (e.g. the
    # date of a delete+insert model), so whole rows are compared instead of rows by key.
    repeated_keys_left: int = 0
    repeated_keys_right: int = 0
    differing_keys: int = 0

    @property
    def by_key(self) -> bool:
        return not (self.repeated_keys_left or self.repeated_keys_right)

    @property
    def identical(self) -> bool:
        return not (
            self.only_left or self.only_right or self.changed
            or self.columns_only_left or self.columns_only_right or self.type_changes
        )

    def to_dict(self) -> dict:
        return {**self.__dict__, "identical": self.identical, "by_key": self.by_key}

    def to_markdown(self) -> str:
        key = ", ".join(self.key)
        out = [f"### Diff `{self.left}` → `{self.right}` (key: {key})", ""]
        if not self.by_key:
            out += [
                f"`{key}` doesn't identify a row ({self.repeated_keys_left:,} values repeat in left, "
                f"{self.repeated_keys_right:,} in right), so whole rows were compared. "
                "Pass the row grain as the key to see which columns changed.", "",
            ]
        if self.identical:
            out.append(f"✅ Identical: {self.left_rows:,} rows, same schema, same values.")
            return "\n".join(out)
        out += [
            "| | left | right |", "|---|---:|---:|",
            f"| rows | {self.left_rows:,} | {self.right_rows:,} |",
            f"| only here | {self.only_left:,} | {self.only_right:,} |",
        ]
        out += [f"| changed (same key) | {self.changed:,} | |", ""] if self.by_key else [""]
        if self.differing_keys:
            out += [f"Rows differ in {self.differing_keys:,} `{key}` value(s).", ""]
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

    def rows(sql: str) -> list[dict]:
        cols, data = engine.run(sql)
        return [
            {c: (MASK if is_pii(c, pii) and v is not None else v) for c, v in zip(cols, r)}
            for r in data
        ]

    bare = ", ".join(key)
    repeated = "SELECT count(*) FROM (SELECT {k} FROM {t} _lz GROUP BY {k} HAVING count(*) > 1) _lz_k"
    report.repeated_keys_left = engine.scalar(repeated.format(k=bare, t=left))
    report.repeated_keys_right = engine.scalar(repeated.format(k=bare, t=right))
    if not report.by_key:
        # Joining on a repeated key pairs every row with every other row of the same key and
        # reports changes that don't exist. Count each distinct row on both sides instead.
        _diff_whole_rows(engine, report, left, right, [c for c in lcols if c in rcols], rows, sample)
        return report

    on = " AND ".join(f"l.{k} = r.{k}" for k in key)
    keys = ", ".join(f"l.{k}" for k in key)
    rkeys = ", ".join(f"r.{k}" for k in key)
    anti = f"FROM {left} l WHERE NOT EXISTS (SELECT 1 FROM {right} r WHERE {on})"
    anti_r = f"FROM {right} r WHERE NOT EXISTS (SELECT 1 FROM {left} l WHERE {on})"
    report.only_left = engine.scalar(f"SELECT count(*) {anti}")
    report.only_right = engine.scalar(f"SELECT count(*) {anti_r}")

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


def _diff_whole_rows(engine, report: DiffReport, left: str, right: str, cols: list[str], rows, sample: int) -> None:
    """Multiset difference over the shared columns: a row present twice on one side and once
    on the other counts once. GROUP BY treats NULLs as equal, so no null-safe join is needed."""
    cols_sql, keys = ", ".join(cols), ", ".join(report.key)
    n_left = "sum(CASE WHEN _lz_side = 'l' THEN 1 ELSE 0 END)"
    n_right = "sum(CASE WHEN _lz_side = 'r' THEN 1 ELSE 0 END)"
    differing = (
        f"(SELECT {cols_sql}, {n_left} AS _lz_l, {n_right} AS _lz_r FROM ("
        f"SELECT {cols_sql}, 'l' AS _lz_side FROM {left} _lz "
        f"UNION ALL SELECT {cols_sql}, 'r' AS _lz_side FROM {right} _lz"
        f") _lz_u GROUP BY {cols_sql} HAVING {n_left} <> {n_right}) _lz_d"
    )
    report.only_left, report.only_right = engine.run(
        "SELECT coalesce(sum(CASE WHEN _lz_l > _lz_r THEN _lz_l - _lz_r ELSE 0 END), 0), "
        f"coalesce(sum(CASE WHEN _lz_r > _lz_l THEN _lz_r - _lz_l ELSE 0 END), 0) FROM {differing}"
    )[1][0]
    report.differing_keys = engine.scalar(f"SELECT count(*) FROM (SELECT {keys} FROM {differing} GROUP BY {keys}) _lz_k")
    report.samples[f"differing {keys}"] = rows(
        f"SELECT {keys}, sum(CASE WHEN _lz_l > _lz_r THEN _lz_l - _lz_r ELSE 0 END) AS only_left, "
        f"sum(CASE WHEN _lz_r > _lz_l THEN _lz_r - _lz_l ELSE 0 END) AS only_right "
        f"FROM {differing} GROUP BY {keys} ORDER BY {keys} LIMIT {sample}"
    )
    report.samples["only in left"] = rows(
        f"SELECT {cols_sql} FROM {differing} WHERE _lz_l > _lz_r ORDER BY {cols_sql} LIMIT {sample}"
    )
    report.samples["only in right"] = rows(
        f"SELECT {cols_sql} FROM {differing} WHERE _lz_r > _lz_l ORDER BY {cols_sql} LIMIT {sample}"
    )
