"""Fetch the manifest of a dbt Cloud job, so the data map describes what production runs.

It uses the Admin API artifacts endpoint, which serves the manifest of the job's latest
successful run as it is. The Discovery API only exposes parts of it through GraphQL.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import DbtCloudConfig

HINTS = {
    401: "the token was rejected",
    403: "the token cannot read this job's artifacts",
    404: "no manifest found; check account_id and job_id, and that the job has a successful run",
}


class DbtCloudError(RuntimeError):
    pass


def base_url(host: str) -> str:
    host = host.rstrip("/")
    return host if host.startswith(("http://", "https://")) else f"https://{host}"


def artifact_url(cloud: DbtCloudConfig, name: str = "manifest.json") -> str:
    return f"{base_url(cloud.host)}/api/v2/accounts/{cloud.account_id}/jobs/{cloud.job_id}/artifacts/{name}"


def download_manifest(cloud: DbtCloudConfig, timeout: float = 60) -> dict:
    token = os.environ.get(cloud.token_env)
    if not token:
        raise DbtCloudError(f"Set {cloud.token_env} to a dbt Cloud token that can read job artifacts")
    req = urllib.request.Request(artifact_url(cloud), headers={"Accept": "application/json", "Accept-Encoding": "gzip"})
    # Unredirected: if dbt Cloud redirects to a signed storage URL, the token stays behind.
    req.add_unredirected_header("Authorization", f"Token {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
    except urllib.error.HTTPError as e:
        raise DbtCloudError(f"dbt Cloud returned {e.code} for job {cloud.job_id}: {HINTS.get(e.code, e.reason)}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise DbtCloudError(f"Could not reach {base_url(cloud.host)}: {getattr(e, 'reason', e)}") from None
    try:
        manifest = json.loads(body)
    except ValueError:
        raise DbtCloudError(f"dbt Cloud did not return a JSON manifest for job {cloud.job_id}") from None
    if not isinstance(manifest, dict) or "nodes" not in manifest:
        raise DbtCloudError(f"dbt Cloud did not return a dbt manifest for job {cloud.job_id}")
    return manifest


def _origin(cloud: DbtCloudConfig, manifest: dict) -> str:
    generated = (manifest.get("metadata") or {}).get("generated_at")
    return f"dbt Cloud job {cloud.job_id}" + (f", generated at {generated}" if generated else "")


def fetch_manifest(cloud: DbtCloudConfig, cache_dir: Path) -> tuple[dict, str]:
    """The job's manifest and a line saying where it came from.

    A cached copy younger than `cache_minutes` is used as is. If dbt Cloud cannot be
    reached, an older cached copy is better than nothing, and the origin says so.
    """
    cache = cache_dir / f"dbt_cloud_{cloud.account_id}_{cloud.job_id}_manifest.json"
    age = time.time() - cache.stat().st_mtime if cache.exists() else None
    if age is not None and age < cloud.cache_minutes * 60:
        manifest = json.loads(cache.read_text())
        return manifest, _origin(cloud, manifest)
    try:
        manifest = download_manifest(cloud)
    except DbtCloudError as e:
        if age is None:
            raise
        manifest = json.loads(cache.read_text())
        return manifest, f"{_origin(cloud, manifest)} (cached copy; refresh failed: {e})"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest))
    tmp.replace(cache)
    return manifest, _origin(cloud, manifest)
