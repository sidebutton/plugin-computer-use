#!/usr/bin/env python3
"""Persistent stdio MCP server for the SideButton computer-use plugin.

Speaks newline-delimited JSON-RPC 2.0 over stdin/stdout (the MCP stdio
transport): ``initialize`` / ``tools/list`` / ``tools/call`` (plus ``ping`` and
the ``notifications/initialized`` notification). The process is long-lived and
single-owner so the cross-call state the computer-use surface needs (a held
mouse button, the screenshot->coordinate session) can live here once the sibling
tickets land.

SCRUM-1397 wires ``screenshot`` end-to-end as the proof action. Every other tool
is declared in ``tools/list`` but returns a pending-owner ``isError`` until its
sibling ticket (SCRUM-1400..1405) implements it against the ``computer.py``
dispatch base.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from computer import Computer, ComputerError, SingleOwnerLock  # noqa: E402
from tools import IMPLEMENTED, TOOL_NAMES, TOOLS, owner_ticket  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "computer-use"
SERVER_VERSION = "0.1.0"


class Server:
    """Stateless-per-message JSON-RPC dispatcher over a single Computer."""

    def __init__(self, computer: Computer):
        self.computer = computer
        # name -> handler(arguments) -> MCP result. Only tools also listed in
        # tools.IMPLEMENTED are dispatched; every other declared tool returns a
        # pending-owner error. Sibling tickets (SCRUM-1400..1405) register their
        # handlers here as they land.
        self._handlers = {
            "screenshot": self._screenshot,   # SCRUM-1397 / 1400
            "zoom": self._zoom,               # SCRUM-1400
            "type": self._type,               # SCRUM-1403
            "key": self._key,                 # SCRUM-1403
            "hold_key": self._hold_key,       # SCRUM-1403
        }

    def handle(self, msg) -> dict | None:
        """Return a JSON-RPC response dict, or ``None`` for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            mid = msg.get("id") if isinstance(msg, dict) else None
            return _error(mid, -32600, "invalid request")

        method = msg.get("method")
        mid = msg.get("id")
        is_notification = "id" not in msg

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
            elif method in ("notifications/initialized", "initialized"):
                return None  # notification, no response
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call_tool(msg.get("params") or {})
            else:
                if is_notification:
                    return None
                return _error(mid, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            return _error(mid, -32603, f"internal error: {exc}")

        return None if is_notification else {"jsonrpc": "2.0", "id": mid, "result": result}

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        if name not in TOOL_NAMES:
            return _tool_error(f"unknown tool: {name!r}")

        handler = self._handlers.get(name) if name in IMPLEMENTED else None
        if handler is None:
            # Declared in the surface but its body is owned by a sibling ticket.
            return _tool_error(
                f"tool {name!r} is declared but not implemented yet; its body is "
                f"owned by {owner_ticket(name)}. The dispatch base (computer.py: "
                f"run_xdotool / scale_coordinates / screenshot / to_device) is "
                f"ready for that work."
            )

        try:
            return handler(params.get("arguments") or {})
        except ComputerError as exc:
            return _tool_error(str(exc))

    # --- tool handlers ---------------------------------------------------
    # Capture group (SCRUM-1400): screenshot/zoom return MCP image content
    # blocks (plus a saved-path text block when save_to_disk is honoured).
    def _screenshot(self, args: dict) -> dict:
        cap = self.computer.screenshot(
            save_to_disk=bool(args.get("save_to_disk", False))
        )
        return _capture_result(cap)

    def _zoom(self, args: dict) -> dict:
        cap = self.computer.zoom(
            region=args.get("region"),
            save_to_disk=bool(args.get("save_to_disk", False)),
        )
        return _capture_result(cap)

    # Keyboard group (SCRUM-1403): plain text acknowledgements.
    def _type(self, args: dict) -> dict:
        return _tool_ok(self.computer.type_text(args["text"]))

    def _key(self, args: dict) -> dict:
        return _tool_ok(self.computer.press_key(args["text"], args.get("repeat", 1)))

    def _hold_key(self, args: dict) -> dict:
        return _tool_ok(self.computer.hold_key(args["text"], args["duration"]))


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _capture_result(cap) -> dict:
    """An MCP result for a capture: an image block, plus a text block carrying the
    saved path when ``save_to_disk`` was honoured."""
    content = [{"type": "image", "data": cap.data_b64, "mimeType": "image/png"}]
    if cap.path:
        content.append({"type": "text", "text": f"Saved to disk: {cap.path}"})
    return {"content": content, "isError": False}


def _tool_ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def main() -> int:
    computer = Computer()
    # Single-owner: one computer-use session per shared desktop. Held for the
    # process lifetime; a second instance fails fast instead of fighting over
    # the pointer/keyboard.
    lock = SingleOwnerLock()
    try:
        lock.acquire()
    except ComputerError as exc:
        sys.stderr.write(f"computer-use: {exc}\n")
        return 1

    server = Server(computer)
    out = sys.stdout
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                out.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
                out.flush()
                continue
            response = server.handle(msg)
            if response is not None:
                out.write(json.dumps(response) + "\n")
                out.flush()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
