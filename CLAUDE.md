# Lazarillo: notes for contributors and agents

- Python ≥ 3.11, package in `src/lazarillo`, tests in `tests/` (`pytest -q`).
- Setup: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`, then `python demo/build_demo.py`.
- Every CLI command prints Markdown; the MCP tools return the same Markdown.
- Guardrails live in code (`guardrails.py`, `warehouse.open_warehouse`), never only in prompts.
  Any new tool that touches data must go through `Warehouse` so the read-only, row-cap
  and PII rules apply.
- The demo company (Tarima Tickets) is fictional. Never add real company names, schemas or data.
