# Changelog

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
