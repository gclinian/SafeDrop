"""Peer-tool wrappers for :class:`safedrop.handoff.HandoffStore`.

Registered on the local SafeDrop ``ToolRegistry`` so paired devices can
save / load / list "draft" state over the encrypted CALL_TOOL channel.
The MCP server side calls these same handlers internally for its own
``handoff_*`` tools so there's no duplication.
"""

from __future__ import annotations

from typing import Any, Optional

from safedrop.handoff import HandoffStore
from safedrop.tools import ToolRegistry, ToolSpec


_store: Optional[HandoffStore] = None


def get_store() -> HandoffStore:
    """Process-wide :class:`HandoffStore` (lazy singleton, default path)."""
    global _store
    if _store is None:
        _store = HandoffStore()
    return _store


def _save(args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("key") or "").strip()
    content = str(args.get("content") or "")
    mime = str(args.get("mime_type") or "text/plain")
    updated_by = str(args.get("__from_label") or "")  # set by caller if known
    if not key:
        return {"status": "error", "error": "key is required"}
    try:
        entry = get_store().save(key, content, mime, updated_by=updated_by)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    return {
        "status": "saved",
        "key": entry.key,
        "length": len(entry.content),
        "updated_at": entry.updated_at,
    }


def _load(args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("key") or "").strip()
    if not key:
        return {"status": "error", "error": "key is required"}
    entry = get_store().load(key)
    if entry is None:
        return {"status": "not_found", "key": key}
    return {
        "status": "loaded",
        "key": entry.key,
        "content": entry.content,
        "mime_type": entry.mime_type,
        "updated_at": entry.updated_at,
        "updated_by": entry.updated_by,
    }


def _list(_args: dict[str, Any]) -> dict[str, Any]:
    return {"entries": [e.summary() for e in get_store().list()]}


def _delete(args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("key") or "").strip()
    if not key:
        return {"status": "error", "error": "key is required"}
    return {"status": "deleted" if get_store().delete(key) else "not_found", "key": key}


def register_handoff_peer_tools(registry: ToolRegistry) -> None:
    """Add handoff_save / handoff_load / handoff_list / handoff_delete to ``registry``."""

    registry.register(ToolSpec(
        name="handoff_save",
        description=(
            "Save a piece of state (text) under a key so it can be picked up "
            "on another paired device via handoff_load. Useful for: draft "
            "messages, browser tab URLs, in-progress shell commands, etc. "
            "Overwrites any prior entry under the same key."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key":       {"type": "string"},
                "content":   {"type": "string"},
                "mime_type": {"type": "string", "default": "text/plain"},
            },
            "required": ["key", "content"],
        },
        handler=_save,
    ))

    registry.register(ToolSpec(
        name="handoff_load",
        description=(
            "Load a previously saved handoff by key. Returns "
            "{status, key, content, mime_type, updated_at, updated_by}."
        ),
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        handler=_load,
    ))

    registry.register(ToolSpec(
        name="handoff_list",
        description=(
            "List all stored handoffs, newest first. Each row has "
            "{key, preview (first 120 chars), length, mime_type, "
            "updated_at, updated_by}. Use handoff_load to fetch full content."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_list,
    ))

    registry.register(ToolSpec(
        name="handoff_delete",
        description="Delete a handoff entry by key.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        handler=_delete,
    ))
