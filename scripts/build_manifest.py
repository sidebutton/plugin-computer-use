#!/usr/bin/env python3
"""Generate plugin.json and docs/computer-use-mcp-tools-schema.md from tools.py.

Both artifacts are derived from the single source of truth (``src/tools.py``) so
the manifest, the tools/list surface, and the schema doc can never drift. Run
this after editing the tool surface:

    python3 scripts/build_manifest.py

``tests/test_manifest.py`` fails if the committed files are stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tools import IMPLEMENTED, OWNER, TOOLS  # noqa: E402

NAME = "computer-use"
VERSION = "0.1.0"
DESCRIPTION = (
    "Persistent stdio MCP server exposing the Anthropic computer-use action "
    "surface (screenshot, click, move, keyboard, clipboard, batch) against the "
    "agent desktop on DISPLAY=:10. Loads on the merged service-plugin tier "
    "(SCRUM-1406); individual tool bodies land in SCRUM-1400..1405."
)

# Service-plugin spawn spec consumed by the merged engine's validateServiceSpec
# (the-assistant packages/server/src/plugins/loader.ts). Only command / timeoutMs
# / toolNamespace / tools are recognized; everything else is ignored. `command`
# must be a non-empty *string* — the engine whitespace-splits it and spawns the
# child with cwd=plugin dir, so the relative "src/server.py" resolves
# (service-manager.ts). `toolNamespace` pins the public prefix to "computer_use"
# (else it defaults to the hyphenated plugin name → computer-use_*). hold_key/wait
# can run up to ~100s, so they override the 120s service default explicitly.
SERVICE = {
    "command": "python3 src/server.py",
    "toolNamespace": "computer_use",
    "tools": {
        "hold_key": {"timeoutMs": 120000},
        "wait": {"timeoutMs": 120000},
    },
}


def build_manifest() -> dict:
    return {
        "name": NAME,
        "version": VERSION,
        "description": DESCRIPTION,
        "runtime": "service",
        "service": SERVICE,
        # Service-tier manifests carry no static tools: the engine normalizes
        # this to [] and discovers the live surface from the child's tools/list
        # (guarded by tests/test_stdio_roundtrip.py). Emitted explicitly so the
        # committed manifest matches what the loader stores.
        "tools": [],
    }


def _required(schema: dict) -> str:
    req = schema.get("required") or []
    props = schema.get("properties") or {}
    if not props:
        return "—"
    parts = []
    for key in props:
        parts.append(f"`{key}`" + ("" if key in req else "?"))
    return ", ".join(parts)


def build_schema_doc() -> str:
    groups = [
        ("Capture", "SCRUM-1400"),
        ("Click", "SCRUM-1401"),
        ("Move / drag / scroll", "SCRUM-1402"),
        ("Keyboard", "SCRUM-1403"),
        ("Clipboard + session", "SCRUM-1404"),
        ("Utility / batch", "SCRUM-1405"),
    ]
    by_ticket: dict[str, list] = {}
    for tool in TOOLS:
        by_ticket.setdefault(OWNER[tool["name"]], []).append(tool)

    # Derived from tools.IMPLEMENTED (surface order) so this prose can never go
    # stale as sibling tickets wire up their bodies.
    implemented = [t["name"] for t in TOOLS if t["name"] in IMPLEMENTED]
    impl_str = ", ".join(f"`{n}`" for n in implemented)

    lines = [
        "# computer-use MCP tool schema",
        "",
        "> Generated from `src/tools.py` by `scripts/build_manifest.py` — do not "
        "edit by hand. This is the authoritative tool surface referenced by "
        "AC4 of SCRUM-1397; it is what the standalone server returns from "
        "`tools/list`.",
        "",
        "Tool names are the **bare canonical** Anthropic computer-use action "
        "ids. Several collide with core SideButton MCP tools (`screenshot`, "
        "`type`, `scroll`, `wait`, `click`); namespacing on aggregation is "
        "deferred to the service engine (SCRUM-1406).",
        "",
        f"**{len(TOOLS)} tools, {len(implemented)} implemented** ({impl_str}). "
        "The rest are declared and return a pending-owner error until their "
        "sibling ticket lands.",
        "",
        "| Tool | Owner | Input | Status |",
        "| --- | --- | --- | --- |",
    ]
    for tool in TOOLS:
        status = "implemented" if tool["name"] in IMPLEMENTED else "declared"
        lines.append(
            f"| `{tool['name']}` | {OWNER[tool['name']]} | "
            f"{_required(tool['inputSchema'])} | {status} |"
        )
    lines += ["", "_`?` marks an optional property._", ""]

    for title, ticket in groups:
        tools = by_ticket.get(ticket, [])
        lines.append(f"## {title} ({ticket})")
        lines.append("")
        for tool in tools:
            lines.append(f"### `{tool['name']}`")
            lines.append("")
            lines.append(tool["description"])
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(tool["inputSchema"], indent=2))
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    (REPO / "plugin.json").write_text(
        json.dumps(build_manifest(), indent=2) + "\n"
    )
    (REPO / "docs").mkdir(exist_ok=True)
    (REPO / "docs" / "computer-use-mcp-tools-schema.md").write_text(
        build_schema_doc() + "\n"
    )
    print("wrote plugin.json and docs/computer-use-mcp-tools-schema.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
