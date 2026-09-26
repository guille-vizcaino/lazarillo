# Reading a lake in S3, Glue and DuckLake

Lazarillo reads lake tables from local disk or from S3. Iceberg tables come from any
catalog pyiceberg supports (SQL, AWS Glue, REST...), and DuckLakes can be attached next
to the warehouse. None of this needs a cloud account to try; see *Testing without AWS*.

```bash
pip install "lazarillo[delta,glue]"   # glue brings iceberg and boto3
```

## Configuration

```yaml
warehouse:
  path: warehouse.duckdb

attach:
  src: source.duckdb                       # another DuckDB file, as before
  lake: ducklake:lake/metadata.ducklake    # a DuckLake (files local or in S3)
  # lake: {type: ducklake, path: "postgres:dbname=lake", data_path: s3://my-lake/ducklake/}

landing:
  # Where `delta:` and `parquet:` refs may read. Anything else is refused.
  # Defaults to the folder holding lazarillo.yml.
  locations:
    - s3://my-lake/landing/
  s3:                         # every key is optional
    region: eu-west-1
    profile: lake-readonly    # or leave it out and use the AWS default chain
    # endpoint: http://localhost:9000    # MinIO, LocalStack...
  iceberg_catalog:            # passed to pyiceberg's load_catalog
    name: glue
    type: glue
    # glue.id: "123456789012"            # another account's catalog
```

Then refer to tables as usual:

```console
$ lazarillo diff delta:s3://my-lake/landing/delta/orders iceberg:landing.orders -k order_id
$ lazarillo diff parquet:s3://my-lake/landing/raw/dt=2026-09-*/*.parquet landing.orders -k order_id
$ lazarillo query "select status, count(*) from lake.orders group by 1"
```

## Credentials

Lazarillo resolves credentials once with boto3 and hands the same identity to every
reader (deltalake, pyarrow, pyiceberg and DuckDB for DuckLake), so SSO and assume-role
profiles work everywhere. The order is:

1. `access_key_id` / `secret_access_key` in `landing.s3` (avoid committing them).
2. `profile` in `landing.s3`.
3. The AWS default chain: environment variables, `AWS_PROFILE`, ~/.aws, instance or task role.

Keys in `landing.s3` also fill in the Glue catalog's credentials and region. Anything
set on `iceberg_catalog` itself (`glue.region`, `s3.endpoint`...) wins.

Lazarillo only reads. A policy like this one is enough:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::my-lake", "arn:aws:s3:::my-lake/landing/*"]},
    {"Effect": "Allow", "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetTables"],
     "Resource": "*"}
  ]
}
```

## Guardrails

- SQL still cannot touch files or the network (`enable_external_access = false`).
  Delta, Parquet and Iceberg tables are opened in Python and handed to DuckDB as Arrow.
- `delta:` and `parquet:` paths must sit inside `landing.locations`. `..` segments,
  symlinks that leave the folder and other buckets are refused.
- Iceberg tables are reached by name through the catalog, never by path.
- A DuckLake is attached read-only. DuckDB reads its Parquet files itself, so the
  lake's data folder, and only that folder, stays readable from SQL.

## Limits

- Iceberg tables are loaded into memory before the diff. Keep them to what fits, or
  compare a snapshot or partition you copy elsewhere, until pushdown lands.
- DuckLake needs DuckDB's `ducklake` extension, and `httpfs` when its files live in S3.
  DuckDB downloads them on first use; install them once if you work offline.
- The warehouse is still a DuckDB file. Redshift is next on the roadmap.

## Testing without AWS

`pytest` runs every S3 and Glue test against [moto](https://github.com/getmoto/moto)'s
server, in process. To run them against a real S3-compatible server too:

```bash
docker run -d -p 9000:9000 -e MINIO_ROOT_USER=lazarillo -e MINIO_ROOT_PASSWORD=lazarillo-secret bitnamilegacy/minio
LAZARILLO_TEST_S3_ENDPOINT=http://127.0.0.1:9000 LAZARILLO_TEST_S3_KEY=lazarillo \
  LAZARILLO_TEST_S3_SECRET=lazarillo-secret pytest -m minio
```

MinIO has no Glue, so there the Iceberg tests use a SQL catalog whose data lives in the bucket.
