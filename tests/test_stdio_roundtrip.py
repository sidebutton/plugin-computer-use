"""End-to-end stdio MCP round-trip against a spawned server.py.

Covers the SCRUM-1397 acceptance criteria:
  AC1  initialize handshake over stdin/stdout
  AC2  tools/list returns the full declared surface
  AC3  tools/call screenshot returns a base64 PNG (needs a display; skipped when
       DISPLAY is unset — run via ./run_tests.sh which wraps xvfb-run)
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "src" / "server.py"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

sys.path.insert(0, str(REPO / "src"))
from tools import TOOL_NAMES  # noqa: E402


class StdioServer:
    """Spawn server.py and exchange newline-delimited JSON-RPC messages."""

    def __init__(self):
        env = dict(os.environ)
        # Isolate the single-owner lock so the test never collides with a real
        # session, and skip the post-action settle delay.
        env["CU_LOCK_PATH"] = os.path.join(
            tempfile.gettempdir(), f"cu-test-lock-{os.getpid()}.lock"
        )
        env["CU_SCREENSHOT_DELAY"] = "0"
        # Saved captures land in an isolated dir we remove in close().
        self.save_dir = tempfile.mkdtemp(prefix=f"cu-test-save-{os.getpid()}-")
        env["CU_SAVE_DIR"] = self.save_dir
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )

    def request(self, method, params=None, mid=1):
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(
                "server produced no response; stderr=\n"
                + self.proc.stderr.read()
            )
        return json.loads(line)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        finally:
            for stream in (self.proc.stdout, self.proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
            shutil.rmtree(self.save_dir, ignore_errors=True)


class StdioRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.server = StdioServer()

    def tearDown(self):
        self.server.close()

    def test_ac1_initialize_handshake(self):
        resp = self.server.request(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}},
        )
        self.assertEqual(resp["id"], 1)
        self.assertNotIn("error", resp)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "computer-use")
        self.assertIn("tools", resp["result"]["capabilities"])
        # The initialized notification must not draw a response.
        self.server.notify("notifications/initialized")

    def test_ac2_tools_list_returns_full_surface(self):
        resp = self.server.request("tools/list", mid=2)
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, TOOL_NAMES)
        self.assertIn("screenshot", names)
        # Each tool advertises a valid object inputSchema.
        for tool in resp["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    @unittest.skipUnless(
        os.environ.get("DISPLAY"), "no DISPLAY (run via ./run_tests.sh / xvfb-run)"
    )
    def test_ac3_screenshot_round_trips_a_png(self):
        resp = self.server.request(
            "tools/call", {"name": "screenshot", "arguments": {}}, mid=3
        )
        result = resp["result"]
        self.assertFalse(result.get("isError"), msg=str(result))
        block = result["content"][0]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["mimeType"], "image/png")
        raw = base64.b64decode(block["data"])
        self.assertTrue(raw.startswith(PNG_MAGIC), "payload is not a PNG")
        # Parse the IHDR chunk to confirm a real raster of non-zero size. (A byte
        # threshold would be flaky: a blank headless Xvfb root compresses to a
        # tiny PNG, while the live :10 desktop is hundreds of KB.)
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)

    @unittest.skipUnless(
        os.environ.get("DISPLAY"), "no DISPLAY (run via ./run_tests.sh / xvfb-run)"
    )
    def test_ac1_screenshot_save_to_disk_returns_a_path_block(self):
        resp = self.server.request(
            "tools/call",
            {"name": "screenshot", "arguments": {"save_to_disk": True}},
            mid=7,
        )
        result = resp["result"]
        self.assertFalse(result.get("isError"), msg=str(result))
        blocks = result["content"]
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["mimeType"], "image/png")
        # A text block beside the image carries the saved path; the file exists.
        texts = [b["text"] for b in blocks if b["type"] == "text"]
        self.assertTrue(texts, msg=str(blocks))
        path = texts[0].split("Saved to disk:", 1)[1].strip()
        self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(path.startswith(self.server.save_dir), path)

    @unittest.skipUnless(
        os.environ.get("DISPLAY"), "no DISPLAY (run via ./run_tests.sh / xvfb-run)"
    )
    def test_ac2_zoom_round_trips_a_png_and_establishes_session(self):
        # Called on a fresh server with no prior screenshot -> lazy session.
        resp = self.server.request(
            "tools/call",
            {"name": "zoom", "arguments": {"region": [100, 100, 400, 300]}},
            mid=8,
        )
        result = resp["result"]
        self.assertFalse(result.get("isError"), msg=str(result))
        block = result["content"][0]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["mimeType"], "image/png")
        raw = base64.b64decode(block["data"])
        self.assertTrue(raw.startswith(PNG_MAGIC), "payload is not a PNG")
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)

    def test_request_access_then_list_granted_reflects_it(self):
        resp = self.server.request(
            "tools/call",
            {
                "name": "request_access",
                "arguments": {
                    "apps": ["Firefox"],
                    "reason": "drive the browser",
                    "clipboardRead": True,
                },
            },
            mid=10,
        )
        granted = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(granted["grantedApplications"], ["Firefox"])
        self.assertIs(granted["screenshotFiltering"], False)
        self.assertTrue(granted["clipboardRead"])
        # The grant persists in the long-lived session.
        resp = self.server.request(
            "tools/call",
            {"name": "list_granted_applications", "arguments": {}},
            mid=11,
        )
        listed = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(listed["applications"], ["Firefox"])
        self.assertTrue(listed["clipboardRead"])

    def test_read_clipboard_without_grant_is_error(self):
        resp = self.server.request(
            "tools/call", {"name": "read_clipboard", "arguments": {}}, mid=12
        )
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("clipboardRead", resp["result"]["content"][0]["text"])

    @unittest.skipUnless(
        os.environ.get("DISPLAY") and shutil.which("xclip"),
        "needs xclip + a DISPLAY for the X clipboard round-trip",
    )
    def test_clipboard_write_then_read_round_trips(self):
        payload = "sidebutton clipboard round-trip 1404"
        # Grant read+write, then write -> read back.
        self.server.request(
            "tools/call",
            {
                "name": "request_access",
                "arguments": {
                    "apps": ["xterm"],
                    "reason": "clipboard test",
                    "clipboardRead": True,
                    "clipboardWrite": True,
                },
            },
            mid=13,
        )
        wresp = self.server.request(
            "tools/call",
            {"name": "write_clipboard", "arguments": {"text": payload}},
            mid=14,
        )
        self.assertFalse(wresp["result"]["isError"], msg=str(wresp))
        rresp = self.server.request(
            "tools/call", {"name": "read_clipboard", "arguments": {}}, mid=15
        )
        self.assertFalse(rresp["result"]["isError"], msg=str(rresp))
        self.assertEqual(rresp["result"]["content"][0]["text"], payload)

    def test_switch_display_is_a_noop(self):
        resp = self.server.request(
            "tools/call",
            {"name": "switch_display", "arguments": {"display": "auto"}},
            mid=16,
        )
        out = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(resp["result"]["isError"])
        self.assertFalse(out["switched"])
        self.assertTrue(out["display"])  # reports the current display

    def test_open_application_degrades_gracefully(self):
        # wmctrl/xdotool absent on this image -> a non-error best-effort result.
        resp = self.server.request(
            "tools/call",
            {"name": "open_application", "arguments": {"app": "Firefox"}},
            mid=17,
        )
        out = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(out["app"], "Firefox")
        self.assertIn("focused", out)

    def _assert_keyboard_dispatched(self, resp):
        """The call reached the keyboard handler, not the pending-owner stub.

        With ``xdotool`` installed the action succeeds (``isError: false``);
        without it the dispatch base reports the missing binary. Either way the
        tool is wired (it must NOT return the 'declared but not implemented'
        sibling error), so the assertion holds on a runner image with or without
        xdotool (it is a declared system_dep).
        """
        result = resp["result"]
        text = result["content"][0].get("text", "")
        self.assertNotIn("declared but not implemented", text)
        if shutil.which("xdotool"):
            self.assertFalse(result.get("isError"), msg=str(result))
        else:
            self.assertTrue(result.get("isError"))
            self.assertIn("xdotool is not installed", text)

    def test_type_round_trips(self):
        resp = self.server.request(
            "tools/call", {"name": "type", "arguments": {"text": "hello"}}, mid=7
        )
        self._assert_keyboard_dispatched(resp)

    def test_key_round_trips_with_repeat(self):
        resp = self.server.request(
            "tools/call",
            {"name": "key", "arguments": {"text": "ctrl+a", "repeat": 2}},
            mid=8,
        )
        self._assert_keyboard_dispatched(resp)

    def test_hold_key_round_trips(self):
        # Short duration so the live path (xdotool present) stays fast.
        resp = self.server.request(
            "tools/call",
            {"name": "hold_key", "arguments": {"text": "shift", "duration": 0.05}},
            mid=9,
        )
        self._assert_keyboard_dispatched(resp)

    def test_click_dispatches_not_pending_owner(self):
        # No screenshot taken yet, so the look-before-click guard (AC3) fires.
        # That proves the click body ran rather than the pending-owner stub —
        # and it holds with or without xdotool, since the guard precedes any
        # xdotool call (xdotool is a declared system_dep, absent on this image).
        resp = self.server.request(
            "tools/call",
            {"name": "left_click", "arguments": {"coordinate": [10, 10]}},
            mid=18,
        )
        result = resp["result"]
        text = result["content"][0].get("text", "")
        self.assertNotIn("declared but not implemented", text)
        self.assertTrue(result.get("isError"))
        self.assertIn("no screenshot session", text)

    @unittest.skipUnless(
        os.environ.get("DISPLAY") and shutil.which("xdotool"),
        "needs xdotool + a DISPLAY for a live click",
    )
    def test_left_click_live_after_screenshot(self):
        # screenshot establishes the coordinate session; the click then lands.
        self.server.request(
            "tools/call", {"name": "screenshot", "arguments": {}}, mid=19
        )
        resp = self.server.request(
            "tools/call",
            {"name": "left_click", "arguments": {"coordinate": [10, 10]}},
            mid=20,
        )
        self.assertFalse(resp["result"].get("isError"), msg=str(resp["result"]))
        self.assertIn("left_click", resp["result"]["content"][0]["text"])

    def test_pending_tool_returns_owner_error(self):
        # mouse_move (SCRUM-1402) is still a declared-only sibling; left_click is
        # now implemented (SCRUM-1401), so it no longer returns the owner stub.
        resp = self.server.request(
            "tools/call",
            {"name": "mouse_move", "arguments": {"coordinate": [10, 10]}},
            mid=4,
        )
        result = resp["result"]
        self.assertTrue(result["isError"])
        self.assertIn("SCRUM-1402", result["content"][0]["text"])

    def test_unknown_tool_is_an_error(self):
        resp = self.server.request(
            "tools/call", {"name": "nope", "arguments": {}}, mid=5
        )
        self.assertTrue(resp["result"]["isError"])

    def test_unknown_method_is_jsonrpc_error(self):
        resp = self.server.request("does/not/exist", mid=6)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_malformed_line_yields_parse_error(self):
        self.server.proc.stdin.write("{ not json\n")
        self.server.proc.stdin.flush()
        resp = json.loads(self.server.proc.stdout.readline())
        self.assertEqual(resp["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
