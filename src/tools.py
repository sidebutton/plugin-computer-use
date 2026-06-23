"""Canonical computer-use tool surface (the contract sibling tickets build to).

This is the single source of truth that ``server.py`` serves over ``tools/list``,
that ``plugin.json`` mirrors, and that ``docs/computer-use-mcp-tools-schema.md``
documents. Names are the **bare canonical** Anthropic computer-use action ids
(``screenshot``, ``left_click``, ``type`` ...). Several of these collide with
core SideButton MCP tool names; namespacing on aggregation is deferred to the
service engine (SCRUM-1406) — see the README.

Each entry is a plain MCP tool definition (``name`` / ``description`` /
``inputSchema``). ``OWNER`` maps each tool to the sibling ticket that implements
its body; SCRUM-1397 only wires ``screenshot``.
"""

from __future__ import annotations

# Reusable schema fragments -------------------------------------------------
_COORDINATE = {
    "type": "array",
    "items": {"type": "integer"},
    "minItems": 2,
    "maxItems": 2,
    "description": "[x, y] in the model coordinate space (scaled to the screen).",
}

# Shared with screenshot + zoom (the canonical contract puts it on both).
_SAVE_TO_DISK = {
    "type": "boolean",
    "description": "Save the image to disk and return the saved path in the tool "
    "result, so it can be attached to a message for the user. Only set this when "
    "you intend to share the image.",
}


def _obj(properties: dict, required=None) -> dict:
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


# Tool surface, grouped by sibling ticket ----------------------------------
# capture (SCRUM-1400)
_CAPTURE = [
    {
        "name": "screenshot",
        "description": "Capture the active display (DISPLAY=:10) and return a "
        "base64-encoded PNG image block. The returned image is the coordinate "
        "space that subsequent click/move calls refer to. Set save_to_disk to "
        "also write the PNG and return its path.",
        "inputSchema": _obj({"save_to_disk": _SAVE_TO_DISK}),
    },
    {
        "name": "zoom",
        "description": "Take a higher-resolution screenshot of a region of the "
        "last full-screen screenshot — use it to inspect small text, button "
        "labels, or fine UI detail. Coordinates in later click calls still refer "
        "to the full-screen screenshot, never the zoomed image. Read-only.",
        "inputSchema": _obj(
            {
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "(x0, y0, x1, y1): rectangle to zoom into, in "
                    "the coordinate space of the most recent full-screen "
                    "screenshot. x0,y0 = top-left, x1,y1 = bottom-right.",
                },
                "save_to_disk": _SAVE_TO_DISK,
            },
            required=["region"],
        ),
    },
]

# click (SCRUM-1401)
_CLICK = [
    {
        "name": name,
        "description": desc,
        "inputSchema": _obj(
            {
                "coordinate": _COORDINATE,
                "text": {
                    "type": "string",
                    "description": "Optional modifier key(s) to hold during the "
                    "click, e.g. 'ctrl' or 'shift+alt'.",
                },
            },
            required=["coordinate"],
        ),
    }
    for name, desc in [
        ("left_click", "Left-click at a coordinate."),
        ("right_click", "Right-click at a coordinate."),
        ("middle_click", "Middle-click at a coordinate."),
        ("double_click", "Double left-click at a coordinate."),
        ("triple_click", "Triple left-click at a coordinate."),
    ]
]

# move / drag / scroll (SCRUM-1402)
_MOVE = [
    {
        "name": "mouse_move",
        "description": "Move the pointer to a coordinate without clicking.",
        "inputSchema": _obj({"coordinate": _COORDINATE}, required=["coordinate"]),
    },
    {
        "name": "left_click_drag",
        "description": "Press the left button at start_coordinate (or the current "
        "position) and drag to coordinate before releasing.",
        "inputSchema": _obj(
            {"start_coordinate": _COORDINATE, "coordinate": _COORDINATE},
            required=["coordinate"],
        ),
    },
    {
        "name": "scroll",
        "description": "Scroll in a direction by an amount at a coordinate.",
        "inputSchema": _obj(
            {
                "coordinate": _COORDINATE,
                "scroll_direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                },
                "scroll_amount": {
                    "type": "integer",
                    "description": "Number of scroll 'clicks'.",
                },
            },
            required=["coordinate", "scroll_direction", "scroll_amount"],
        ),
    },
    {
        "name": "left_mouse_down",
        "description": "Press and hold the left mouse button (released later by "
        "left_mouse_up). Requires the persistent session.",
        "inputSchema": _obj({"coordinate": _COORDINATE}),
    },
    {
        "name": "left_mouse_up",
        "description": "Release a left mouse button held by left_mouse_down.",
        "inputSchema": _obj({"coordinate": _COORDINATE}),
    },
]

