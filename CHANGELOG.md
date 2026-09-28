# Changelog

## Unreleased

- `diff` and `verify` no longer report changes that don't exist when the key repeats, as the
  `unique_key` of a delete+insert model by date does. Such a key made the join pair every row
  with every other row of the same date. The diff now notices the repeat, compares whole rows
  and lists which key values differ; pass the row grain as the key to see changes by column.
- The `verify` MCP tool takes `key`, like `lazarillo verify -k`.

## 0.1.2

- `lazarillo init` registers the MCP server for your agent: Claude Code (`.mcp.json`),
  Cursor (`.cursor/mcp.json`) or VS Code (`.vscode/mcp.json`). It asks in a terminal,
  defaulting to the agents the project already has settings for, or takes `--mcp`. Other
  servers in those files are kept. On a project with a `lazarillo.yml`, `init --mcp` only does this.
- The repo no longer ships a `.mcp.json` at its root; the demo gets one from `init --mcp`.

## 0.1.1

- `lazarillo init` reads the warehouse from the production target in dbt's `profiles.yml`
  (DuckDB, or Redshift with password or IAM) and asks only for what is missing. Flags such as
  `--warehouse redshift --host … --iam` cover scripts and agents; `--no-input` never asks.
- Redshift passwords can come from a named env var (`password_env`), as dbt profiles do.
  Passwords are never written to `lazarillo.yml`.
- `lazarillo doctor` (and the `doctor` MCP tool): config, connection, a write the warehouse
  must refuse, visible tables, dbt manifest and production schema, as a Markdown checklist.
- The README quickstart no longer ends in "No warehouse at …".

## 0.1.0

First public release.

- `lazarillo map`, `describe` and `impact`: a data map from the dbt manifest, local or from a
  dbt Cloud job, with materializations, incremental strategies, tests, lineage and exposures.
- `lazarillo query`: one read-only statement per call, row-capped, with PII masking and no
  filesystem access.
- `lazarillo diff`: row-level diff between warehouse tables and lake files (Delta, Parquet,
  Iceberg), locally or on S3, AWS Glue and DuckLake.
- `lazarillo verify`: builds a model in dev, diffs it against prod and reports the blast radius.
- Warehouses: DuckDB and Redshift (password or IAM, read-only session).
- `lazarillo init` and `lazarillo mcp`: the same tools over MCP, returning the same Markdown.
- Tarima Tickets demo in `examples/`, with three planted data problems.
