"""Unit tests for the computer.py dispatch base (no desktop required)."""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from computer import (  # noqa: E402
    CaptureSession,
    Computer,
    ComputerError,
    ScalingSource,
    SingleOwnerLock,
    _png_size,
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


class ClickActionTest(unittest.TestCase):
    """click() maps the coordinate via the screenshot session, builds the right
    xdotool argv per button/count, and holds optional modifiers for the click —
    always releasing. No xdotool/desktop needed: ``run_xdotool`` is a recorder and
    the session is set directly."""

    def setUp(self):
        self.c = Computer(display=":10")
        # An identity session (image == device) so the recorded argv carries the
        # input coordinate verbatim; the scaling mapping is exercised separately.
        self.c.last_capture = CaptureSession(1366, 768, 1366, 768)
        self.calls = []

        def record(args, **_kw):
            self.calls.append(list(args))
            return ""

        self.c.run_xdotool = record

    def test_left_click_moves_then_clicks_button_1(self):
        ack = self.c.click([100, 200])
        self.assertEqual(self.calls, [["mousemove", "--sync", 100, 200, "click", 1]])
        self.assertIn("left_click", ack)
        self.assertIn("(100, 200)", ack)

    def test_right_and_middle_use_x11_button_numbers(self):
        self.c.click([10, 20], button="right")
        self.c.click([10, 20], button="middle")
        self.assertEqual(self.calls[0][-1], 3)   # right == X11 button 3
        self.assertEqual(self.calls[1][-1], 2)   # middle == X11 button 2

    def test_double_and_triple_use_repeat_with_delay(self):
        self.c.click([5, 6], count=2)
        self.c.click([5, 6], count=3)
        self.assertEqual(
            self.calls[0],
            ["mousemove", "--sync", 5, 6, "click", "--repeat", 2, "--delay", 100, 1],
        )
        self.assertEqual(self.calls[1][self.calls[1].index("--repeat") + 1], 3)

    def test_modifier_held_with_keydown_then_click_then_keyup(self):
        ack = self.c.click([1, 2], text="ctrl")
        self.assertEqual(
            self.calls,
            [
                ["keydown", "--", "ctrl"],
                ["mousemove", "--sync", 1, 2, "click", 1],
                ["keyup", "--", "ctrl"],
            ],
        )
        self.assertIn("holding ctrl", ack)

    def test_modifier_released_even_if_click_raises(self):
        # keydown succeeds, the click raises -> keyup must still run (finally), so
        # the modifier is never left stranded down.
        def fail_on_click(args, **_kw):
            self.calls.append(list(args))
            if args and args[0] == "mousemove":
                raise ComputerError("click failed")
            return ""

        self.c.run_xdotool = fail_on_click
        with self.assertRaises(ComputerError):
            self.c.click([1, 2], text="shift+alt")
        self.assertEqual(
            self.calls,
            [
                ["keydown", "--", "shift+alt"],
                ["mousemove", "--sync", 1, 2, "click", 1],
                ["keyup", "--", "shift+alt"],
            ],
        )

    def test_coordinate_is_mapped_through_the_session(self):
        # A 1366x768 image on a 1920x1080 device -> centre maps to (960, 540).
        self.c.last_capture = CaptureSession(1920, 1080, 1366, 768)
        self.c.click([683, 384])
        self.assertEqual(self.calls[0][:4], ["mousemove", "--sync", 960, 540])

    def test_click_without_a_session_raises_and_dispatches_nothing(self):
        self.c.last_capture = None
        with self.assertRaises(ComputerError):
            self.c.click([10, 10])
        self.assertEqual(self.calls, [])  # the look-before-click guard fires first

    def test_bad_coordinate_raises(self):
        for bad in ([1], [1, 2, 3], "nope", [None, 2]):
            with self.assertRaises(ComputerError):
                self.c.click(bad)

    def test_unknown_button_raises(self):
        with self.assertRaises(ComputerError):
            self.c.click([1, 2], button="back")


class PointerActionTest(unittest.TestCase):
    """move/drag/scroll + the stateful left_mouse_down/up pair build the right
    xdotool argv, map coordinates through the screenshot session, hold optional
    scroll modifiers (always releasing), and track the held button across calls.
    No xdotool/desktop needed: ``run_xdotool`` is a recorder and the session is
    set directly (an identity session carries the input coordinate verbatim)."""

    def setUp(self):
        self.c = Computer(display=":10")
        self.c.last_capture = CaptureSession(1366, 768, 1366, 768)
        self.calls = []

        def record(args, **_kw):
            self.calls.append(list(args))
            return ""

        self.c.run_xdotool = record

    def test_mouse_move_builds_a_synced_mousemove(self):
        ack = self.c.mouse_move([100, 200])
        self.assertEqual(self.calls, [["mousemove", "--sync", 100, 200]])
        self.assertIn("(100, 200)", ack)

    def test_drag_with_start_moves_then_presses_drags_releases(self):
        ack = self.c.left_click_drag([300, 400], start_coordinate=[10, 20])
        self.assertEqual(
            self.calls,
            [[
                "mousemove", "--sync", 10, 20,
                "mousedown", 1,
                "mousemove", "--sync", 300, 400,
                "mouseup", 1,
            ]],
        )
        self.assertIn("from (10, 20) to (300, 400)", ack)

    def test_drag_without_start_drags_from_current_position(self):
        ack = self.c.left_click_drag([300, 400])
        self.assertEqual(
            self.calls,
            [["mousedown", 1, "mousemove", "--sync", 300, 400, "mouseup", 1]],
        )
        self.assertIn("from the current position", ack)

    def test_scroll_maps_direction_to_x11_wheel_button(self):
        for direction, button in (("up", 4), ("down", 5), ("left", 6), ("right", 7)):
            self.calls.clear()
            self.c.scroll([5, 6], direction, 3)
            self.assertEqual(
                self.calls,
                [["mousemove", "--sync", 5, 6, "click", "--repeat", 3, button]],
            )

    def test_scroll_holds_modifier_with_keydown_then_scroll_then_keyup(self):
        ack = self.c.scroll([5, 6], "down", 2, text="ctrl")
        self.assertEqual(
            self.calls,
            [
                ["keydown", "--", "ctrl"],
                ["mousemove", "--sync", 5, 6, "click", "--repeat", 2, 5],
                ["keyup", "--", "ctrl"],
            ],
        )
        self.assertIn("holding ctrl", ack)

    def test_scroll_releases_modifier_even_if_the_scroll_raises(self):
        def fail_on_scroll(args, **_kw):
            self.calls.append(list(args))
            if args and args[0] == "mousemove":
                raise ComputerError("scroll failed")
            return ""

        self.c.run_xdotool = fail_on_scroll
        with self.assertRaises(ComputerError):
            self.c.scroll([5, 6], "up", 1, text="shift")
        self.assertEqual(
            self.calls,
            [
                ["keydown", "--", "shift"],
                ["mousemove", "--sync", 5, 6, "click", "--repeat", 1, 4],
                ["keyup", "--", "shift"],
            ],
        )

    def test_scroll_rejects_bad_direction_and_amount(self):
        with self.assertRaises(ComputerError):
            self.c.scroll([5, 6], "sideways", 1)
        for bad in (0, -2, "x", None):
            with self.assertRaises(ComputerError):
                self.c.scroll([5, 6], "up", bad)

    def test_left_mouse_down_presses_button_1_and_marks_held(self):
        ack = self.c.left_mouse_down([7, 8])
        self.assertEqual(
            self.calls, [["mousemove", "--sync", 7, 8, "mousedown", 1]]
        )
        self.assertTrue(self.c._left_button_down)
        self.assertIn("(7, 8)", ack)

    def test_left_mouse_down_without_coordinate_presses_in_place(self):
        self.c.left_mouse_down()
        self.assertEqual(self.calls, [["mousedown", 1]])
        self.assertTrue(self.c._left_button_down)

    def test_left_mouse_down_twice_raises_already_held(self):
        self.c.left_mouse_down([1, 2])
        with self.assertRaises(ComputerError) as ctx:
            self.c.left_mouse_down([3, 4])
        self.assertIn("already held", str(ctx.exception))
        # the rejected second press dispatched nothing extra.
        self.assertEqual(self.calls, [["mousemove", "--sync", 1, 2, "mousedown", 1]])

    def test_left_mouse_up_releases_and_clears_held(self):
        self.c.left_mouse_down([1, 2])
        self.calls.clear()
        ack = self.c.left_mouse_up([9, 10])
        self.assertEqual(self.calls, [["mousemove", "--sync", 9, 10, "mouseup", 1]])
        self.assertFalse(self.c._left_button_down)
        self.assertIn("(9, 10)", ack)

    def test_left_mouse_up_when_not_held_is_a_noop(self):
        ack = self.c.left_mouse_up([9, 10])
        self.assertEqual(self.calls, [])  # nothing dispatched
        self.assertIn("not held", ack)
        self.assertFalse(self.c._left_button_down)

    def test_held_flag_is_not_set_when_the_press_fails(self):
        def boom(_args, **_kw):
            raise ComputerError("xdotool is not installed")

        self.c.run_xdotool = boom
        with self.assertRaises(ComputerError):
            self.c.left_mouse_down([1, 2])
        self.assertFalse(self.c._left_button_down)  # never stranded True

    def test_coordinates_are_mapped_through_the_session(self):
        # A 1366x768 image on a 1920x1080 device -> centre maps to (960, 540).
        self.c.last_capture = CaptureSession(1920, 1080, 1366, 768)
        self.c.mouse_move([683, 384])
        self.assertEqual(self.calls[0], ["mousemove", "--sync", 960, 540])

    def test_move_without_a_session_raises_and_dispatches_nothing(self):
        self.c.last_capture = None
        for call in (
            lambda: self.c.mouse_move([10, 10]),
            lambda: self.c.left_click_drag([10, 10]),
            lambda: self.c.scroll([10, 10], "up", 1),
            lambda: self.c.left_mouse_down([10, 10]),
        ):
            with self.assertRaises(ComputerError):
                call()
        self.assertEqual(self.calls, [])  # look-before-you-point guard fires first

    def test_bad_coordinate_raises(self):
        for bad in ([1], [1, 2, 3], "nope", [None, 2]):
            with self.assertRaises(ComputerError):
                self.c.mouse_move(bad)

    def test_reset_held_state_releases_button_and_clears_grants(self):
        self.c.left_mouse_down([1, 2])
        self.c.request_access(apps=["Slack"], reason="x", clipboardRead=True)
        self.calls.clear()
        self.c.reset_held_state()
        self.assertEqual(self.calls, [["mouseup", "1"]])  # button released
        self.assertFalse(self.c._left_button_down)
        self.assertEqual(self.c._allowlist, set())        # grants cleared
        self.assertFalse(self.c._clipboard_read)

    def test_reset_held_state_is_a_safe_noop_when_nothing_held(self):
        self.c.reset_held_state()        # no button, no grants
        self.assertEqual(self.calls, [])
        # and it must never raise even if the release dispatch fails.
        self.c._left_button_down = True

        def boom(_args, **_kw):
            raise ComputerError("xdotool is not installed")

        self.c.run_xdotool = boom
        self.c.reset_held_state()        # swallows the error
        self.assertFalse(self.c._left_button_down)

    def test_clear_stranded_button_issues_a_best_effort_mouseup(self):
        # Our flag is False (fresh process) but a crashed predecessor may have
        # left button 1 down — clear it unconditionally.
        self.c.clear_stranded_button()
        self.assertEqual(self.calls, [["mouseup", "1"]])


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

    def test_implemented_is_the_full_tool_surface(self):
        # With the move group (SCRUM-1402) merged alongside the utility group
        # (SCRUM-1405), every declared tool is now implemented: capture
        # (SCRUM-1397 screenshot + SCRUM-1400 zoom) + click (SCRUM-1401) +
        # move/drag/scroll (SCRUM-1402) + keyboard (SCRUM-1403) + clipboard/session
        # (SCRUM-1404) + utility (SCRUM-1405). No tool stays declared-only.
        self.assertEqual(
            IMPLEMENTED,
            {
                "screenshot",
                "zoom",
                "left_click",
                "right_click",
                "middle_click",
                "double_click",
                "triple_click",
                "mouse_move",
                "left_click_drag",
                "scroll",
                "left_mouse_down",
                "left_mouse_up",
                "type",
                "key",
                "hold_key",
                "read_clipboard",
                "write_clipboard",
                "request_access",
                "list_granted_applications",
                "open_application",
                "switch_display",
                "computer_batch",
                "wait",
                "cursor_position",
            },
        )

    def test_key_has_optional_repeat(self):
        # SCRUM-1403: key gains an additive `repeat` input; surface stays 24.
        key = next(t for t in TOOLS if t["name"] == "key")
        schema = key["inputSchema"]
        repeat = schema["properties"].get("repeat")
        self.assertIsNotNone(repeat)
        self.assertEqual(repeat["type"], "integer")
        self.assertEqual(repeat["minimum"], 1)
        self.assertNotIn("repeat", schema.get("required", []))  # stays optional
        self.assertEqual(len(TOOL_NAMES), 24)

    def test_scroll_has_optional_text_modifier(self):
        # SCRUM-1402: scroll gains an additive `text` modifier (held during the
        # scroll); it stays optional and the surface stays 24.
        scroll = next(t for t in TOOLS if t["name"] == "scroll")
        schema = scroll["inputSchema"]
        text = schema["properties"].get("text")
        self.assertIsNotNone(text)
        self.assertEqual(text["type"], "string")
        self.assertNotIn("text", schema.get("required", []))
        self.assertEqual(
            schema["required"], ["coordinate", "scroll_direction", "scroll_amount"]
        )
        self.assertEqual(len(TOOL_NAMES), 24)

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


class CaptureSessionTest(unittest.TestCase):
    """AC3 — image-space -> device-pixel mapping from measured geometry."""

    def test_to_device_maps_image_space_to_device_pixels(self):
        # A 1366x768 image on a 1920x1080 device -> ~x1.406.
        s = CaptureSession(1920, 1080, 1366, 768)
        self.assertEqual(s.to_device(683, 384), (960, 540))    # centre
        self.assertEqual(s.to_device(0, 0), (0, 0))
        self.assertEqual(s.to_device(1366, 768), (1920, 1080))  # far corner

    def test_to_device_is_identity_when_image_equals_device(self):
        s = CaptureSession(1366, 768, 1366, 768)
        self.assertEqual(s.to_device(640, 480), (640, 480))

    def test_round_trip_image_to_device_is_stable(self):
        s = CaptureSession(1920, 1080, 1366, 768)
        # device->image (scale_coordinates COMPUTER) then image->device returns
        # the original, within rounding.
        c = Computer(width=1920, height=1080)
        ix, iy = c.scale_coordinates(ScalingSource.COMPUTER, 960, 540)
        self.assertEqual(s.to_device(ix, iy), (960, 540))

    def test_computer_to_device_uses_the_recorded_session(self):
        c = Computer(display=":10")
        with self.assertRaises(ComputerError):
            c.to_device(10, 10)  # no session established yet
        c.last_capture = CaptureSession(1920, 1080, 1366, 768)
        self.assertEqual(c.to_device(683, 384), (960, 540))


class ScalingTargetTest(unittest.TestCase):
    """The downscale target is keyed on the MEASURED size, not self.width/height."""

    def test_downscale_target_is_measured_basis(self):
        c = Computer(display=":10")
        self.assertEqual(c._scaling_target(1920, 1080), (1366, 768))
        self.assertEqual(c._scaling_target(1600, 1000), (1280, 800))
        self.assertIsNone(c._scaling_target(1366, 768))    # already at/below target
        self.assertIsNone(c._scaling_target(1000, 1000))   # no matching aspect

    def test_disabled_scaling_has_no_target(self):
        c = Computer(display=":10", scaling_enabled=False)
        self.assertIsNone(c._scaling_target(1920, 1080))


class ZoomRegionTest(unittest.TestCase):
    """AC2 — region (x0,y0,x1,y1) validation + image-space -> device-rect."""

    def test_region_maps_to_a_device_rect(self):
        s = CaptureSession(1920, 1080, 1366, 768)
        x0, y0, x1, y1 = Computer._validate_region([100, 100, 200, 200], 1366, 768)
        dx0, dy0 = s.to_device(x0, y0)
        dx1, dy1 = s.to_device(x1, y1)
        self.assertEqual((dx0, dy0, dx1 - dx0, dy1 - dy0), (141, 141, 140, 140))

    def test_region_validation_rejects_degenerate_and_oob(self):
        for bad in ([200, 100, 100, 200], [100, 200, 200, 100], [1, 2, 3]):
            with self.assertRaises(ComputerError):
                Computer._validate_region(bad, 1366, 768)
        with self.assertRaises(ComputerError):  # entirely off-screen
            Computer._validate_region([2000, 100, 3000, 200], 1366, 768)

    def test_region_clamps_to_image_bounds(self):
        self.assertEqual(
            Computer._validate_region([-10, -10, 5000, 5000], 1366, 768),
            (0, 0, 1366, 768),
        )


class WaitTest(unittest.TestCase):
    """SCRUM-1405 — wait sleeps in-process, with input guards. No desktop: the
    sleep is patched so the test never actually blocks."""

    def setUp(self):
        self.c = Computer(display=":10")

    def test_sleeps_in_process_with_no_xdotool(self):
        with mock.patch("time.sleep") as slept:
            ack = self.c.wait(0.25)
        slept.assert_called_once_with(0.25)
        self.assertIn("0.25", ack)

    def test_zero_is_allowed(self):
        with mock.patch("time.sleep") as slept:
            self.c.wait(0)
        slept.assert_called_once_with(0.0)

    def test_negative_is_rejected_without_sleeping(self):
        with mock.patch("time.sleep") as slept:
            with self.assertRaises(ComputerError):
                self.c.wait(-1)
        slept.assert_not_called()

    def test_non_numeric_is_rejected(self):
        for bad in ("soon", None, [1]):
            with self.assertRaises(ComputerError):
                self.c.wait(bad)

    def test_nan_and_inf_are_rejected(self):
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ComputerError):
                self.c.wait(bad)

    def test_over_cap_is_rejected_without_sleeping(self):
        from computer import WAIT_MAX_SECONDS

        with mock.patch("time.sleep") as slept:
            with self.assertRaises(ComputerError):
                self.c.wait(WAIT_MAX_SECONDS + 1)
        slept.assert_not_called()


