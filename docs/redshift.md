# Redshift as the warehouse

Point Lazarillo at Redshift (provisioned or Serverless) and `query`, `diff` and `verify`
run against it with the same guardrails as DuckDB. `map`, `describe` and `impact` only
read the dbt manifest, so they already work with any warehouse.

```bash
pip install "lazarillo[redshift]"     # psycopg2 and boto3
pip install dbt-redshift              # only if you use verify
```

## Configuration

`lazarillo init` writes this for you: it reads the production target of your dbt-redshift
profile, or asks for the host, database and how you sign in. Without a terminal, pass flags:

```bash
lazarillo init --no-input -w redshift --host my-cluster.abc123.eu-west-1.redshift.amazonaws.com \
  --database analytics --iam --cluster my-cluster --aws-profile warehouse-readonly
lazarillo doctor
```

```yaml
warehouse:
  type: redshift
  host: my-cluster.abc123.eu-west-1.redshift.amazonaws.com
  port: 5439
  database: analytics
  user: lazarillo_ro            # password from PGPASSWORD or ~/.pgpass
  # timeout_seconds: 300        # every statement is cancelled after this
  # sslmode: require            # the default

dbt:
  project_dir: transform
  prod_target: prod             # targets in your dbt-redshift profile
  dev_target: dev
  prod_schema: analytics
  dev_schema: dbt_dev
```

Leave the password out of the file. libpq reads it from `PGPASSWORD` or `~/.pgpass`, or
name the variable that holds it, as dbt profiles do with `env_var()`:

```yaml
  password_env: REDSHIFT_PASSWORD
```

Or use IAM and skip passwords altogether:

```yaml
warehouse:
  type: redshift
  host: my-cluster.abc123.eu-west-1.redshift.amazonaws.com
  database: analytics
  iam: true
  cluster_identifier: my-cluster    # provisioned
  # workgroup: analytics            # Serverless, instead of cluster_identifier
  # user: lazarillo_ro              # provisioned only; omit to map the IAM identity
  # profile: warehouse-readonly     # else the AWS default chain
  # region: eu-west-1               # else taken from the host
```

With IAM, Lazarillo asks the Redshift API for a temporary password on every connection
(`GetClusterCredentials`, `GetClusterCredentialsWithIAM` or Serverless `GetCredentials`).
The AWS identity comes from the same chain as the lake: `profile`, else environment
variables, `AWS_PROFILE`, SSO, or an instance or task role.

## What runs where

| ref | runs in |
|---|---|
| `analytics.fct_orders`, `db.schema.table` | Redshift |
| `delta:`, `iceberg:`, `parquet:`, attached databases | a local, in-memory DuckDB |
| `query "SQL"` | Redshift |

A diff between two warehouse tables, such as `verify`'s prod against dev, runs entirely
inside Redshift: only counts and a few sample rows come back. A diff between a lake
table and a warehouse table copies the warehouse side locally, after applying `--where`
in Redshift. Above `guardrails.max_transfer_rows` (1,000,000 by default) it is refused
and you are asked to narrow the window.

```console
$ lazarillo verify fct_orders --where "ordered_at >= '2026-09-01'"
$ lazarillo diff delta:s3://my-lake/landing/delta/orders staging.orders -k order_id \
    --where "ordered_at >= '2026-09-20'"
```

## Guardrails

- Agent SQL goes through the same parser check as with DuckDB: one read-only statement.
  Redshift-only syntax the check cannot parse, such as `TOP 10` or `UNLOAD`, is refused;
  use `LIMIT`.
- Every session is a single `BEGIN READ ONLY` transaction, so anything that slips past
  the parser, such as a function that writes, is refused by Redshift itself.
- `statement_timeout` cancels long queries (`timeout_seconds`, 300 by default).
- Row caps and PII masking are applied to results as usual.
- Connections are tagged `application_name = lazarillo`, so they are easy to find in
  `SYS_QUERY_HISTORY` and WLM rules.

Still, give Lazarillo a user that can only read. For example:

```sql
CREATE USER lazarillo_ro PASSWORD DISABLE;   -- IAM only
GRANT USAGE ON SCHEMA analytics, dbt_dev TO lazarillo_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics, dbt_dev TO lazarillo_ro;
```

`verify` builds the dev schema with dbt, which uses your dbt profile and its own
credentials. Lazarillo itself never writes.

## Testing without Redshift

Redshift speaks the Postgres protocol, so the integration tests run against Postgres:

```bash
docker run -d -p 5439:5432 -e POSTGRES_USER=lazarillo -e POSTGRES_PASSWORD=lazarillo \
  -e POSTGRES_DB=dev postgres:16-alpine
LAZARILLO_TEST_PG_DSN=postgresql://lazarillo:lazarillo@127.0.0.1:5439/dev pytest -m postgres
```

IAM credentials are tested with stubbed AWS APIs and need no account. Postgres is not
Redshift, so the SQL Lazarillo generates sticks to what both accept (no `IS DISTINCT FROM`,
`FILTER` or `ANTI JOIN`).
