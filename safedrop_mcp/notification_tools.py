"""Cross-device notification mirroring (v1.6).

Adds a ``show_notification`` peer tool to the local SafeDrop registry.
When another paired device calls it, the call is recorded in a
ring-buffer and any subscribed callback fires (the desktop GUI uses
that hook to pop a tkinter banner; iOS / Android can do platform-
native notifications via their own tool implementations).

This module owns *just* the Python side. iOS and Android have their
own ``show_notification`` registered in their respective ToolRegistry.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Any, Callable, Deque, Optional

from safedrop.tools import ToolRegistry, ToolSpec


NotificationCallback = Callable[[dict[str, Any]], None]
_RING_CAP = 50


class NotificationBus:
    """In-process ring buffer + optional callback hook."""

    def __init__(self, capacity: int = _RING_CAP) -> None:
        self._lock = Lock()
        self._ring: Deque[dict[str, Any]] = deque(maxlen=capacity)
        self.on_notification: Optional[NotificationCallback] = None

    def push(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._ring.append(entry)
        cb = self.on_notification
        if cb is not None:
            try:
                cb(entry)
            except Exception:
                pass

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._ring)
        if limit > 0 and len(rows) > limit:
            rows = rows[-limit:]
        return rows


# Module-level singleton — the GUI installs its callback on this.
bus = NotificationBus()


def _handle(args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    level = str(args.get("level") or "info").strip().lower()
    if level not in ("info", "warn", "error"):
        level = "info"
    if not title and not body:
        return {"status": "error", "error": "title or body required"}
    entry = {
        "ts": time.time(),
        "title": title,
        "body": body,
        "level": level,
        "from_label": str(args.get("__from_label") or ""),
    }
    bus.push(entry)
    return {"status": "shown", "ts": entry["ts"]}


def register_notification_peer_tool(registry: ToolRegistry) -> None:
    """Register the ``show_notification`` peer tool on ``registry``."""
    registry.register(ToolSpec(
        name="show_notification",
        description=(
            "Show a notification on this device. The recipient renders it "
            "natively (tkinter banner on desktop, UNUserNotificationCenter "
            "on iOS, NotificationManager on Android). Returns when the "
            "notification has been enqueued — no user interaction expected."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body":  {"type": "string"},
                "level": {"type": "string", "enum": ["info", "warn", "error"], "default": "info"},
            },
        },
        handler=_handle,
    ))