# keyboard (SCRUM-1403)
_KEYBOARD = [
    {
        "name": "type",
        "description": "Type a string of text at the current focus.",
        "inputSchema": _obj({"text": {"type": "string"}}, required=["text"]),
    },
    {
        "name": "key",
        "description": "Press a key or chord using xdotool key syntax, e.g. "
        "'Return', 'ctrl+s', 'alt+Tab'.",
        "inputSchema": _obj(
            {
                "text": {"type": "string"},
                "repeat": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Number of times to press the key/chord "
                    "(xdotool --repeat); defaults to 1.",
                },
            },
            required=["text"],
        ),
    },
    {
        "name": "hold_key",
        "description": "Hold a key (or chord) down for a duration in seconds.",
        "inputSchema": _obj(
            {
                "text": {"type": "string"},
                "duration": {
                    "type": "number",
                    "description": "Seconds to hold (may be up to ~100s).",
                },
            },
            required=["text", "duration"],
        ),
    },
]

# clipboard + session stubs (SCRUM-1404)
# Names + count are the authoritative 6-tool group from SCRUM-1404 (clipboard is
# read/write split, and list_granted_applications pairs with request_access).
# This is the 2-tool delta that brings the surface to the epic's 24 (SCRUM-1399).
_CLIPBOARD = [
    {
        "name": "read_clipboard",
        "description": "Read the X clipboard contents (via xclip).",
        "inputSchema": _obj({}),
    },
    {
        "name": "write_clipboard",
        "description": "Write text to the X clipboard (via xclip).",
        "inputSchema": _obj({"text": {"type": "string"}}, required=["text"]),
    },
    {
        "name": "request_access",
        "description": "Request a session grant for one or more applications "
        "(Linux stub: auto-grants and returns screenshotFiltering=false; the real "
        "grant model lands with the service engine).",
        "inputSchema": _obj(
            {
                "applications": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Applications to request access to.",
                }
            }
        ),
    },
    {
        "name": "list_granted_applications",
        "description": "Return the set of applications currently granted desktop "
        "access (Linux stub: echoes the granted set).",
        "inputSchema": _obj({}),
    },
    {
        "name": "open_application",
        "description": "Launch or focus a desktop application by name.",
        "inputSchema": _obj({"name": {"type": "string"}}, required=["name"]),
    },
    {
        "name": "switch_display",
        "description": "Switch the display the session targets, e.g. ':10'.",
        "inputSchema": _obj(
            {"display": {"type": "string"}}, required=["display"]
        ),
    },
]

# utility / batch (SCRUM-1405)
_UTILITY = [
    {
        "name": "computer_batch",
        "description": "Run a sequence of computer-use actions in order and "
        "return one combined result.",
        "inputSchema": _obj(
            {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["name"],
                    },
                    "description": "Ordered list of {name, arguments} actions.",
                }
            },
            required=["actions"],
        ),
    },
    {
        "name": "wait",
        "description": "Wait for a duration in seconds (then optionally screenshot).",
        "inputSchema": _obj(
            {"duration": {"type": "number"}}, required=["duration"]
        ),
    },
    {
        "name": "cursor_position",
        "description": "Return the current cursor [x, y] in model coordinates.",
        "inputSchema": _obj({}),
    },
]

# Map each tool to the sibling ticket that owns its body.
OWNER = {
    **{t["name"]: "SCRUM-1400" for t in _CAPTURE},
    **{t["name"]: "SCRUM-1401" for t in _CLICK},
    **{t["name"]: "SCRUM-1402" for t in _MOVE},
    **{t["name"]: "SCRUM-1403" for t in _KEYBOARD},
    **{t["name"]: "SCRUM-1404" for t in _CLIPBOARD},
    **{t["name"]: "SCRUM-1405" for t in _UTILITY},
}

# The full, ordered surface returned by tools/list.
TOOLS = [*_CAPTURE, *_CLICK, *_MOVE, *_KEYBOARD, *_CLIPBOARD, *_UTILITY]
TOOL_NAMES = [t["name"] for t in TOOLS]

# Tools with a live body wired in server.py. Everything else is declared-only
# and returns a pending-owner error until its sibling ticket lands.
#   screenshot            — SCRUM-1397 (scaffold proof action)
#   screenshot / zoom     — SCRUM-1400 (capture group: save_to_disk + zoom)
#   type / key / hold_key — SCRUM-1403 (keyboard group)
IMPLEMENTED = {"screenshot", "zoom", "type", "key", "hold_key"}


def owner_ticket(name: str) -> str:
    return OWNER.get(name, "an unassigned sibling ticket")
