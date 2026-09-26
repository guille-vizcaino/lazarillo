# Lazarillo: notes for contributors and agents

- Python ≥ 3.11, package in `src/lazarillo`, tests in `tests/` (`pytest -q`).
- Setup: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`, then `python examples/tarima-tickets/build_demo.py`.
- The package never imports from `examples/`; the demo is a project that uses Lazarillo, and tests build
  their own fixtures.
- Every CLI command prints Markdown; the MCP tools return the same Markdown.
- Guardrails live in code (`guardrails.py`, `warehouse.open_warehouse`), never only in prompts.
  Any new tool that touches data must go through `Warehouse` so the read-only, row-cap
  and PII rules apply. Lake files are opened by `lake.Lake`, only inside `landing.locations`.
- S3 and Glue tests run on moto with no account; `pytest -m minio` adds a real MinIO (docs/aws.md).
- The demo company (Tarima Tickets) is fictional. Never add real company names, schemas or data.
