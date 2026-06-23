# plugin-computer-use

A **persistent stdio MCP server** that exposes the Anthropic
[computer-use](https://docs.anthropic.com/en/docs/build-with-claude/computer-use)
action surface (screenshot, click, move, keyboard, clipboard, batch) against the
SideButton agent desktop on `DISPLAY=:10`.

This repo is the **scaffold + dispatch core** for the Computer Use epic
([SCRUM-1399](https://aictpo.atlassian.net/browse/SCRUM-1399)). It is delivered
by [**SCRUM-1397**](https://aictpo.atlassian.net/browse/SCRUM-1397):

- the long-lived stdio MCP server loop (`initialize` / `tools/list` / `tools/call`),
- the ported `computer.py` **dispatch base** (DISPLAY targeting, screenshot →
  base64 PNG, coordinate scaling, single-owner lock, xdotool runner),
- the full tool surface **declared** so `tools/list` returns it,
- `screenshot` **wired end-to-end** as the proof action.

The individual tool **bodies** land in sibling tickets (SCRUM-1400…1405) and
hosting this as a `runtime: "service"` plugin is SCRUM-1406.

## Why a persistent server

The current SideButton plugin model
([the-assistant `packages/server/src/plugins`](https://github.com/maxsv0/the-assistant))
spawns a **fresh, stateless** handler process per `tools/call` and SIGKILLs it at
a 30s timeout. That cannot host the computer-use surface, which needs cross-call
state: a held mouse button (`left_mouse_down` … `left_mouse_up`), the
screenshot→coordinate session, session grants, and holds up to ~100s. So this is
a **single, long-lived child process** that speaks MCP over stdio.

## Tool surface

24 tools, grouped by the sibling ticket that owns each body. The **capture group**
(`screenshot`, `zoom`, SCRUM-1400), the **click group** (`left_click` / `right_click`
/ `middle_click` / `double_click` / `triple_click`, SCRUM-1401), the **keyboard
group** (`type` / `key` / `hold_key`, SCRUM-1403), and the **clipboard + session
group** (SCRUM-1404) are implemented; the rest are declared and return a clear
pending-owner error until their ticket lands. Full input schemas:
[`docs/computer-use-mcp-tools-schema.md`](docs/computer-use-mcp-tools-schema.md).

| Group | Ticket | Tools |
| --- | --- | --- |
| capture | SCRUM-1400 | `screenshot` ✅, `zoom` ✅ |
| click | SCRUM-1401 | `left_click` ✅, `right_click` ✅, `middle_click` ✅, `double_click` ✅, `triple_click` ✅ |
| move / drag / scroll | SCRUM-1402 | `mouse_move`, `left_click_drag`, `scroll`, `left_mouse_down`, `left_mouse_up` |
| keyboard | SCRUM-1403 | `type` ✅, `key` ✅, `hold_key` ✅ |
| clipboard + session | SCRUM-1404 | `read_clipboard` ✅, `write_clipboard` ✅, `request_access` ✅, `list_granted_applications` ✅, `open_application` ✅, `switch_display` ✅ |
| utility / batch | SCRUM-1405 | `computer_batch`, `wait`, `cursor_position` |

### Clipboard + session behaviour (SCRUM-1404)

The macOS session/permission model has no XFCE/Xvfb equivalent, so these
**degrade gracefully instead of erroring** — keeping cross-runner (macOS-authored)
skills working — while honouring the native grant flags so call shapes match:

- `request_access` **auto-grants** the requested `apps` (no compositor dialog),
  records the `clipboardRead` / `clipboardWrite` / `systemKeyCombos` flags
  (additive across calls), and returns `screenshotFiltering: false`.
- `list_granted_applications` echoes the allowlist + active grant flags.
- `read_clipboard` / `write_clipboard` shell out to
  `xclip -selection clipboard`, **gated** on the `clipboardRead` / `clipboardWrite`
  grants (a call without the grant returns an `isError` result, matching native).
- `open_application` is **best-effort** window focus (`wmctrl -a`, then
  `xdotool search --name … windowactivate`); the primary target is the single
  RDP window. With neither binary installed it returns a non-error no-op note.
- `switch_display` is a **no-op** on the single Xvfb `:10` and reports the
  current display (accepts `"auto"`).

> **Surface count.** This is the **24-tool** surface the epic
> ([SCRUM-1399](https://aictpo.atlassian.net/browse/SCRUM-1399)) specifies. The
> clipboard + session group follows the explicit enumeration in
> [SCRUM-1404](https://aictpo.atlassian.net/browse/SCRUM-1404) (`read_clipboard` /
> `write_clipboard` split + `list_granted_applications`), which is the 2-tool
> delta over the work plan's interim count of 22. `src/tools.py` is the single
> source of truth; `docs/computer-use-mcp-tools-schema.md` (AC4) is generated
> from it.

> **Bare names + collisions.** Names are the canonical Anthropic action ids.
> `screenshot`, `type`, `scroll`, `wait`, `click` collide with **core** SideButton
> MCP tools, and the current loader drops the *entire* plugin on any collision.
> That is fine standalone (this server owns its namespace); **namespacing on
> aggregation is deferred to SCRUM-1406** (recommended: bare names in the child,
> prefix/slug-namespace on the host).

## Layout

```
plugin-computer-use/
├── plugin.json        # generated service-plugin manifest (proposes runtime:"service")
├── src/
│   ├── server.py      # stdio MCP loop: initialize / tools/list / tools/call
│   ├── computer.py    # dispatch base (ported computer.py)
│   └── tools.py       # canonical tool surface (single source of truth)
├── scripts/
│   └── build_manifest.py   # regenerates plugin.json + the schema doc from tools.py
├── tests/             # unittest: dispatch-base unit + stdio round-trip + manifest
├── docs/
│   └── computer-use-mcp-tools-schema.md   # generated; the AC4 schema doc
├── run_tests.sh       # runs the suite (xvfb-wrapped when no DISPLAY)
├── pyproject.toml     # dependency-free, python>=3.10
├── README.md  LICENSE  .gitignore
```

## Run it standalone

```bash
# speak MCP by hand (newline-delimited JSON-RPC):
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"screenshot","arguments":{"save_to_disk":true}}}' \
  '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"zoom","arguments":{"region":[600,300,900,500]}}}' \
  '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"type","arguments":{"text":"hello"}}}' \
  '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"key","arguments":{"text":"ctrl+a","repeat":1}}}' \
  '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"hold_key","arguments":{"text":"shift","duration":2}}}' \
  '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"left_click","arguments":{"coordinate":[600,300],"text":"ctrl"}}}' \
  | DISPLAY=:10 python3 src/server.py
```

`initialize` returns the handshake, `tools/list` the 24-tool surface, the
`screenshot` call a base64 PNG image block (plus a `Saved to disk: <path>` text
block when `save_to_disk` is set), and `zoom` a magnified PNG of the region. The
keyboard and click calls each return a short text ack (`isError:false`); they need
`xdotool` on `PATH`. The `left_click` maps its `[600, 300]` against the `id:3`
screenshot's coordinate session — a click before any screenshot returns a clear
`no screenshot session yet` error instead of clicking blind.

## Capture & coordinates

`screenshot` captures `DISPLAY=:10` and, when the measured size matches a model
resolution, downscales it (on the live 1920×1080 `:10` it returns **1366×768**).
Each capture records a **screenshot → coordinate session**: the *measured* device
geometry and the returned image geometry. Coordinates the model returns are in
**image space** (relative to the last screenshot); the server maps them back to
device pixels via `Computer.to_device(x, y)` — the foundation the click/move
siblings (SCRUM-1401/1402) consume. Both the downscale and the coordinate mapping
are derived from the *same measured geometry*, so they can never use different
bases (the wrong-pixel-click failure mode).

`zoom` takes `region: (x0, y0, x1, y1)` in image space, maps it to a device rect,
and crops it from a **fresh full-resolution** capture — genuine magnification, not
an upscale of the downscaled screenshot. It is **read-only**: it never moves the
click-coordinate origin (clicks still refer to the last `screenshot`). If no
screenshot has been taken yet, `zoom` establishes the session lazily.

True 1:1 (no downscale) would require pinning `:10` / the RDP window to a
model-friendly size — that is provisioning ([SCRUM-1396](https://aictpo.atlassian.net/browse/SCRUM-1396)),
out of scope here.

### Click group (SCRUM-1401)

Pointer clicks at a **screenshot-session coordinate**. The `[x, y]` `coordinate`
is image space (relative to the last `screenshot`) and is mapped to device pixels
via `Computer.to_device` — a click **before any screenshot** returns a clear
`no screenshot session yet` error (look before you click). Optional `text`
modifier(s) (`'ctrl'`, `'shift+alt'`, …) are held for the click and **always
released** (`keyup` in a `finally`, the same guarantee as `hold_key`).

| Tool | xdotool | Button |
| --- | --- | --- |
| `left_click` | `mousemove --sync <dx> <dy> click 1` | left (1) |
| `right_click` | `mousemove --sync <dx> <dy> click 3` | right (3) |
| `middle_click` | `mousemove --sync <dx> <dy> click 2` | middle (2) |
| `double_click` | `mousemove --sync <dx> <dy> click --repeat 2 --delay 100 1` | left ×2 |
| `triple_click` | `mousemove --sync <dx> <dy> click --repeat 3 --delay 100 1` | left ×3 |

With a modifier the click is wrapped in `keydown -- <text>` → click → `keyup --
<text>`. On-screen pixel accuracy is validated live in
[SCRUM-1408](https://aictpo.atlassian.net/browse/SCRUM-1408) (`xdotool` is absent
on the current runner image, so the unit tests assert the device-pixel argv).

### Keyboard group (SCRUM-1403)

| Tool | xdotool | Notes |
| --- | --- | --- |
| `type` | `xdotool type --delay 12 -- <text>` | types `text` at the current focus |
| `key` | `xdotool key --repeat <repeat> -- <text>` | chords, e.g. `ctrl+s`; optional `repeat` (default 1) |
| `hold_key` | `keydown -- <text>` → `sleep <duration>` → `keyup -- <text>` | the hold runs in the persistent server (Python `time.sleep`), so durations up to ~100s do not trip the per-call subprocess timeout; `keyup` runs in a `finally` so a held key/modifier is always released |

## Test

```bash
./run_tests.sh          # uses $DISPLAY if set, else wraps in xvfb-run
# or directly:
DISPLAY=:10 python3 -m unittest discover -s tests -v
```

- `tests/test_dispatch_base.py` — coordinate-scaling math, the screenshot →
  coordinate session + `to_device` mapping, the measured-basis downscale target,
  `zoom` region validation + region→device-rect math, xdotool command
  construction, single-owner lock, screenshot-backend detection, surface shape,
  plus live `screenshot`/`zoom` + `save_to_disk` (DISPLAY-gated).
- `tests/test_stdio_roundtrip.py` — `initialize` → `tools/list` → `tools/call`
  `screenshot` (incl. `save_to_disk` path block) and `zoom` over a spawned server,
  plus error paths.
- `tests/test_manifest.py` — `plugin.json` + schema doc are present and in sync
  with `src/tools.py`.

The screenshot round-trip needs an X display; `run_tests.sh` provides one via
`xvfb-run` when `$DISPLAY` is unset, so AC3 still exercises in headless CI.

## System dependencies

System packages (apt), not pip — the plugin install copies no `node_modules`/venv
and runs no build step, so the server is **stdlib-only** and shells out to:

| Tool | Used for | Notes |
| --- | --- | --- |
| a screenshot backend | `screenshot` | `gnome-screenshot` **or** `scrot` **or** ImageMagick (`import`/`convert`). The runner ships ImageMagick. |
| `xdotool` | pointer/keyboard actions; `open_application` fallback | required by the click/move/keyboard groups (siblings); absent on the runner image. |
| `xclip` | `read_clipboard` / `write_clipboard` | already on the runner; grant-gated. |
| `wmctrl` | `open_application` window focus | best-effort; `open_application` degrades to a no-op when absent. |

`scrot` and `gnome-screenshot` are **absent** on the runner image, so the
screenshot backend falls through to ImageMagick `import -window root` (verified
on `DISPLAY=:10`). When SCRUM-1407 adds this plugin to the agent-runners catalog,
declare `xdotool`, a screenshot backend, and `xclip` in its `system_deps`.

## DISPLAY and single-owner

- The server targets the **inherited `$DISPLAY`**, defaulting to `:10` (the
  runner desktop). It never hardcodes a display — the screen-record plugin's bug
  was capturing a non-existent `:1.0`.
- It takes a process-lifetime **single-owner lock** (`flock`,
  `/tmp/sidebutton-computer-use.lock`, override with `CU_LOCK_PATH`) so only one
  session drives the shared pointer/keyboard; a second instance exits non-zero.

## Service-manifest contract (SCRUM-1406)

`plugin.json` targets the merged `runtime: "service"` tier: the SideButton server
keeps the child alive, discovers its tools via `tools/list`, and forwards
`tools/call` to it.

```jsonc
{
  "name": "computer-use",
  "runtime": "service",
  "service": {
    "command": "python3 src/server.py",  // non-empty string; the engine splits on
                                          // whitespace and spawns with cwd=plugin dir
    "toolNamespace": "computer_use",      // tools surface as computer_use_<tool>
    "tools": {                            // per-tool timeout overrides (ms)
      "hold_key": { "timeoutMs": 120000 },
      "wait":     { "timeoutMs": 120000 }
    }
  },
  "tools": []                             // service plugins declare no static tools
}
```

> The loader (`the-assistant` `packages/server/src/plugins/loader.ts`) recognizes
> only `command` / `timeoutMs` / `toolNamespace` / `tools` under `service`, and
> **hard-rejects** the manifest unless `command` is a non-empty **string** — an
> array fails validation and the plugin never loads. Tools are discovered live,
> so the top-level `tools` array is normalized to `[]`. This repo owns only
> `plugin.json`; the agent-runners catalog entry + `system_deps` are SCRUM-1407.

## Configuration (env)

| Var | Default | Purpose |
| --- | --- | --- |
| `DISPLAY` | `:10` | target X display |
| `CU_WIDTH` / `CU_HEIGHT` | `1920` / `1080` | screen size for coordinate scaling |
| `CU_SCREENSHOT_DELAY` | `2.0` | post-action settle before a screenshot |
| `CU_LOCK_PATH` | `/tmp/sidebutton-computer-use.lock` | single-owner lock file |
| `CU_SAVE_DIR` | `/tmp/sidebutton-computer-use/` | where `save_to_disk` writes shareable PNGs (host-pruned; saved files are not auto-deleted) |

## License

MIT © 2026 SideButton
