"""Run the harness's SQL on Redshift (provisioned or Serverless).

Redshift speaks the Postgres protocol, so psycopg2 does the talking. The guardrails in
guardrails.py check every statement first; on top of that each session is one read-only
transaction with a statement timeout, so a write that slipped through is still refused
by Redshift itself.
"""

from __future__ import annotations

import re

from .config import RedshiftConfig
from .engine import Engine, WarehouseError

# Postgres type OIDs, which Redshift reuses, to readable names and Arrow types.
_TYPES = {
    16: "boolean", 20: "bigint", 21: "smallint", 23: "integer", 25: "text", 700: "real",
    701: "double precision", 1042: "char", 1043: "varchar", 1082: "date", 1083: "time",
    1114: "timestamp", 1184: "timestamptz", 1266: "timetz", 1700: "numeric", 3000: "geometry",
    4000: "super",
}

_REGION = re.compile(r"\.([a-z]{2}(?:-[a-z]+)+-\d)\.redshift(?:-serverless)?\.amazonaws\.com$")


def region_from_host(host: str) -> str | None:
    """`my-cluster.abc123.eu-west-1.redshift.amazonaws.com` -> `eu-west-1`."""
    m = _REGION.search(host)
    return m.group(1) if m else None


def credentials(rs: RedshiftConfig) -> tuple[str | None, str | None]:
    """User and password. With IAM, a temporary password from the Redshift API.

    boto3 resolves the identity like it does for the lake: `profile`, else the default
    chain (environment variables, ~/.aws, SSO, instance or task role).
    """
    if not rs.iam:
        # A missing password is fine: libpq then reads PGPASSWORD or ~/.pgpass.
        return rs.user, rs.password
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise WarehouseError("Redshift IAM auth needs boto3: pip install 'lazarillo[redshift]'") from None

    session = boto3.Session(profile_name=rs.profile, region_name=rs.region or region_from_host(rs.host))
    try:
        if rs.workgroup:
            r = session.client("redshift-serverless").get_credentials(workgroupName=rs.workgroup, dbName=rs.database)
            return r["dbUser"], r["dbPassword"]
        client = session.client("redshift")
        if rs.user:
            r = client.get_cluster_credentials(
                DbUser=rs.user, DbName=rs.database, ClusterIdentifier=rs.cluster_identifier, AutoCreate=False
            )
        else:
            # The database user is derived from the IAM identity (IAM:<user> or IAMR:<role>).
            r = client.get_cluster_credentials_with_iam(DbName=rs.database, ClusterIdentifier=rs.cluster_identifier)
        return r["DbUser"], r["DbPassword"]
    except (BotoCoreError, ClientError) as e:
        raise WarehouseError(f"Could not get Redshift credentials through IAM: {e}") from e


def connect(rs: RedshiftConfig) -> "RedshiftEngine":
    try:
        import psycopg2
    except ImportError:
        raise WarehouseError("A Redshift warehouse needs pip install 'lazarillo[redshift]'") from None
    user, password = credentials(rs)
    params = {
        "host": rs.host, "port": rs.port, "dbname": rs.database, "user": user, "password": password,
        "sslmode": rs.sslmode, "application_name": "lazarillo", "connect_timeout": 15,
    }
    try:
        con = psycopg2.connect(**{k: v for k, v in params.items() if v is not None})
    except psycopg2.Error as e:
        raise WarehouseError(f"Could not connect to Redshift at {rs.host}:{rs.port}: {e}".strip()) from e
    return RedshiftEngine(con, rs.timeout_seconds)


class RedshiftEngine(Engine):
    remote = True

    def __init__(self, con, timeout_seconds: int = 300):
        import psycopg2

        self._error = psycopg2.Error
        self.con = con
        # Transactions are managed here, not by psycopg2, so BEGIN READ ONLY is ours.
        self.con.autocommit = True
        self._timeout_ms = int(timeout_seconds * 1000)
        self._begin()

    def _begin(self) -> None:
        with self.con.cursor() as cur:
            cur.execute(f"SET statement_timeout TO {self._timeout_ms}")
            cur.execute("BEGIN READ ONLY")

    def _execute(self, sql: str):
        cur = self.con.cursor()
        try:
            cur.execute(sql)
        except self._error as e:
            cur.close()
            # A failed statement aborts the transaction; start a fresh read-only one.
            try:
                with self.con.cursor() as c:
                    c.execute("ROLLBACK")
                self._begin()
            except self._error:
                pass  # the connection itself is gone; the original error says why
            raise WarehouseError(str(e).strip()) from e
        return cur

    def run(self, sql: str) -> tuple[list[str], list[tuple]]:
        with self._execute(sql) as cur:
            return [d.name for d in cur.description], cur.fetchall()

    def columns(self, relation: str) -> list[tuple[str, str]]:
        with self._execute(f"SELECT * FROM {relation} AS _lz LIMIT 0") as cur:
            return [(d.name, _type_name(d)) for d in cur.description]

    def fetch_arrow(self, sql: str):
        """Run a query and return an Arrow table typed from the result's metadata."""
        import pyarrow as pa

        with self._execute(sql) as cur:
            desc, rows = cur.description, cur.fetchall()
        arrays = []
        for i, d in enumerate(desc):
            values = [r[i] for r in rows]
            try:
                arrays.append(pa.array(values, type=_arrow_type(d)))
            except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
                arrays.append(pa.array(values))
        return pa.table(arrays, names=[d.name for d in desc])

    def close(self) -> None:
        try:
            with self.con.cursor() as cur:
                cur.execute("ROLLBACK")
        except self._error:
            pass
        finally:
            self.con.close()


def _type_name(d) -> str:
    name = _TYPES.get(d.type_code, f"oid {d.type_code}")
    if d.type_code == 1700 and d.precision is not None:
        return f"numeric({d.precision},{d.scale})"
    if d.type_code in (1042, 1043) and d.internal_size and d.internal_size > 0:
        return f"{name}({d.internal_size})"
    return name


def _arrow_type(d):
    import pyarrow as pa

    if d.type_code == 1700:
        return pa.decimal128(d.precision, d.scale) if d.precision else None
    return {
        16: pa.bool_(), 20: pa.int64(), 21: pa.int16(), 23: pa.int32(), 25: pa.string(),
        700: pa.float32(), 701: pa.float64(), 1042: pa.string(), 1043: pa.string(),
        1082: pa.date32(), 1114: pa.timestamp("us"), 1184: pa.timestamp("us", tz="UTC"),
    }.get(d.type_code)
