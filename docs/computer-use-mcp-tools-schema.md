# computer-use MCP tool schema

> Generated from `src/tools.py` by `scripts/build_manifest.py` — do not edit by hand. This is the authoritative tool surface referenced by AC4 of SCRUM-1397; it is what the standalone server returns from `tools/list`.

Tool names are the **bare canonical** Anthropic computer-use action ids. Several collide with core SideButton MCP tools (`screenshot`, `type`, `scroll`, `wait`, `click`); namespacing on aggregation is deferred to the service engine (SCRUM-1406).

**24 tools, 11 implemented** (`screenshot`, `zoom`, `type`, `key`, `hold_key`, `read_clipboard`, `write_clipboard`, `request_access`, `list_granted_applications`, `open_application`, `switch_display`). The rest are declared and return a pending-owner error until their sibling ticket lands.

| Tool | Owner | Input | Status |
| --- | --- | --- | --- |
| `screenshot` | SCRUM-1400 | `save_to_disk`? | implemented |
| `zoom` | SCRUM-1400 | `region`, `save_to_disk`? | implemented |
| `left_click` | SCRUM-1401 | `coordinate`, `text`? | declared |
| `right_click` | SCRUM-1401 | `coordinate`, `text`? | declared |
| `middle_click` | SCRUM-1401 | `coordinate`, `text`? | declared |
| `double_click` | SCRUM-1401 | `coordinate`, `text`? | declared |
| `triple_click` | SCRUM-1401 | `coordinate`, `text`? | declared |
| `mouse_move` | SCRUM-1402 | `coordinate` | declared |
| `left_click_drag` | SCRUM-1402 | `start_coordinate`?, `coordinate` | declared |
| `scroll` | SCRUM-1402 | `coordinate`, `scroll_direction`, `scroll_amount` | declared |
| `left_mouse_down` | SCRUM-1402 | `coordinate`? | declared |
| `left_mouse_up` | SCRUM-1402 | `coordinate`? | declared |
| `type` | SCRUM-1403 | `text` | implemented |
| `key` | SCRUM-1403 | `text`, `repeat`? | implemented |
| `hold_key` | SCRUM-1403 | `text`, `duration` | implemented |
| `read_clipboard` | SCRUM-1404 | — | implemented |
| `write_clipboard` | SCRUM-1404 | `text` | implemented |
| `request_access` | SCRUM-1404 | `apps`, `reason`, `clipboardRead`?, `clipboardWrite`?, `systemKeyCombos`? | implemented |
| `list_granted_applications` | SCRUM-1404 | — | implemented |
| `open_application` | SCRUM-1404 | `app` | implemented |
| `switch_display` | SCRUM-1404 | `display` | implemented |
| `computer_batch` | SCRUM-1405 | `actions` | declared |
| `wait` | SCRUM-1405 | `duration` | declared |
| `cursor_position` | SCRUM-1405 | — | declared |

_`?` marks an optional property._

## Capture (SCRUM-1400)

### `screenshot`

Capture the active display (DISPLAY=:10) and return a base64-encoded PNG image block. The returned image is the coordinate space that subsequent click/move calls refer to. Set save_to_disk to also write the PNG and return its path.

```json
{
  "type": "object",
  "properties": {
    "save_to_disk": {
      "type": "boolean",
      "description": "Save the image to disk and return the saved path in the tool result, so it can be attached to a message for the user. Only set this when you intend to share the image."
    }
  }
}
```

### `zoom`

Take a higher-resolution screenshot of a region of the last full-screen screenshot — use it to inspect small text, button labels, or fine UI detail. Coordinates in later click calls still refer to the full-screen screenshot, never the zoomed image. Read-only.

```json
{
  "type": "object",
  "properties": {
    "region": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 4,
      "maxItems": 4,
      "description": "(x0, y0, x1, y1): rectangle to zoom into, in the coordinate space of the most recent full-screen screenshot. x0,y0 = top-left, x1,y1 = bottom-right."
    },
    "save_to_disk": {
      "type": "boolean",
      "description": "Save the image to disk and return the saved path in the tool result, so it can be attached to a message for the user. Only set this when you intend to share the image."
    }
  },
  "required": [
    "region"
  ]
}
```

## Click (SCRUM-1401)

### `left_click`

Left-click at a coordinate.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    },
    "text": {
      "type": "string",
      "description": "Optional modifier key(s) to hold during the click, e.g. 'ctrl' or 'shift+alt'."
    }
  },
  "required": [
    "coordinate"
  ]
}
```

### `right_click`

Right-click at a coordinate.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    },
    "text": {
      "type": "string",
      "description": "Optional modifier key(s) to hold during the click, e.g. 'ctrl' or 'shift+alt'."
    }
  },
  "required": [
    "coordinate"
  ]
}
```

### `middle_click`

Middle-click at a coordinate.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    },
    "text": {
      "type": "string",
      "description": "Optional modifier key(s) to hold during the click, e.g. 'ctrl' or 'shift+alt'."
    }
  },
  "required": [
    "coordinate"
  ]
}
```

### `double_click`

Double left-click at a coordinate.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    },
    "text": {
      "type": "string",
      "description": "Optional modifier key(s) to hold during the click, e.g. 'ctrl' or 'shift+alt'."
    }
  },
  "required": [
    "coordinate"
  ]
}
```

### `triple_click`

