"""dbt Cloud manifests, served by a local HTTP server that plays the Admin API."""

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from click.testing import CliRunner

from lazarillo.cli import main
from lazarillo.config import load_config
from lazarillo.context import DataMap
from lazarillo.dbt_cloud import DbtCloudError

from test_context import MANIFEST

CLOUD_MANIFEST = {**MANIFEST, "metadata": {"generated_at": "2026-09-26T06:00:00Z"}}
PATH = "/api/v2/accounts/11/jobs/22/artifacts/manifest.json"


class FakeDbtCloud(BaseHTTPRequestHandler):
    status = 200
    gzip = False
    requests: list = []

    def do_GET(self):
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        if self.path != PATH:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(CLOUD_MANIFEST).encode()
        if self.gzip:
            body = gzip.compress(body)
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        if self.gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body if self.status == 200 else b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def dbt_cloud(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DBT_CLOUD_API_TOKEN", "secret")
    handler = type("Handler", (FakeDbtCloud,), {"requests": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield handler, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def write_config(tmp_path, host, extra=""):
    (tmp_path / "lazarillo.yml").write_text(
        "warehouse: {path: wh.duckdb}\n"
        f"dbt:\n{extra}  cloud:\n    account_id: 11\n    job_id: 22\n    host: {host}\n"
    )
    return load_config(tmp_path / "lazarillo.yml")


def write_local_manifest(tmp_path):
    (tmp_path / "proj" / "target").mkdir(parents=True)
    local = {**MANIFEST, "nodes": {k: v for k, v in MANIFEST["nodes"].items() if k != "model.p.fct"}}
    (tmp_path / "proj" / "target" / "manifest.json").write_text(json.dumps(local))


def test_reads_the_job_manifest_with_the_token_from_the_environment(tmp_path, dbt_cloud):
    handler, host = dbt_cloud
    cfg = write_config(tmp_path, host)
    assert cfg.dbt.project_dir is None

    dm = DataMap.from_config(cfg)

    assert handler.requests == [(PATH, "Token secret")]
    assert {m.name for m in dm.nodes.values()} == {"stg", "fct"}
    assert "_Manifest: dbt Cloud job 22, generated at 2026-09-26T06:00:00Z_" in dm.to_markdown()


def test_gzip_responses_are_decompressed(tmp_path, dbt_cloud):
    handler, host = dbt_cloud
    handler.gzip = True
    assert "fct" in {m.name for m in DataMap.from_config(write_config(tmp_path, host)).nodes.values()}


def test_downloads_are_cached(tmp_path, dbt_cloud):
    handler, host = dbt_cloud
    cfg = write_config(tmp_path, host)
    DataMap.from_config(cfg)
    DataMap.from_config(cfg)
    assert len(handler.requests) == 1
    assert (tmp_path / ".lazarillo" / "dbt_cloud_11_22_manifest.json").exists()

    cfg.dbt.cloud.cache_minutes = 0
    DataMap.from_config(cfg)
    assert len(handler.requests) == 2


def test_a_stale_cache_beats_nothing_when_dbt_cloud_fails(tmp_path, dbt_cloud):
    handler, host = dbt_cloud
    cfg = write_config(tmp_path, host)
    DataMap.from_config(cfg)
    cfg.dbt.cloud.cache_minutes = 0
    handler.status = 500

    dm = DataMap.from_config(cfg)

    assert "fct" in {m.name for m in dm.nodes.values()}
    assert "cached copy; refresh failed: dbt Cloud returned 500" in dm.origin


def test_falls_back_to_the_local_manifest_and_says_so(tmp_path, dbt_cloud):
    handler, host = dbt_cloud
    handler.status = 401
    write_local_manifest(tmp_path)
    cfg = write_config(tmp_path, host, extra="  project_dir: proj\n")

    dm = DataMap.from_config(cfg)

    assert {m.name for m in dm.nodes.values()} == {"stg"}
    assert "local target/manifest.json, because dbt Cloud failed" in dm.to_markdown()
    assert "the token was rejected" in dm.origin


def test_missing_token_is_a_clear_error(tmp_path, dbt_cloud, monkeypatch):
    handler, host = dbt_cloud
    monkeypatch.delenv("DBT_CLOUD_API_TOKEN")
    write_config(tmp_path, host)

    result = CliRunner().invoke(main, ["-c", str(tmp_path / "lazarillo.yml"), "map"])

    assert result.exit_code == 1
    assert "Set DBT_CLOUD_API_TOKEN" in result.output
    assert handler.requests == []


def test_wrong_job_is_reported(tmp_path, dbt_cloud):
    handler, host = dbt_cloud
    cfg = write_config(tmp_path, host)
    cfg.dbt.cloud.job_id = 99
    with pytest.raises(DbtCloudError, match="404 for job 99: no manifest found"):
        DataMap.from_config(cfg)


def test_token_is_not_forwarded_on_redirects(tmp_path, dbt_cloud):
    handler, host = dbt_cloud
    signed = "/signed/manifest.json"

    def do_get(self):
        handler.requests.append((self.path, self.headers.get("Authorization")))
        self.send_response(302 if self.path == PATH else 200)
        if self.path == PATH:
            self.send_header("Location", f"{host}{signed}")
        self.end_headers()
        if self.path == signed:
            self.wfile.write(json.dumps(CLOUD_MANIFEST).encode())

    handler.do_GET = do_get
    DataMap.from_config(write_config(tmp_path, host))
    assert handler.requests == [(PATH, "Token secret"), (signed, None)]


def test_dbt_section_needs_a_manifest_source(tmp_path):
    (tmp_path / "lazarillo.yml").write_text("warehouse: {path: wh.duckdb}\ndbt: {prod_schema: x}\n")
    with pytest.raises(ValueError, match="project_dir, cloud, or both"):
        load_config(tmp_path / "lazarillo.yml")


def test_host_defaults_to_https():
    from lazarillo.config import DbtCloudConfig
    from lazarillo.dbt_cloud import artifact_url

    url = artifact_url(DbtCloudConfig(account_id=1, job_id=2, host="emea.dbt.com"))
    assert url == "https://emea.dbt.com/api/v2/accounts/1/jobs/2/artifacts/manifest.json"
