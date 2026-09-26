# Lazarillo

> **Lazarillo** (n.): the boy who guides a blind man across 16th-century Spain in
> *Lazarillo de Tormes*. Your AI agent can't see your warehouse. Lazarillo guides it.

Lazarillo is a **data harness**: the layer between an AI agent and your lakehouse that
gives the agent **context**, enforces **guardrails** and **verifies** every change before it
reaches production.

SaaS products used to wrap a database in a UI. The equivalent today is a harness: it wraps
your data, your conventions and your workflows so that an agent can work on them
safely. Lazarillo is an opinionated harness for analytics engineering. It is local-first,
vendor-neutral, and exposed as a CLI and an MCP server.

## Why

Agents already write decent SQL. They fail in three other ways:

| The agent… | Lazarillo gives it… |
|---|---|
| doesn't know what `fct_orders` means, how it's built, or who reads it | **Context**: a data map compiled from the dbt manifest, covering materializations, incremental strategies, tests, lineage and exposures such as Power BI dashboards. |
| can `DROP` a table, scan 3 TB or print customer emails | **Guardrails** in code, not in the prompt: read-only, one statement per call, row caps, PII masking, no filesystem access. |
| says "done ✅" without proof | **Verification**: build the change in a dev schema, diff it row by row against prod, and show the blast radius. |

The picaresque twist: Lázaro famously tricks the blind man. So the harness **never trusts
the agent's word**. Every claim comes with a diff.

## Use it on your project

```bash
pip install "lazarillo[all] @ git+https://github.com/guille-vizcaino/lazarillo"
cd your-project
lazarillo init                # writes lazarillo.yml and finds your dbt project, if any
lazarillo query "select 42"
```

`lazarillo init` prints the `.mcp.json` block that hands the harness to your agent. The core
only needs DuckDB; pick extras for the rest of your stack: `delta`, `iceberg`, `dbt`, `mcp`,
or `all`. Without dbt you still get `query` and `diff`; add a dbt project to unlock `map`,
`describe`, `impact` and `verify`. If production runs in dbt Cloud, the map can come from
your production job instead ([docs/dbt-cloud.md](https://github.com/guille-vizcaino/lazarillo/blob/main/docs/dbt-cloud.md)).

## Try the demo (2 minutes, no cloud account)

```bash
git clone https://github.com/guille-vizcaino/lazarillo && cd lazarillo
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
python examples/tarima-tickets/build_demo.py   # builds Tarima Tickets, a fictional concert ticketing company
cd examples/tarima-tickets && lazarillo map
```

The demo lives in [`examples/tarima-tickets`](https://github.com/guille-vizcaino/lazarillo/tree/main/examples/tarima-tickets) and uses Lazarillo
exactly like your project would: its own `lazarillo.yml`, dbt project and data. It mirrors a common AWS setup (ticketing system → S3 landing in Delta, migrating
to Iceberg → warehouse → dbt → Power BI) on DuckDB. Three problems are planted in it,
the kind that reach production every week:

### 1. Source vs landing: "are we extracting everything?"

```console
$ lazarillo diff src.orders landing.orders -k order_id
| rows       | 3,996 | 3,961 |
| only here  |    35 |     0 |
Schema drift
- `sales_channel` only in left
```

The extract uses an `updated_at` watermark. Box office sales reach the ticketing system
days later, when the venue syncs, so they fall behind the watermark and are never landed.
The source also grew a column the extract ignores.

### 2. Delta → Iceberg migration: "is the new table identical?"

```console
$ lazarillo diff delta:data/landing/delta/orders iceberg:landing.orders -k order_id
| changed (same key) | 1,081 |
- `ordered_at`: 1,081 rows
| 2 | 2026-09-11 05:47:14 | 2026-09-11 03:47:14 |
```

Same row count and same keys, so a count-based check passes. The new writer stores
timestamps in UTC, and a row-level diff catches the shift.

### 3. Incremental drift: "is prod what a full rebuild would give?"

```console
$ lazarillo verify fct_orders
Built `dev.fct_orders` and compared it with `analytics.fct_orders`.
- `status`: 38 rows      (paid → refunded)
### Blast radius
- downstream models: fct_event_revenue
- **Revenue by event** (dashboard, owner: Finance)
⚠️ Dev and prod differ. Review the diff above before merging; 1 exposure(s) will see the change.
```

The incremental filter only picks up *new* orders, so later refunds never reach
production. The revenue-by-event dashboard overstates what each promoter is owed.
`verify` is also the loop an agent uses after editing a model: change the filter to
`updated_at`, verify, and show the diff.

## Use it with an agent

```bash
lazarillo -c examples/tarima-tickets/lazarillo.yml mcp     # MCP over stdio
```

This repo ships a `.mcp.json` wired to the demo, so opening it in Claude Code gives the agent these tools:
`data_map`, `describe_model`, `impact`, `query`, `diff` and `verify`. The server also sends
instructions that tell the agent to verify before it claims success.

Try: *"Finance says the payout for Los Tejados in Barcelona looks too high. Find out why and propose a fix."*

## Commands

| command | what it does |
|---|---|
| `lazarillo init [DIR]` | Write a starter `lazarillo.yml` and print the MCP config |
| `lazarillo map` | Sources, models, materializations, exposures |
| `lazarillo describe MODEL` | Columns, tests, SQL, upstream and downstream |
| `lazarillo impact MODEL` | Downstream models and dashboards |
| `lazarillo query "SQL"` | One read-only statement, capped and masked |
| `lazarillo diff LEFT RIGHT -k KEY [--where]` | Row-level diff between any two relations |
| `lazarillo verify MODEL [--where]` | Build in dev, diff against prod, report blast radius |
| `lazarillo mcp` | Serve all of the above over MCP |

Relations can be `schema.table`, `attached_db.table`, `delta:<path>`,
`iceberg:<namespace.table>` or `parquet:<glob>`. Paths can be local or `s3://`, Iceberg
catalogs can be SQL, AWS Glue or REST, and DuckLakes can be attached next to the
warehouse. See [docs/aws.md](https://github.com/guille-vizcaino/lazarillo/blob/main/docs/aws.md). The warehouse itself can be a DuckDB file or
Redshift, with IAM auth; see [docs/redshift.md](https://github.com/guille-vizcaino/lazarillo/blob/main/docs/redshift.md).

## Scope

**In scope:** everything from landing to consumption, i.e. landing tables, dbt models and
the dashboards that read them.

**Out of scope:** orchestration and scheduling (Step Functions, Airflow, EventBridge…).
Lazarillo doesn't run your pipelines. It checks what they produce.

## Roadmap

- [x] Data map from the dbt manifest, including exposures
- [x] Read-only guardrails, row caps and PII masking
- [x] Row-level diff across warehouse, Delta, Iceberg and Parquet
- [x] `verify`: dev build, diff and blast radius
- [x] MCP server
- [x] Lake in S3 (Delta, Parquet, Iceberg), AWS Glue catalog and DuckLake
- [x] Redshift as the warehouse, with IAM auth ([docs/redshift.md](https://github.com/guille-vizcaino/lazarillo/blob/main/docs/redshift.md))
- [x] dbt Cloud: read the manifest of the production job
- [ ] `--defer` builds so `verify` doesn't rebuild parents
- [ ] Cost guardrails (`EXPLAIN`-based scan budget)
- [ ] **`lazarillo checkride`**: a benchmark of real data-engineering tasks, scored with
      and without the harness ([docs/checkride.md](https://github.com/guille-vizcaino/lazarillo/blob/main/docs/checkride.md))

## License

MIT © Guillermo Vizcaíno Román. The demo company and all its data are fictional.
