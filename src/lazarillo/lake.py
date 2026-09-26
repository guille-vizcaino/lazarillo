"""Read lake tables from local disk or S3 and hand them to DuckDB as Arrow.

DuckDB itself never touches the filesystem or the network on the agent's behalf
(`enable_external_access = false`). Lake tables are opened here, in Python, and only
from the locations configured in `lazarillo.yml`.
"""

from __future__ import annotations

import fnmatch
import glob
import posixpath
from dataclasses import dataclass
from urllib.parse import ParseResult, urlparse

from .config import Config, S3Config, is_uri
from .guardrails import GuardrailViolation

_GLOB_CHARS = "*?["


class LakeError(Exception):
    """A lake table could not be opened: missing table, bad credentials, missing extra..."""


@dataclass(frozen=True)
class S3Access:
    """Resolved S3 settings, translated for each library that reads the lake."""

    region: str | None = None
    endpoint: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    profile: str | None = None

    @classmethod
    def resolve(cls, s3: S3Config | None) -> "S3Access":
        """Pick explicit keys, else the profile, else the AWS default chain via boto3.

        Resolving once with boto3 means Delta, Parquet, Iceberg and DuckLake all see the
        same identity, including SSO and assumed-role profiles some readers can't parse.
        """
        s3 = s3 or S3Config()
        keys = (s3.access_key_id, s3.secret_access_key, s3.session_token)
        region = s3.region
        if not s3.access_key_id:
            try:
                import boto3
            except ImportError:
                if s3.profile:
                    raise RuntimeError("An S3 profile needs boto3: pip install 'lazarillo[s3]'") from None
            else:
                session = boto3.Session(profile_name=s3.profile)
                region = region or session.region_name
                if creds := session.get_credentials():
                    c = creds.get_frozen_credentials()
                    keys = (c.access_key, c.secret_key, c.token)
        return cls(region, s3.endpoint, *keys, profile=s3.profile)

    @property
    def _endpoint(self) -> ParseResult:
        return urlparse(self.endpoint if "://" in (self.endpoint or "") else f"https://{self.endpoint}")

    def pyarrow_fs(self):
        from pyarrow.fs import S3FileSystem

        kw: dict = {"region": self.region}
        if self.endpoint:
            kw |= {"endpoint_override": self._endpoint.netloc, "scheme": self._endpoint.scheme}
        if self.access_key_id:
            kw |= {"access_key": self.access_key_id, "secret_key": self.secret_access_key,
                   "session_token": self.session_token}
        return S3FileSystem(**{k: v for k, v in kw.items() if v is not None})

    def delta_options(self) -> dict[str, str]:
        opts = {
            "AWS_REGION": self.region,
            "AWS_ENDPOINT_URL": self.endpoint and f"{self._endpoint.scheme}://{self._endpoint.netloc}",
            "AWS_ALLOW_HTTP": "true" if self.endpoint and self._endpoint.scheme == "http" else None,
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_SESSION_TOKEN": self.session_token,
        }
        return {k: v for k, v in opts.items() if v}

    def iceberg_properties(self, catalog_type: str | None) -> dict[str, str]:
        """pyiceberg properties for the S3 FileIO and, for a Glue catalog, the Glue client."""
        props = {
            "s3.region": self.region,
            "s3.endpoint": self.endpoint and f"{self._endpoint.scheme}://{self._endpoint.netloc}",
            "s3.access-key-id": self.access_key_id,
            "s3.secret-access-key": self.secret_access_key,
            "s3.session-token": self.session_token,
        }
        if catalog_type == "glue":
            props |= {
                "glue.region": self.region,
                "glue.access-key-id": self.access_key_id,
                "glue.secret-access-key": self.secret_access_key,
                "glue.session-token": self.session_token,
            }
        return {k: v for k, v in props.items() if v}

    def duckdb_secret(self) -> str:
        """A DuckDB S3 secret, for DuckLake tables whose files live in S3."""
        def q(v: str) -> str:
            return "'" + v.replace("'", "''") + "'"

        parts = ["TYPE s3"]
        if self.access_key_id:
            parts += [f"KEY_ID {q(self.access_key_id)}", f"SECRET {q(self.secret_access_key or '')}"]
            if self.session_token:
                parts.append(f"SESSION_TOKEN {q(self.session_token)}")
        if self.region:
            parts.append(f"REGION {q(self.region)}")
        if self.endpoint:
            parts += [f"ENDPOINT {q(self._endpoint.netloc)}", "URL_STYLE 'path'",
                      f"USE_SSL {'true' if self._endpoint.scheme == 'https' else 'false'}"]
        return f"CREATE OR REPLACE SECRET lazarillo_s3 ({', '.join(parts)})"


