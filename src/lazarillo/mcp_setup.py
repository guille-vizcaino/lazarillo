"""Register Lazarillo as an MCP server in the agent's project config.

Each client reads a JSON file in the project. Other servers in the file are kept; only
the `lazarillo` entry is added or replaced.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "lazarillo"


@dataclass(frozen=True)
class Client:
    label: str
    path: str        # relative to the project
    key: str         # where the servers live in that file
    extra: tuple = ()  # fields the client wants on each server


CLIENTS = {
    "claude-code": Client("Claude Code", ".mcp.json", "mcpServers"),
    "cursor": Client("Cursor", ".cursor/mcp.json", "mcpServers"),
    "vscode": Client("VS Code", ".vscode/mcp.json", "servers", (("type", "stdio"),)),
}


def lazarillo_command() -> str:
    """The lazarillo executable, as an absolute path: agents don't activate virtualenvs."""
    local = Path(sys.executable).parent / "lazarillo"  # same virtualenv as this process
    return str(local) if local.exists() else (shutil.which("lazarillo") or "lazarillo")


def server_entry(config: Path, client: str = "claude-code") -> dict:
    return {**dict(CLIENTS[client].extra), "command": lazarillo_command(), "args": ["-c", str(config), "mcp"]}


def detect_clients(directory: Path) -> list[str]:
    """Clients the project already has settings for, e.g. a .cursor/ folder."""
    marks = {"claude-code": (".mcp.json", ".claude"), "cursor": (".cursor",), "vscode": (".vscode",)}
    return [name for name, paths in marks.items() if any((directory / p).exists() for p in paths)]


def parse_clients(value: str) -> list[str]:
    """`claude-code, cursor` -> ["claude-code", "cursor"]; `none` or blank -> []."""
    names = [v.strip().lower() for v in value.replace(" ", ",").split(",") if v.strip()]
    names = [n for n in names if n != "none"]
    if unknown := [n for n in names if n not in CLIENTS]:
        raise ValueError(f"Unknown MCP client {', '.join(unknown)}; use {', '.join(CLIENTS)} or none")
    return list(dict.fromkeys(names))


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON ({e}); fix it or add the server by hand") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object; fix it or add the server by hand")
    return data


def check(directory: Path, clients: list[str]) -> None:
    """Fail before anything is written if a client's file cannot be updated."""
    for name in clients:
        _load(directory / CLIENTS[name].path)


def install(directory: Path, config: Path, clients: list[str]) -> list[str]:
    """Add or replace the lazarillo server for each client. Returns one Markdown line each."""
    lines = []
    for name in clients:
        client = CLIENTS[name]
        path = directory / client.path
        data = _load(path)
        servers = data.setdefault(client.key, {})
        action = "updated" if SERVER_NAME in servers else "added"
        servers[SERVER_NAME] = server_entry(config, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        lines.append(f"- MCP: {action} `{SERVER_NAME}` in `{client.path}` ({client.label})")
    return lines