class CursorPositionTest(unittest.TestCase):
    """SCRUM-1405 — cursor_position parses getmouselocation --shell and scales the
    real pointer DOWN into model space. run_xdotool is stubbed (no desktop)."""

    def setUp(self):
        self.c = Computer(display=":10", width=1920, height=1080)

    def test_uses_getmouselocation_shell(self):
        seen = {}

        def rec(args, **_kw):
            seen["args"] = list(args)
            return "X=0\nY=0\nSCREEN=0\nWINDOW=1\n"

        self.c.run_xdotool = rec
        self.c.cursor_position()
        self.assertEqual(seen["args"], ["getmouselocation", "--shell"])

    def test_screen_corner_scales_to_model_corner(self):
        self.c.run_xdotool = lambda *a, **k: "X=1920\nY=1080\nSCREEN=0\nWINDOW=9\n"
        self.assertEqual(self.c.cursor_position(), [1366, 768])

    def test_centre_scales_down(self):
        self.c.run_xdotool = lambda *a, **k: "X=960\nY=540\nSCREEN=0\nWINDOW=9\n"
        self.assertEqual(self.c.cursor_position(), [683, 384])

    def test_unparseable_output_raises(self):
        self.c.run_xdotool = lambda *a, **k: "no coords here\n"
        with self.assertRaises(ComputerError):
            self.c.cursor_position()


