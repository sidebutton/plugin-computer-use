# computer-use MCP tool schema

> Generated from `src/tools.py` by `scripts/build_manifest.py` — do not edit by hand. This is the authoritative tool surface referenced by AC4 of SCRUM-1397; it is what the standalone server returns from `tools/list`.

Tool names are the **bare canonical** Anthropic computer-use action ids. Several collide with core SideButton MCP tools (`screenshot`, `type`, `scroll`, `wait`, `click`); namespacing on aggregation is deferred to the service engine (SCRUM-1406).

**22 tools.** Only `screenshot` is implemented in SCRUM-1397 (the proof action); the rest are declared and return a pending-owner error until their sibling ticket lands.

| Tool | Owner | Input | Status |
| --- | --- | --- | --- |
| `screenshot` | SCRUM-1400 | — | implemented |
| `zoom` | SCRUM-1400 | `region` | declared |
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
| `type` | SCRUM-1403 | `text` | declared |
| `key` | SCRUM-1403 | `text` | declared |
| `hold_key` | SCRUM-1403 | `text`, `duration` | declared |
| `clipboard` | SCRUM-1404 | `action`, `text`? | declared |
| `request_access` | SCRUM-1404 | `scope`? | declared |
| `open_application` | SCRUM-1404 | `name` | declared |
| `switch_display` | SCRUM-1404 | `display` | declared |
| `computer_batch` | SCRUM-1405 | `actions` | declared |
| `wait` | SCRUM-1405 | `duration` | declared |
| `cursor_position` | SCRUM-1405 | — | declared |

_`?` marks an optional property._

## Capture (SCRUM-1400)

### `screenshot`

Capture the active display (DISPLAY=:10) and return a base64-encoded PNG. Implemented in SCRUM-1397 as the proof action.

```json
{
  "type": "object",
  "properties": {}
}
```

### `zoom`

Capture and return a magnified PNG of a sub-region of the screen.

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
      "description": "[x, y, width, height] region to magnify."
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

### `clipboard`

Get or set the X clipboard contents (via xclip).

```json
{
  "type": "object",
  "properties": {
    "action": {
      "type": "string",
      "enum": [
        "get",
        "set"
      ]
    },
    "text": {
      "type": "string",
      "description": "Text to write when action='set'."
    }
  },
  "required": [
    "action"
  ]
}
```

### `request_access`

Request a session grant to drive the desktop (session stub; the grant model lands with the service engine).

```json
{
  "type": "object",
  "properties": {
    "scope": {
      "type": "string",
      "description": "Requested access scope."
    }
  }
}
```

### `open_application`

Launch a desktop application by name.

```json
{
  "type": "object",
  "properties": {
    "name": {
      "type": "string"
    }
  },
  "required": [
    "name"
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

