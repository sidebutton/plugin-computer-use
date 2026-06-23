"""Unit tests for the computer.py dispatch base (no desktop required)."""

import base64
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_capture_group_is_implemented(self):
        # SCRUM-1397 wired screenshot; SCRUM-1400 adds zoom (capture group done).
        self.assertEqual(IMPLEMENTED, {"screenshot", "zoom"})


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