Triple left-click at a coordinate.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    },
    "text": {
      "type": "string",
      "description": "Optional modifier key(s) to hold during the click, e.g. 'ctrl' or 'shift+alt'."
    }
  },
  "required": [
    "coordinate"
  ]
}
```

## Move / drag / scroll (SCRUM-1402)

### `mouse_move`

Move the pointer to a coordinate without clicking.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    }
  },
  "required": [
    "coordinate"
  ]
}
```

### `left_click_drag`

Press the left button at start_coordinate (or the current position) and drag to coordinate before releasing.

```json
{
  "type": "object",
  "properties": {
    "start_coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    },
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    }
  },
  "required": [
    "coordinate"
  ]
}
```

### `scroll`

Scroll in a direction by an amount at a coordinate.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    },
    "scroll_direction": {
      "type": "string",
      "enum": [
        "up",
        "down",
        "left",
        "right"
      ]
    },
    "scroll_amount": {
      "type": "integer",
      "description": "Number of scroll 'clicks'."
    }
  },
  "required": [
    "coordinate",
    "scroll_direction",
    "scroll_amount"
  ]
}
```

### `left_mouse_down`

Press and hold the left mouse button (released later by left_mouse_up). Requires the persistent session.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    }
  }
}
```

### `left_mouse_up`

Release a left mouse button held by left_mouse_down.

```json
{
  "type": "object",
  "properties": {
    "coordinate": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2,
      "description": "[x, y] in the model coordinate space (scaled to the screen)."
    }
  }
}
```

## Keyboard (SCRUM-1403)

### `type`

Type a string of text at the current focus.

```json
{
  "type": "object",
  "properties": {
    "text": {
      "type": "string"
    }
  },
  "required": [
    "text"
  ]
}
```

### `key`

Press a key or chord using xdotool key syntax, e.g. 'Return', 'ctrl+s', 'alt+Tab'.

```json
{
  "type": "object",
  "properties": {
    "text": {
      "type": "string"
    },
    "repeat": {
      "type": "integer",
      "minimum": 1,
      "description": "Number of times to press the key/chord (xdotool --repeat); defaults to 1."
    }
  },
  "required": [
    "text"
  ]
}
```

### `hold_key`

Hold a key (or chord) down for a duration in seconds.

```json
{
  "type": "object",
  "properties": {
    "text": {
      "type": "string"
    },
    "duration": {
      "type": "number",
      "description": "Seconds to hold (may be up to ~100s)."
    }
  },
  "required": [
    "text",
    "duration"
  ]
}
```

## Clipboard + session (SCRUM-1404)

### `read_clipboard`

Read the X clipboard contents (via xclip).

```json
{
  "type": "object",
  "properties": {}
}
```

### `write_clipboard`

Write text to the X clipboard (via xclip).

```json
{
  "type": "object",
  "properties": {
    "text": {
      "type": "string"
    }
  },
  "required": [
    "text"
  ]
}
```

### `request_access`

Request a session grant to control one or more applications; must be called before the other tools. Linux stub: auto-grants the requested apps (no compositor dialog) and returns screenshotFiltering=false. The clipboardRead/clipboardWrite/systemKeyCombos flags are honoured so call shapes match native.

```json
{
  "type": "object",
  "properties": {
    "apps": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "Application display names or bundle identifiers to request access to."
    },
    "reason": {
      "type": "string",
      "description": "One-sentence explanation shown to the user in the native approval dialog (no surface on Linux)."
    },
    "clipboardRead": {
      "type": "boolean",
      "description": "Also request permission to read the clipboard."
    },
    "clipboardWrite": {
      "type": "boolean",
      "description": "Also request permission to write the clipboard."
    },
    "systemKeyCombos": {
      "type": "boolean",
      "description": "Also request permission to send system-level key combos (quit/switch app, lock screen)."
    }
  },
  "required": [
    "apps",
    "reason"
  ]
}
```

### `list_granted_applications`

Return the applications currently in the session allowlist plus the active grant flags and coordinate mode (Linux stub: echoes the auto-granted set). No side effects.

```json
{
  "type": "object",
  "properties": {}
}
```

### `open_application`

Bring an application to the front, launching it if necessary. Linux: best-effort window focus via wmctrl -a / xdotool windowactivate (primary target is the single RDP window); degrades to a no-op when neither binary is present.

```json
{
  "type": "object",
  "properties": {
    "app": {
      "type": "string",
      "description": "Display name or bundle identifier of the application to focus."
    }
  },
  "required": [
    "app"
  ]
}
```

### `switch_display`

Switch the display the session targets, e.g. ':10'.

```json
{
  "type": "object",
  "properties": {
    "display": {
      "type": "string"
    }
  },
  "required": [
    "display"
  ]
}
```

## Utility / batch (SCRUM-1405)

### `computer_batch`

Run a sequence of computer-use actions in order and return one combined result.

```json
{
  "type": "object",
  "properties": {
    "actions": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "name": {
            "type": "string"
          },
          "arguments": {
            "type": "object"
          }
        },
        "required": [
          "name"
        ]
      },
      "description": "Ordered list of {name, arguments} actions."
    }
  },
  "required": [
    "actions"
  ]
}
```

### `wait`

Wait for a duration in seconds (then optionally screenshot).

```json
{
  "type": "object",
  "properties": {
    "duration": {
      "type": "number"
    }
  },
  "required": [
    "duration"
  ]
}
```

### `cursor_position`

Return the current cursor [x, y] in model coordinates.

```json
{
  "type": "object",
  "properties": {}
}
```

