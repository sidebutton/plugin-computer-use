"""Unit tests for the computer.py dispatch base (no desktop required)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class KeyboardActionTest(unittest.TestCase):
    """type/key/hold_key build the right xdotool argv, and hold_key runs
    keydown -> sleep -> keyup, always releasing. No xdotool/desktop needed:
    ``run_xdotool`` is replaced with a recorder."""

    def setUp(self):
        self.c = Computer(display=":10")
        self.calls = []

        def record(args, **_kw):
            self.calls.append(list(args))
            return ""

        self.c.run_xdotool = record

    def test_type_uses_delayed_type_with_separator(self):
        ack = self.c.type_text("hi -x")
        self.assertEqual(self.calls, [["type", "--delay", "12", "--", "hi -x"]])
        self.assertIn("5", ack)  # len("hi -x") == 5

    def test_key_defaults_repeat_to_one(self):
        self.c.press_key("ctrl+s")
        self.assertEqual(self.calls, [["key", "--repeat", 1, "--", "ctrl+s"]])

    def test_key_honours_repeat(self):
        self.c.press_key("Down", repeat=5)
        self.assertEqual(self.calls, [["key", "--repeat", 5, "--", "Down"]])

    def test_hold_key_runs_keydown_sleep_keyup_in_order(self):
        # hold_key only shells out for keydown/keyup; interleave the patched
        # sleep into the same list to assert ordering.
        with mock.patch("time.sleep", lambda d: self.calls.append(("sleep", d))):
            ack = self.c.hold_key("shift", 1.5)
        self.assertEqual(
            self.calls,
            [["keydown", "--", "shift"], ("sleep", 1.5), ["keyup", "--", "shift"]],
        )
        self.assertIn("shift", ack)

    def test_hold_key_releases_even_if_wait_is_interrupted(self):
        def interrupt(_d):
            raise KeyboardInterrupt

        with mock.patch("time.sleep", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.c.hold_key("ctrl", 99)
        # keyup still ran via the finally -> no stranded modifier.
        self.assertEqual(self.calls, [["keydown", "--", "ctrl"], ["keyup", "--", "ctrl"]])

    def test_hold_key_skips_release_when_keydown_fails(self):
        # Nothing was pressed, so no keyup (and no wait) should be issued.
        def fail_keydown(args, **_kw):
            self.calls.append(list(args))
            raise ComputerError("keydown failed")

        self.c.run_xdotool = fail_keydown
        with mock.patch("time.sleep") as slept:
            with self.assertRaises(ComputerError):
                self.c.hold_key("ctrl", 1)
        slept.assert_not_called()
        self.assertEqual(self.calls, [["keydown", "--", "ctrl"]])


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

    def test_implemented_is_screenshot_plus_keyboard_group(self):
        # SCRUM-1397 wired screenshot; SCRUM-1403 adds the keyboard group.
        self.assertEqual(IMPLEMENTED, {"screenshot", "type", "key", "hold_key"})

    def test_key_has_optional_repeat(self):
        # AC4: key gains an additive `repeat` input; the surface stays 24 tools.
        key = next(t for t in TOOLS if t["name"] == "key")
        schema = key["inputSchema"]
        repeat = schema["properties"].get("repeat")
        self.assertIsNotNone(repeat)
        self.assertEqual(repeat["type"], "integer")
        self.assertEqual(repeat["minimum"], 1)
        self.assertNotIn("repeat", schema.get("required", []))  # stays optional
        self.assertEqual(len(TOOL_NAMES), 24)


if __name__ == "__main__":
    unittest.main()
