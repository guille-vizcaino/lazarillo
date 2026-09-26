# Changelog

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
