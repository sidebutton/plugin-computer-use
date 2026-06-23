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
        # name -> handler(arguments) -> MCP result dict. A table (vs. an elif
        # chain) keeps the merge-conflict surface small for the sibling tickets
        # (SCRUM-1400..1405) that each wire their own group into this method.
        # Only tools also listed in tools.IMPLEMENTED are dispatched; every other
        # declared tool returns a pending-owner error.
        self._handlers = {
            # capture (SCRUM-1397 / 1400)
            "screenshot": self._screenshot,
            "zoom": self._zoom,
            # click (SCRUM-1401)
            "left_click": self._left_click,
            "right_click": self._right_click,
            "middle_click": self._middle_click,
            "double_click": self._double_click,
            "triple_click": self._triple_click,
            # keyboard (SCRUM-1403)
            "type": self._type,
            "key": self._key,
            "hold_key": self._hold_key,
            # clipboard + session (SCRUM-1404)
            "read_clipboard": self._read_clipboard,
            "write_clipboard": self._write_clipboard,
            "request_access": self._request_access,
            "list_granted_applications": self._list_granted_applications,
            "open_application": self._open_application,
            "switch_display": self._switch_display,
            # utility / batch (SCRUM-1405)
            "wait": self._wait,
            "cursor_position": self._cursor_position,
            "computer_batch": self._computer_batch,
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
        return self._dispatch(params.get("name"), params.get("arguments") or {})

    def _dispatch(self, name, arguments: dict) -> dict:
        """Run one tool by name and return its MCP result dict.

        The single point of name validation + IMPLEMENTED/pending-owner gating, so
        ``computer_batch`` can re-dispatch each of its steps through exactly the
        same path as a top-level ``tools/call`` — no second code path. A handler's
        ``ComputerError`` becomes an ``isError`` result; any other exception
        propagates (``handle`` maps it to a JSON-RPC error; ``computer_batch``
        guards its own steps so one bad step can't crash the whole batch).
        """
        if name not in TOOL_NAMES:
            return _tool_error(f"unknown tool: {name!r}")

        handler = self._handlers.get(name)
        if handler is None or name not in IMPLEMENTED:
            # Declared in the surface but its body is owned by a sibling ticket.
            return _tool_error(
                f"tool {name!r} is declared but not yet implemented; its body is "
                f"owned by {owner_ticket(name)}. The dispatch base (computer.py: "
                f"run_xdotool / scale_coordinates / screenshot / to_device) is "
                f"ready for that work."
            )

        try:
            return handler(arguments or {})
        except ComputerError as exc:
            return _tool_error(str(exc))

    # --- tool handlers ----------------------------------------------------
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

    # Click group (SCRUM-1401): map each canonical action to a (button, count)
    # click at a screenshot-session coordinate; optional `text` holds modifiers.
    def _click(self, args: dict, button: str, count: int) -> dict:
        return _tool_text(
            self.computer.click(
                coordinate=args.get("coordinate"),
                button=button,
                count=count,
                text=args.get("text"),
            )
        )

    def _left_click(self, args: dict) -> dict:
        return self._click(args, "left", 1)

    def _right_click(self, args: dict) -> dict:
        return self._click(args, "right", 1)

    def _middle_click(self, args: dict) -> dict:
        return self._click(args, "middle", 1)

    def _double_click(self, args: dict) -> dict:
        return self._click(args, "left", 2)

    def _triple_click(self, args: dict) -> dict:
        return self._click(args, "left", 3)

    # Keyboard group (SCRUM-1403): plain text acknowledgements.
    def _type(self, args: dict) -> dict:
        return _tool_text(self.computer.type_text(args["text"]))

    def _key(self, args: dict) -> dict:
        return _tool_text(self.computer.press_key(args["text"], args.get("repeat", 1)))

    def _hold_key(self, args: dict) -> dict:
        return _tool_text(self.computer.hold_key(args["text"], args["duration"]))

    def _read_clipboard(self, args: dict) -> dict:
        return _tool_text(self.computer.read_clipboard())

    def _write_clipboard(self, args: dict) -> dict:
        text = args.get("text") or ""
        self.computer.write_clipboard(text)
        return _tool_text(f"wrote {len(text)} characters to the clipboard")

    def _request_access(self, args: dict) -> dict:
        return _tool_json(
            self.computer.request_access(
                apps=args.get("apps"),
                reason=args.get("reason"),
                clipboardRead=bool(args.get("clipboardRead", False)),
                clipboardWrite=bool(args.get("clipboardWrite", False)),
                systemKeyCombos=bool(args.get("systemKeyCombos", False)),
            )
        )

    def _list_granted_applications(self, args: dict) -> dict:
        return _tool_json(self.computer.list_granted_applications())

    def _open_application(self, args: dict) -> dict:
        return _tool_json(self.computer.open_application(args.get("app") or ""))

    def _switch_display(self, args: dict) -> dict:
        return _tool_json(self.computer.switch_display(args.get("display")))

    # Utility group (SCRUM-1405): wait/cursor_position plus computer_batch, an
    # in-server fan-out that re-dispatches each step through self._dispatch.
    def _wait(self, args: dict) -> dict:
        return _tool_text(self.computer.wait(args.get("duration")))

    def _cursor_position(self, args: dict) -> dict:
        return _tool_json(self.computer.cursor_position())

    def _computer_batch(self, args: dict) -> dict:
        """Run an ordered ``[{name, arguments}, …]`` sequence in one call.

        Re-dispatches each step through :meth:`_dispatch` (same validation +
        pending-owner gating as a top-level call), **stop-on-first-error**: the
        first step whose result is ``isError`` halts the batch and its index is
        reported as ``stopped_at``; the remaining steps are skipped. Returns one
        combined result — a JSON ``{"batch": …}`` summary block followed by each
        executed step's content blocks (text tagged with its step index, images
        passed through) — with ``isError`` set iff the batch halted early.

        A nested ``computer_batch`` step is rejected (no recursion), as is an
        empty ``actions`` sequence. The batch is deliberately NOT transactional:
        a halt leaves the already-applied steps' effects in place (the summary's
        ``stopped_at`` makes that fully observable), matching the browser_batch
        contract (the-assistant docs/plans/PLAN-actions-batching.md).
        """
        actions = args.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ComputerError("computer_batch requires a non-empty 'actions' array")

        blocks: list[dict] = []
        steps: list[dict] = []
        stopped_at: int | None = None
        succeeded = 0
        for i, action in enumerate(actions):
            name = action.get("name") if isinstance(action, dict) else None
            if not isinstance(name, str) or not name:
                res = _tool_error(f"action {i} must be an object with a string 'name'")
            elif name == "computer_batch":
                res = _tool_error(
                    f"action {i}: computer_batch cannot be nested inside a batch"
                )
            else:
                try:
                    res = self._dispatch(name, action.get("arguments") or {})
                except Exception as exc:  # noqa: BLE001 — one bad step never crashes the batch
                    res = _tool_error(f"action {i} ({name}) raised: {exc}")
            failed = bool(res.get("isError"))
            steps.append({"index": i, "name": name, "ok": not failed})
            for block in res.get("content", []):
                blocks.append(_tag_block(i, name, block))
            if failed:
                stopped_at = i
                break
            succeeded += 1

        failed_count = 1 if stopped_at is not None else 0
        summary = {
            "total": len(actions),
            "succeeded": succeeded,
            "failed": failed_count,
            "skipped": len(actions) - succeeded - failed_count,
            "stopped_at": stopped_at,
            "steps": steps,
        }
        return {
            "content": [{"type": "text", "text": json.dumps({"batch": summary})}, *blocks],
            "isError": stopped_at is not None,
        }


def _tag_block(index: int, name, block: dict) -> dict:
    """Prefix a batch step's text block with its step index/name so concatenated
    per-step output stays attributable; non-text (e.g. image) blocks pass through."""
    if block.get("type") == "text":
        return {**block, "text": f"[step {index} · {name}] {block.get('text', '')}"}
    return block


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


def _tool_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_json(obj) -> dict:
    """A successful result whose payload is structured (grants, focus, display)."""
    return {
        "content": [{"type": "text", "text": json.dumps(obj)}],
        "isError": False,
    }


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