class ComputerBatchTest(unittest.TestCase):
    """SCRUM-1405 — computer_batch fan-out, driven through the real
    ``Server._dispatch`` (same gating as a top-level call) with run_xdotool
    stubbed so keyboard steps run without a desktop."""

    def setUp(self):
        from server import Server

        self.c = Computer(display=":10")
        self.calls = []

        def record(args, **_kw):
            self.calls.append(list(args))
            return ""

        self.c.run_xdotool = record
        self.srv = Server(self.c)

    def _batch(self, actions):
        return self.srv._dispatch("computer_batch", {"actions": actions})

    def _summary(self, res):
        return json.loads(res["content"][0]["text"])["batch"]

    def test_runs_steps_in_order_and_combines_results(self):
        res = self._batch(
            [
                {"name": "type", "arguments": {"text": "hi"}},
                {"name": "key", "arguments": {"text": "Return"}},
            ]
        )
        self.assertFalse(res["isError"])
        s = self._summary(res)
        self.assertEqual(
            (s["total"], s["succeeded"], s["failed"], s["skipped"]), (2, 2, 0, 0)
        )
        self.assertIsNone(s["stopped_at"])
        # the underlying xdotool ran in batch order
        self.assertEqual(
            self.calls,
            [
                ["type", "--delay", "12", "--", "hi"],
                ["key", "--repeat", 1, "--", "Return"],
            ],
        )
        # each step's text block is tagged with its index/name
        tags = [b["text"] for b in res["content"][1:]]
        self.assertTrue(any(t.startswith("[step 0 · type]") for t in tags))
        self.assertTrue(any(t.startswith("[step 1 · key]") for t in tags))

    def test_stops_at_first_error_and_skips_the_rest(self):
        res = self._batch(
            [
                {"name": "type", "arguments": {"text": "hi"}},
                {"name": "mouse_move", "arguments": {"coordinate": [1, 2]}},  # fails
                {"name": "key", "arguments": {"text": "Return"}},  # never runs
            ]
        )
        self.assertTrue(res["isError"])
        s = self._summary(res)
        self.assertEqual(s["stopped_at"], 1)
        self.assertEqual((s["succeeded"], s["failed"], s["skipped"]), (1, 1, 1))
        # only the first step shelled out; the post-halt key never ran
        self.assertEqual(self.calls, [["type", "--delay", "12", "--", "hi"]])
        # the failing step's error is carried through, tagged with its index:
        # mouse_move maps its coordinate through the screenshot session and none
        # was established in this unit setup, so it raises the look-before-you-
        # point guard before shelling out — the deterministic, xdotool-free
        # failure that halts the batch here.
        self.assertTrue(
            any(
                "step 1" in b.get("text", "")
                and "no screenshot session" in b.get("text", "")
                for b in res["content"][1:]
            )
        )

    def test_rejects_nested_computer_batch_without_recursing(self):
        res = self._batch([{"name": "computer_batch", "arguments": {"actions": []}}])
        self.assertTrue(res["isError"])
        self.assertEqual(self._summary(res)["stopped_at"], 0)
        self.assertIn("nested", res["content"][1]["text"].lower())
        self.assertEqual(self.calls, [])  # never dispatched / recursed

    def test_rejects_empty_actions(self):
        res = self._batch([])
        self.assertTrue(res["isError"])
        self.assertIn("non-empty", res["content"][0]["text"])

    def test_rejects_non_list_actions(self):
        res = self.srv._dispatch("computer_batch", {"actions": "type"})
        self.assertTrue(res["isError"])

    def test_unknown_action_halts_at_its_index(self):
        res = self._batch(
            [{"name": "type", "arguments": {"text": "x"}}, {"name": "nope"}]
        )
        self.assertTrue(res["isError"])
        self.assertEqual(self._summary(res)["stopped_at"], 1)

    def test_malformed_step_without_a_name_halts(self):
        res = self._batch([{"arguments": {}}])
        self.assertTrue(res["isError"])
        self.assertEqual(self._summary(res)["stopped_at"], 0)


