"""Build the Tarima Tickets demo: a fictional concert ticketing company with the
problems real data platforms have.

    ticketing system (DuckDB)  ──extract──▶  landing (Delta, migrating to Iceberg)
                                             ──▶ warehouse.landing ──dbt──▶ analytics ──▶ Power BI

Three problems are planted on purpose:
1. Source vs landing: the extract uses an `updated_at` watermark, so box office sales,
   which sync to the source days later, are never extracted. The source also added a
   `sales_channel` column that the extract ignores.
2. Delta vs Iceberg: the new Iceberg writer stores `ordered_at` in UTC for the last days,
   while the legacy Delta writer used local time.
3. Incremental drift: `fct_orders` only picks up rows with a newer `ordered_at`, so
   refunds of older orders never reach production and promoters get overpaid.

Run from the repo root:  python examples/tarima-tickets/build_demo.py
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PROJECT = HERE / "tarima"

START = datetime(2026, 9, 1)
DAYS = 14
CUTOFF = START + timedelta(days=11)  # first production run happens with data up to here
ICEBERG_CUTOVER = START + timedelta(days=10)  # new writer goes live
# All artists and venues are made up.
EVENTS = [
    ("Los Tejados", "Sala Faro", "Barcelona", 28),
    ("Marea Baja", "Teatro Cierzo", "Zaragoza", 22),
    ("Nube Negra", "Recinto Levante", "Valencia", 45),
    ("Clara Brisa", "Auditorio Alameda", "Sevilla", 35),
    ("Los Tejados", "Palacio Norte", "Madrid", 32),
    ("Kilómetro Cero", "Sala Faro", "Barcelona", 18),
    ("Neón Rural", "Nave 9", "Bilbao", 25),
    ("Marea Baja", "Recinto Levante", "Valencia", 22),
]
FIRST = ["Lucía", "Hugo", "Martina", "Mateo", "Sofía", "Leo", "Valeria", "Daniel", "Julia", "Pablo"]
LAST = ["García", "Rodríguez", "López", "Martín", "Sánchez", "Pérez", "Gómez", "Ruiz"]


def generate(rng: random.Random):
    events = [
        {"event_id": i, "artist": a, "venue": v, "city": c,
         "starts_at": START + timedelta(days=30 + 4 * i, hours=21)}
        for i, (a, v, c, _) in enumerate(EVENTS, start=1)
    ]
    orders = []
    for n in range(1, 4001):
        ordered_at = START + timedelta(seconds=rng.randrange(DAYS * 86400))
        first, last = rng.choice(FIRST), rng.choice(LAST)
        event_id = rng.randrange(1, len(events) + 1)
        base = EVENTS[event_id - 1][3]
        price = Decimal(base * rng.choice([1, 1, 1, 2, 3])) + Decimal("0.00")
        refunded_at = ordered_at + timedelta(days=rng.uniform(0.5, 6)) if rng.random() < 0.06 else None
        # Box office sales reach the source system days later, when the venue syncs.
        channel = "box_office" if rng.random() < 0.01 else rng.choice(["web", "app"])
        synced_at = ordered_at + (timedelta(days=2) if channel == "box_office" else timedelta(minutes=1))
        orders.append({
            "order_id": n,
            "buyer_name": f"{first} {last}",
            "buyer_email": f"{first}.{last}{n}@example.com".lower(),
            "event_id": event_id,
            "ordered_at": ordered_at.replace(microsecond=0),
            "price_eur": price,
            "fees_eur": (price * Decimal("0.12")).quantize(Decimal("0.01")),
            "sales_channel": channel,
            "_refunded_at": refunded_at,
            "_synced_at": synced_at,
        })
    return events, orders


def snapshot(orders, as_of: datetime, extracted: bool = False):
    """What the source looked like at `as_of`, or what the watermark extract managed to copy."""
    rows = []
    for o in orders:
        if o["_synced_at"] > as_of:
            continue
        late = o["_synced_at"] - o["ordered_at"] > timedelta(days=1)
        if extracted and late and o["_refunded_at"] is None:
            continue  # updated_at was already behind the watermark when the row appeared
        refunded = o["_refunded_at"] is not None and o["_refunded_at"] <= as_of
        rows.append({k: v for k, v in o.items() if not k.startswith("_")} | {
            "status": "refunded" if refunded else "paid",
            "updated_at": (o["_refunded_at"] if refunded else o["ordered_at"]).replace(microsecond=0),
        })
    return rows


ORDER_SCHEMA = pa.schema([
    ("order_id", pa.int64()), ("buyer_name", pa.string()), ("buyer_email", pa.string()),
    ("event_id", pa.int64()), ("ordered_at", pa.timestamp("us")), ("price_eur", pa.decimal128(10, 2)),
    ("fees_eur", pa.decimal128(10, 2)), ("status", pa.string()), ("updated_at", pa.timestamp("us")),
])
SOURCE_SCHEMA = ORDER_SCHEMA.append(pa.field("sales_channel", pa.string()))


def to_landing(rows) -> pa.Table:
    """The extract job: it predates `sales_channel`, so it silently drops it."""
    return pa.Table.from_pylist([{k: r[k] for k in ORDER_SCHEMA.names} for r in rows], ORDER_SCHEMA)


def write_delta(table: pa.Table, name: str):
    from deltalake import write_deltalake

    write_deltalake(str(DATA / "landing" / "delta" / name), table, mode="overwrite")


def write_iceberg(table: pa.Table):
    """The migration target. Its writer converts local time to UTC after the cutover (bug #2)."""
    from pyiceberg.catalog.sql import SqlCatalog

    ts = table.column("ordered_at").to_pylist()
    shifted = [t - timedelta(hours=2) if t >= ICEBERG_CUTOVER else t for t in ts]
    table = table.set_column(table.schema.get_field_index("ordered_at"), "ordered_at",
                             pa.array(shifted, pa.timestamp("us")))
    root = DATA / "landing" / "iceberg"
    root.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog("tarima", uri=f"sqlite:///{root / 'catalog.db'}", warehouse=f"file://{root}")
    catalog.create_namespace_if_not_exists("landing")
    if catalog.table_exists("landing.orders"):
        catalog.drop_table("landing.orders")
    catalog.create_table("landing.orders", schema=table.schema).append(table)


def load_warehouse(orders: pa.Table, events: pa.Table):
    """Production reads landing from Delta (think Redshift Spectrum over the Glue catalog)."""
    con = duckdb.connect(str(DATA / "warehouse.duckdb"))
    con.execute("CREATE SCHEMA IF NOT EXISTS landing")
    con.register("o", orders)
    con.register("e", events)
    con.execute("CREATE OR REPLACE TABLE landing.orders AS SELECT * FROM o")
    con.execute("CREATE OR REPLACE TABLE landing.events AS SELECT * FROM e")
    con.close()


def dbt(*args: str):
    exe = Path(sys.executable).parent / "dbt"
    cmd = [str(exe if exe.exists() else "dbt"), *args, "--project-dir", str(PROJECT), "--profiles-dir", str(PROJECT)]
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True)
    if proc.returncode:
        sys.exit(proc.stdout + proc.stderr)


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    (DATA / "landing").mkdir(parents=True)
    rng = random.Random(42)
    events, orders = generate(rng)
    end = START + timedelta(days=DAYS)

    # The ticketing system as it is today, including the new column.
    src = duckdb.connect(str(DATA / "source.duckdb"))
    src.register("o", pa.Table.from_pylist(snapshot(orders, end), SOURCE_SCHEMA))
    src.register("e", pa.Table.from_pylist(events))
    src.execute("CREATE TABLE orders AS SELECT * FROM o")
    src.execute("CREATE TABLE events AS SELECT * FROM e")
    src.close()
    events_t = pa.Table.from_pylist(events)

    print("① first load and full dbt build (prod)")
    load_warehouse(to_landing(snapshot(orders, CUTOFF, extracted=True)), events_t)
    dbt("build", "--target", "prod")

    print("② daily loads continue; dbt runs incrementally")
    today = to_landing(snapshot(orders, end, extracted=True))
    write_delta(today, "orders")
    write_iceberg(today)
    load_warehouse(today, events_t)
    dbt("build", "--target", "prod")
    print(f"✔ demo ready in {DATA} — try: cd {HERE} && lazarillo map")


if __name__ == "__main__":
    main()
