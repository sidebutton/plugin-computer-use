"""Unit tests for the computer.py dispatch base (no desktop required)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from computer import (  # noqa: E402
    Computer,
    ComputerError,
    ScalingSource,
    SingleOwnerLock,
    detect_screenshot_backend,
)
from tools import IMPLEMENTED, OWNER, TOOL_NAMES, TOOLS  # noqa: E402


class ScaleCoordinatesTest(unittest.TestCase):
    def setUp(self):
        # 1920x1080 (~16:9) -> matches FWXGA 1366x768 within the 0.02 ratio band.
        self.c = Computer(display=":10", width=1920, height=1080)

    def test_api_scales_up_to_the_screen(self):
        # A model coordinate at the centre of 1366x768 maps to the screen centre.
        x, y = self.c.scale_coordinates(ScalingSource.API, 683, 384)
        self.assertEqual((x, y), (960, 540))

    def test_computer_scales_down_to_the_model(self):
        # The real bottom-right corner maps to the FWXGA target.
        x, y = self.c.scale_coordinates(ScalingSource.COMPUTER, 1920, 1080)
        self.assertEqual((x, y), (1366, 768))

    def test_round_trip_is_stable(self):
        sx, sy = self.c.scale_coordinates(ScalingSource.API, 500, 400)
        bx, by = self.c.scale_coordinates(ScalingSource.COMPUTER, sx, sy)
        self.assertEqual((bx, by), (500, 400))

    def test_api_out_of_bounds_raises(self):
        with self.assertRaises(ComputerError):
            self.c.scale_coordinates(ScalingSource.API, 5000, 10)

    def test_scaling_disabled_is_identity(self):
        c = Computer(width=1920, height=1080, scaling_enabled=False)
        self.assertEqual(c.scale_coordinates(ScalingSource.API, 10, 20), (10, 20))

    def test_unknown_aspect_ratio_is_identity(self):
        # A square screen matches no scaling target -> coordinates pass through.
        c = Computer(width=1000, height=1000)
        self.assertEqual(c.scale_coordinates(ScalingSource.API, 10, 20), (10, 20))


class XdotoolBuildTest(unittest.TestCase):
    def test_build_xdotool_argv_and_display_env(self):
        c = Computer(display=":10")
        argv, env = c.build_xdotool(["mousemove", 100, 200])
        self.assertEqual(argv, ["xdotool", "mousemove", "100", "200"])
        self.assertEqual(env["DISPLAY"], ":10")

    def test_display_defaults_to_inherited_then_10(self):
        prev = os.environ.pop("DISPLAY", None)
        try:
            self.assertEqual(Computer().display, ":10")
            os.environ["DISPLAY"] = ":42"
            self.assertEqual(Computer().display, ":42")
        finally:
            if prev is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = prev


class SingleOwnerLockTest(unittest.TestCase):
    def test_second_owner_is_rejected(self):
        path = os.path.join(tempfile.gettempdir(), f"cu-lock-{os.getpid()}.lock")
        first = SingleOwnerLock(path).acquire()
        try:
            with self.assertRaises(ComputerError):
                SingleOwnerLock(path).acquire()
        finally:
            first.release()
        # After release the lock is reusable.
        SingleOwnerLock(path).acquire().release()


class BackendDetectTest(unittest.TestCase):
    def test_a_backend_is_available_on_this_host(self):
        # The runner ships ImageMagick even when scrot/gnome-screenshot are absent.
        self.assertIn(
            detect_screenshot_backend(),
            {"gnome-screenshot", "scrot", "import"},
        )


class ToolSurfaceTest(unittest.TestCase):
    def test_surface_is_the_declared_groups(self):
        # The named surface from the work plan (capture/click/move/keyboard/
        # clipboard/utility). Guards against accidental add/drop.
        expected = {
            "screenshot", "zoom",
            "left_click", "right_click", "middle_click", "double_click",
            "triple_click",
            "mouse_move", "left_click_drag", "scroll", "left_mouse_down",
            "left_mouse_up",
            "type", "key", "hold_key",
            "read_clipboard", "write_clipboard", "request_access",
            "list_granted_applications", "open_application", "switch_display",
            "computer_batch", "wait", "cursor_position",
        }
        self.assertEqual(set(TOOL_NAMES), expected)
        self.assertEqual(len(TOOL_NAMES), 24)  # the epic's 24-tool surface

    def test_every_tool_has_a_valid_input_schema(self):
        for tool in TOOLS:
            for key in ("name", "description", "inputSchema"):
                self.assertIn(key, tool)
            schema = tool["inputSchema"]
            self.assertEqual(schema.get("type"), "object")
            self.assertIsInstance(schema.get("properties"), dict)

    def test_names_are_unique(self):
        self.assertEqual(len(TOOL_NAMES), len(set(TOOL_NAMES)))

    def test_every_tool_has_an_owner_ticket(self):
        for name in TOOL_NAMES:
            self.assertIn(name, OWNER)

    def test_implemented_is_screenshot_plus_clipboard_session(self):
        # screenshot (SCRUM-1397) + the clipboard/session group (SCRUM-1404).
        self.assertEqual(
            IMPLEMENTED,
            {
                "screenshot",
                "read_clipboard",
                "write_clipboard",
                "request_access",
                "list_granted_applications",
                "open_application",
                "switch_display",
            },
        )

    def test_request_access_schema_matches_native(self):
        # AC6: reconciled to the-assistant/docs/computer-use-mcp-tools-schema.md.
        ra = next(t for t in TOOLS if t["name"] == "request_access")
        props = ra["inputSchema"]["properties"]
        self.assertEqual(ra["inputSchema"]["required"], ["apps", "reason"])
        self.assertEqual(props["apps"]["type"], "array")
        for flag in ("clipboardRead", "clipboardWrite", "systemKeyCombos"):
            self.assertEqual(props[flag]["type"], "boolean")
        self.assertNotIn("applications", props)  # the old, non-native key is gone

    def test_open_application_schema_uses_app(self):
        oa = next(t for t in TOOLS if t["name"] == "open_application")
        self.assertEqual(oa["inputSchema"]["required"], ["app"])
        self.assertIn("app", oa["inputSchema"]["properties"])
        self.assertNotIn("name", oa["inputSchema"]["properties"])


class GrantStateTest(unittest.TestCase):
    """request_access / list_granted_applications, no desktop required."""

    def setUp(self):
        self.c = Computer(display=":10")

    def test_request_access_auto_grants_and_flips_flags(self):
        out = self.c.request_access(
            apps=["Slack", "Calendar"], reason="demo", clipboardRead=True
        )
        self.assertEqual(out["grantedApplications"], ["Calendar", "Slack"])
        self.assertEqual(out["deniedApplications"], [])
        self.assertIs(out["screenshotFiltering"], False)
        self.assertTrue(out["clipboardRead"])
        self.assertFalse(out["clipboardWrite"])

    def test_grants_are_additive_across_calls(self):
        self.c.request_access(apps=["Slack"], reason="a", clipboardWrite=True)
        out = self.c.request_access(apps=["Finder"], reason="b")
        self.assertEqual(out["grantedApplications"], ["Finder", "Slack"])
        self.assertTrue(out["clipboardWrite"])  # earlier grant persists

    def test_list_granted_applications_echoes_state(self):
        self.c.request_access(apps=["Slack"], reason="x", systemKeyCombos=True)
        out = self.c.list_granted_applications()
        self.assertEqual(out["applications"], ["Slack"])
        self.assertTrue(out["systemKeyCombos"])
        self.assertEqual(out["coordinateMode"], "screenshot")

    def test_request_access_tolerates_missing_apps(self):
        # Degrade gracefully rather than crash on a thin call.
        out = self.c.request_access()
        self.assertEqual(out["grantedApplications"], [])


class ClipboardGateTest(unittest.TestCase):
    def setUp(self):
        self.c = Computer(display=":10")

    def test_read_without_grant_raises(self):
        with self.assertRaises(ComputerError):
            self.c.read_clipboard()

    def test_write_without_grant_raises(self):
        with self.assertRaises(ComputerError):
            self.c.write_clipboard("hi")

    def test_xclip_argv_is_built_correctly(self):
        rargv, renv = self.c.build_xclip(["-selection", "clipboard", "-o"])
        self.assertEqual(rargv, ["xclip", "-selection", "clipboard", "-o"])
        self.assertEqual(renv["DISPLAY"], ":10")
        wargv, _ = self.c.build_xclip(["-selection", "clipboard", "-i"])
        self.assertEqual(wargv, ["xclip", "-selection", "clipboard", "-i"])


class OpenApplicationAndDisplayTest(unittest.TestCase):
    def setUp(self):
        self.c = Computer(display=":10")

    def test_wmctrl_argv_is_built_correctly(self):
        argv, env = self.c.build_wmctrl(["-a", "Firefox"])
        self.assertEqual(argv, ["wmctrl", "-a", "Firefox"])
        self.assertEqual(env["DISPLAY"], ":10")

    def test_open_application_degrades_without_binaries(self):
        # wmctrl/xdotool are absent on the runner image -> graceful no-op, never
        # a crash. (When a binary is present this returns focused True/False.)
        out = self.c.open_application("Firefox")
        self.assertEqual(out["app"], "Firefox")
        self.assertIn("focused", out)
        self.assertIsInstance(out["focused"], bool)

    def test_switch_display_is_a_noop_returning_current(self):
        out = self.c.switch_display("auto")
        self.assertEqual(out["display"], ":10")
        self.assertFalse(out["switched"])


if __name__ == "__main__":
    unittest.main()