@unittest.skipUnless(
    os.environ.get("DISPLAY"), "no DISPLAY (run via ./run_tests.sh / xvfb-run)"
)
class CaptureLiveTest(unittest.TestCase):
    """AC1/AC2/AC3 against a real display — screenshot/zoom + save_to_disk."""

    def setUp(self):
        self.save_dir = tempfile.mkdtemp(prefix="cu-test-save-")
        os.environ["CU_SAVE_DIR"] = self.save_dir
        os.environ["CU_SCREENSHOT_DELAY"] = "0"
        self.c = Computer()

    def tearDown(self):
        os.environ.pop("CU_SAVE_DIR", None)
        shutil.rmtree(self.save_dir, ignore_errors=True)

    def test_screenshot_records_a_measured_session(self):
        cap = self.c.screenshot()
        s = self.c.last_capture
        self.assertIsNotNone(s)
        # device >= image (only ever downscale), both non-zero.
        self.assertGreaterEqual(s.device_width, s.image_width)
        self.assertGreaterEqual(s.device_height, s.image_height)
        self.assertGreater(s.image_width, 0)
        self.assertEqual((cap.width, cap.height), (s.image_width, s.image_height))
        self.assertIsNone(cap.path)  # not written unless asked

    def test_screenshot_save_to_disk_writes_a_file(self):
        cap = self.c.screenshot(save_to_disk=True)
        self.assertIsNotNone(cap.path)
        self.assertTrue(Path(cap.path).is_file())
        self.assertTrue(cap.path.startswith(self.save_dir))
        self.assertEqual(_png_size(base64.b64decode(cap.data_b64)),
                         (cap.width, cap.height))

    def test_zoom_is_read_only_and_matches_the_device_rect(self):
        self.c.screenshot()
        before = self.c.last_capture
        z = self.c.zoom([100, 100, 300, 250], save_to_disk=True)
        # zoom must NOT move the click-coordinate origin (AC2).
        self.assertEqual(self.c.last_capture, before)
        self.assertTrue(Path(z.path).is_file())
        # the crop equals the device rect derived from the session (genuine
        # magnification: device px > the image-space region on a downscaled :10).
        dx0, dy0 = before.to_device(100, 100)
        dx1, dy1 = before.to_device(300, 250)
        self.assertEqual((z.width, z.height),
                         (max(1, dx1 - dx0), max(1, dy1 - dy0)))

    def test_zoom_establishes_the_session_when_called_first(self):
        self.assertIsNone(self.c.last_capture)
        z = self.c.zoom([10, 10, 110, 90])
        self.assertIsNotNone(self.c.last_capture)
        self.assertGreater(z.width, 0)
        self.assertGreater(z.height, 0)


if __name__ == "__main__":
    unittest.main()
