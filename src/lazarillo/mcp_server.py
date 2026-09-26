"""Expose the harness to any MCP client. The tools are the only door into the data."""

from __future__ import annotations

import duckdb
from mcp.server.mcpserver import MCPServer

from .config import Config
from .context import DataMap
from .diff import diff as run_diff
from .guardrails import GuardrailViolation
from .lake import LakeError
from .verify import verify as run_verify
from .warehouse import open_warehouse

INSTRUCTIONS = """\
You are working on a data platform through Lazarillo, a harness that can see what you cannot.
1. Start with `data_map` to learn the sources, models and who consumes them.
2. Use `describe_model` before proposing a change, and `impact` to know the blast radius.
3. `query` is read-only, row-capped and masks PII. Never try to write through it.
4. Change dbt code, then run `verify` so the change is built in dev and diffed against prod.
   Report the diff and the affected exposures; do not claim success without it.
"""


def build_server(cfg: Config) -> MCPServer:
    server = MCPServer("lazarillo", instructions=INSTRUCTIONS)

    def datamap() -> DataMap:
        return DataMap.from_config(cfg)

    @server.tool()
    def data_map() -> str:
        """Sources, models (with materialization and incremental strategy) and exposures."""
        return datamap().to_markdown()

    @server.tool()
    def describe_model(model: str) -> str:
        """Columns, tests, SQL, upstream, downstream and exposures of one dbt model."""
        return datamap().describe(model)

    @server.tool()
    def impact(model: str) -> str:
        """Downstream models and dashboards affected if `model` changes."""
        models, exposures = datamap().downstream(model)
        lines = ["Models: " + (", ".join(m.name for m in models) or "none")]
        lines += [f"Exposure: {e.name} ({e.type}, owner: {e.owner})" for e in exposures]
        return "\n".join(lines)

    @server.tool()
    def query(sql: str) -> str:
        """Run ONE read-only SELECT. Results are row-capped and PII columns are masked."""
        with open_warehouse(cfg) as wh:
            try:
                return wh.query(sql).to_markdown()
            except GuardrailViolation as e:
                return f"Refused by guardrail: {e}"
            except duckdb.Error as e:
                return f"SQL error: {e}"

    @server.tool()
    def diff(left: str, right: str, key: list[str], where: str | None = None) -> str:
        """Compare two relations row by row. Refs: `schema.table`, `src.table`,
        `delta:<path or s3://...>`, `iceberg:<namespace.table>`, `parquet:<glob or s3://...>`."""
        with open_warehouse(cfg) as wh:
            try:
                return run_diff(wh, left, right, key, where).to_markdown()
            except GuardrailViolation as e:
                return f"Refused by guardrail: {e}"
            except LakeError as e:
                return f"Lake error: {e}"
            except duckdb.Error as e:
                return f"SQL error: {e}"

    @server.tool()
    def verify(model: str, where: str | None = None) -> str:
        """Build `model` (and its parents) in the dev schema, diff it against prod and
        list the exposures that would see the difference."""
        return run_verify(cfg, model, where=where).to_markdown()

    return server