def _normalize_s3(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme not in ("s3", "s3a"):
        raise GuardrailViolation(f"Only s3:// object storage is supported, not {parsed.scheme}://")
    key = parsed.path.lstrip("/")
    if ".." in key.split("/"):
        raise GuardrailViolation(f"Relative segments are not allowed in {uri!r}")
    return f"s3://{parsed.netloc}/{key}"


class Lake:
    """Opens `delta:` and `parquet:` locations, inside the configured allowlist only."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._s3: S3Access | None = None

    @property
    def s3(self) -> S3Access:
        if self._s3 is None:
            self._s3 = S3Access.resolve(self.cfg.s3)
        return self._s3

    def check_location(self, location: str) -> str:
        """Return the normalized location, or refuse it if it is outside `landing.locations`."""
        if is_uri(location):
            target = _normalize_s3(location)
            allowed = [_normalize_s3(loc).rstrip("/") + "/" for loc in self.cfg.locations if is_uri(loc)]
            ok = any((target.rstrip("/") + "/").startswith(a) for a in allowed)
        else:
            from pathlib import Path

            target = str(Path(self.cfg.location(location)).resolve())
            allowed = [Path(loc).resolve() for loc in self.cfg.locations if not is_uri(loc)]
            ok = any(Path(target).is_relative_to(a) for a in allowed)
        if not ok:
            raise GuardrailViolation(
                f"{location!r} is outside the lake locations configured in lazarillo.yml "
                "(landing.locations)."
            )
        return target

    def delta(self, location: str):
        from deltalake import DeltaTable

        target = self.check_location(location)
        options = self.s3.delta_options() if is_uri(target) else None
        return DeltaTable(target, storage_options=options).to_pyarrow_dataset()

    def parquet(self, pattern: str):
        import pyarrow.dataset as ds

        target = self.check_location(pattern)
        if not is_uri(target):
            files = sorted(glob.glob(target, recursive=True))
            if not files:
                raise FileNotFoundError(f"No Parquet files match {pattern!r}")
            for f in files:  # a symlink could still point outside the allowlist
                self.check_location(f)
            return ds.dataset(files, format="parquet")

        fs, path = self.s3.pyarrow_fs(), target.removeprefix("s3://")
        if not any(c in path for c in _GLOB_CHARS):
            return ds.dataset(path, filesystem=fs, format="parquet")
        from pyarrow.fs import FileSelector, FileType

        base = path[: min(path.find(c) for c in _GLOB_CHARS if c in path)]
        base = posixpath.dirname(base) if not base.endswith("/") else base.rstrip("/")
        listing = fs.get_file_info(FileSelector(base, recursive=True))
        files = sorted(i.path for i in listing if i.type == FileType.File and fnmatch.fnmatch(i.path, path))
        if not files:
            raise FileNotFoundError(f"No Parquet files match {pattern!r}")
        return ds.dataset(files, filesystem=fs, format="parquet")

    def iceberg(self, identifier: str):
        from pyiceberg.catalog import load_catalog

        if not self.cfg.iceberg_catalog:
            raise ValueError("No landing.iceberg_catalog configured in lazarillo.yml")
        props = dict(self.cfg.iceberg_catalog)
        name = props.pop("name", "default")
        # Settings from landing.s3 fill the gaps; anything set on the catalog wins.
        if self.cfg.s3 is not None or props.get("type") == "glue":
            props = self.s3.iceberg_properties(props.get("type")) | props
        return load_catalog(name, **props).load_table(identifier).scan().to_arrow()
