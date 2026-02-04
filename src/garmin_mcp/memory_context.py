"""
Persistent training context memory tools.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


DEFAULT_NAMESPACE = "default"


def _memory_dir() -> str:
    return os.path.expanduser(os.getenv("MCP_MEMORY_DIR", "~/.garmin_mcp/memory"))


def _safe_namespace(namespace: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (namespace or "").strip())
    return cleaned or DEFAULT_NAMESPACE


def _memory_path(namespace: str) -> str:
    return os.path.join(_memory_dir(), f"{_safe_namespace(namespace)}.json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_memory(namespace: str) -> Dict[str, Any]:
    path = _memory_path(namespace)
    try:
        with open(path, "r") as handle:
            payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
                return payload
    except FileNotFoundError:
        return {"updated_at": _utc_now_iso(), "entries": []}
    except json.JSONDecodeError:
        return {"updated_at": _utc_now_iso(), "entries": []}
    return {"updated_at": _utc_now_iso(), "entries": []}


def _write_memory(namespace: str, payload: Dict[str, Any]) -> None:
    directory = _memory_dir()
    os.makedirs(directory, exist_ok=True)
    path = _memory_path(namespace)
    temp_path = f"{path}.tmp-{os.getpid()}"
    with open(temp_path, "w") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def register_tools(app):
    """Register training context memory tools with the MCP server app."""

    @app.tool()
    async def memory_get(namespace: str = DEFAULT_NAMESPACE, limit: Optional[int] = None) -> str:
        """Get persisted training context memory entries.

        Args:
            namespace: Logical namespace for separating memories
            limit: Optional limit for most recent entries
        """
        payload = _load_memory(namespace)
        entries: List[Dict[str, Any]] = payload.get("entries", [])
        if limit is not None:
            entries = entries[-max(limit, 0):]
        return json.dumps(
            {
                "updated_at": payload.get("updated_at"),
                "entries": entries,
            },
            indent=2,
        )

    @app.tool()
    async def memory_write(
        namespace: str = DEFAULT_NAMESPACE,
        data: Optional[Dict[str, Any]] = None,
        mode: str = "append",
    ) -> str:
        """Persist training context memory entries.

        Args:
            namespace: Logical namespace for separating memories
            data: Entry payload to append or replace with
            mode: append | replace | clear
        """
        read_only = os.getenv("MCP_READ_ONLY", "true").lower() in ("1", "true", "yes")
        memory_write_enabled = os.getenv("MCP_MEMORY_WRITE_ENABLED", "true").lower() in (
            "1",
            "true",
            "yes",
        )
        if read_only and not memory_write_enabled:
            return "Error: MCP_READ_ONLY is enabled. Writes are disabled."

        payload = _load_memory(namespace)
        entries: List[Dict[str, Any]] = payload.get("entries", [])
        mode = (mode or "append").lower().strip()

        if mode == "clear":
            entries = []
        elif mode == "replace":
            entries = [
                {
                    "timestamp": _utc_now_iso(),
                    "data": data or {},
                }
            ]
        else:
            entries.append(
                {
                    "timestamp": _utc_now_iso(),
                    "data": data or {},
                }
            )

        updated = {"updated_at": _utc_now_iso(), "entries": entries}
        _write_memory(namespace, updated)
        return json.dumps(updated, indent=2)

    return app
