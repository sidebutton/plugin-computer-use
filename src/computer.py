"""Dispatch base for the SideButton computer-use plugin.

Ported from Anthropic's reference ``computer.py`` (anthropic-quickstarts,
``computer-use-demo``). This is the reusable machinery the per-tool sibling
tickets (SCRUM-1400..1405) build on:

  * DISPLAY targeting (defaults to ``:10``, the SideButton runner desktop)
  * screenshot capture -> base64 PNG (gnome-screenshot / scrot / ImageMagick)
  * a single coordinate-scaling function (:meth:`Computer.scale_coordinates`)
  * an ``xdotool`` action runner (:meth:`Computer.run_xdotool`)
  * a configurable post-action screenshot delay
  * single-owner locking (one session per shared desktop)
  * error -> MCP ``{isError: true}`` mapping (via :class:`ComputerError`)

SCRUM-1397 wires ``screenshot`` end-to-end as the proof action; the other
actions are declared in the tool surface (see ``tools.py``) and implemented by
the sibling tickets against this base.
"""

from __future__ import annotations

import base64
import fcntl
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import NamedTuple

# --- Coordinate scaling --------------------------------------------------
# Ported verbatim from Anthropic's computer.py. The model works in one of these
# reduced resolutions; coordinates it returns are scaled UP to the real screen,
# and real screen coordinates are scaled DOWN before they are reported back.
MAX_SCALING_TARGETS = {
    "XGA": {"width": 1024, "height": 768},   # 4:3
    "WXGA": {"width": 1280, "height": 800},  # 16:10
    "FWXGA": {"width": 1366, "height": 768},  # ~16:9
}


class ScalingSource:
    """Where a coordinate came from (mirrors Anthropic's ``ScalingSource``)."""

    COMPUTER = "computer"  # a real on-screen coordinate -> scale DOWN for the API
    API = "api"            # a coordinate from the model -> scale UP to the screen


class ComputerError(RuntimeError):
    """A dispatch failure. The server maps this to an MCP ``isError`` result."""


# Defaults tuned for the SideButton runner. The desktop runs on Xvfb ``:10`` at
# 1920x1080; the SideButton service exports ``DISPLAY=:10`` and the plugin
# executor passes its full env through, so we honour the inherited DISPLAY and
# only fall back to ``:10`` when it is unset. (Hardcoding a non-existent display
# is exactly the bug that made plugin-screen-record silently capture nothing.)
DEFAULT_DISPLAY = ":10"
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_SCREENSHOT_DELAY = 2.0

LOCK_PATH = os.environ.get(
    "CU_LOCK_PATH",
    os.path.join(tempfile.gettempdir(), "sidebutton-computer-use.lock"),
)

# Where ``save_to_disk`` writes shareable captures. Bounded and host-owned:
# saved files are intentionally NOT unlinked (pruning the dir is the host's job,
# per SCRUM-1406). Override with ``CU_SAVE_DIR``.
DEFAULT_SAVE_DIR = os.path.join(tempfile.gettempdir(), "sidebutton-computer-use")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(data: bytes) -> tuple[int, int]:
    """Read ``(width, height)`` from a PNG's IHDR chunk — the *measured* capture
    geometry, never a configured assumption."""
    if len(data) < 24 or not data.startswith(PNG_MAGIC):
        raise ComputerError("capture is not a PNG (cannot measure geometry)")
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


class CaptureSession(NamedTuple):
    """The screenshot->coordinate session recorded at capture time (AC3).

    ``device_*`` is the *measured* raw-capture size (real screen pixels);
    ``image_*`` is the size of the (possibly downscaled) PNG handed to the model.
    Model coordinates are relative to the image, so :meth:`to_device` maps them
    back to real screen pixels for the pointer/keyboard siblings.
    """

    device_width: int
    device_height: int
    image_width: int
    image_height: int

    def to_device(self, x: float, y: float) -> tuple[int, int]:
        """Map an image-space coordinate to real device pixels."""
        return (
            round(x * self.device_width / self.image_width),
            round(y * self.device_height / self.image_height),
        )


class Capture(NamedTuple):
    """A capture result: base64 PNG, its image-space size, and an optional saved
    path (set only when ``save_to_disk`` was requested)."""

    data_b64: str
    width: int
    height: int
    path: str | None = None


def detect_screenshot_backend():
    """Return the first available screenshot backend, or ``None``.

    ``gnome-screenshot`` and ``scrot`` match the Anthropic reference. ImageMagick
    ``import`` is appended because the SideButton runner images ship neither of
    the first two (verified: only ImageMagick is present on these VMs).
    """
    if shutil.which("gnome-screenshot"):
        return "gnome-screenshot"
    if shutil.which("scrot"):
        return "scrot"
    if shutil.which("import"):  # ImageMagick
        return "import"
    return None


class SingleOwnerLock:
    """Advisory single-owner lock so only ONE computer-use session drives the
    shared desktop at a time. Held for the lifetime of the server process via
    ``flock`` (released automatically when the process — and thus the fd — dies).
    """

    def __init__(self, path: str = LOCK_PATH):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> "SingleOwnerLock":
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise ComputerError(
                f"another computer-use session already owns {self.path}"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "SingleOwnerLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


class Computer:
    """The dispatch base. Holds the target display/resolution and exposes the
    primitives every computer-use action is built from."""

    def __init__(
        self,
        display: str | None = None,
        width: int | None = None,
        height: int | None = None,
        screenshot_delay: float | None = None,
        scaling_enabled: bool = True,
    ):
        self.display = display or os.environ.get("DISPLAY") or DEFAULT_DISPLAY
        self.width = int(width or os.environ.get("CU_WIDTH", DEFAULT_WIDTH))
        self.height = int(height or os.environ.get("CU_HEIGHT", DEFAULT_HEIGHT))
        self.screenshot_delay = float(
            screenshot_delay
            if screenshot_delay is not None
            else os.environ.get("CU_SCREENSHOT_DELAY", DEFAULT_SCREENSHOT_DELAY)
        )
        self.scaling_enabled = scaling_enabled
        # The screenshot->coordinate session (AC3). Recorded on every
        # screenshot(); zoom reads it but never overwrites it. None until the
        # first capture establishes it.
        self.last_capture: CaptureSession | None = None

    def _env(self) -> dict:
        """A subprocess env that forces our target DISPLAY."""
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        return env

    # --- coordinate scaling (ported from Anthropic computer.py) ----------
    def scale_coordinates(self, source: str, x: int, y: int) -> tuple[int, int]:
        """Scale a coordinate between the real screen and the model resolution."""
        if not self.scaling_enabled:
            return x, y
        ratio = self.width / self.height
        target = None
        for dim in MAX_SCALING_TARGETS.values():
            if abs(dim["width"] / dim["height"] - ratio) < 0.02:
                if dim["width"] < self.width:
                    target = dim
                break
        if target is None:
            return x, y
        x_factor = target["width"] / self.width
        y_factor = target["height"] / self.height
        if source == ScalingSource.API:
            if x > self.width or y > self.height:
                raise ComputerError(f"coordinates {x}, {y} are out of bounds")
            # scale up
            return round(x / x_factor), round(y / y_factor)
        # scale down
        return round(x * x_factor), round(y * y_factor)

    def _scaling_target(self, width: int, height: int) -> tuple[int, int] | None:
        """The downscale target for a *measured* capture size, or ``None``.

        Same selection rule as :meth:`scale_coordinates` (match a
        ``MAX_SCALING_TARGETS`` entry by aspect ratio; only ever scale *down*),
        but keyed on the measured geometry rather than the configured
        ``self.width/height`` — so the downscale basis and the coordinate-scaling
        basis are one and the same, closing the wrong-basis bug (AC3).
        """
        if not self.scaling_enabled:
            return None
        ratio = width / height
        for dim in MAX_SCALING_TARGETS.values():
            if abs(dim["width"] / dim["height"] - ratio) < 0.02:
                if dim["width"] < width:
                    return dim["width"], dim["height"]
                return None
        return None

    def to_device(self, x: float, y: float) -> tuple[int, int]:
        """Map an image-space coordinate (relative to the last screenshot) to
        real device pixels via the recorded session. The click/move siblings
        (SCRUM-1401/1402) call this before issuing an xdotool event."""
        if self.last_capture is None:
            raise ComputerError(
                "no screenshot session yet — call screenshot before mapping "
                "coordinates"
            )
        return self.last_capture.to_device(x, y)

    # --- xdotool action runner -------------------------------------------
    def build_xdotool(self, args) -> tuple[list[str], dict]:
        """Return ``(argv, env)`` for an xdotool invocation WITHOUT running it.

        Exposed so unit tests (and sibling tickets) can assert command
        construction and the DISPLAY-pinned env without driving the desktop.
        """
        return (["xdotool", *[str(a) for a in args]], self._env())

    def run_xdotool(self, args, timeout: float = 90) -> str:
        """Run an xdotool command against the target display.

        ``timeout`` defaults to 90s so long holds (``hold_key`` / ``wait``, up to
        ~100s) do not self-kill when run standalone. (The 30s per-call cap only
        applies once SCRUM-1406 hosts this as a service plugin.)
        """
        argv, env = self.build_xdotool(args)
        if shutil.which("xdotool") is None:
            raise ComputerError(
                "xdotool is not installed on this host (required for "
                "pointer/keyboard actions; declare it in the plugin system_deps)"
            )
        proc = subprocess.run(argv, env=env, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise ComputerError(
                f"xdotool {' '.join(str(a) for a in args)} failed: "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )
        return proc.stdout.decode(errors="replace")

    # --- capture: screenshot / zoom --------------------------------------
    def _tmp(self, tag: str) -> Path:
        return Path(tempfile.gettempdir()) / f"cu-{tag}-{uuid.uuid4().hex}.png"

    def _run_backend(self, tmp: Path) -> None:
        """Capture the full target display into ``tmp`` at native resolution."""
        backend = detect_screenshot_backend()
        if backend is None:
            raise ComputerError(
                "no screenshot backend found (need gnome-screenshot, scrot, or "
                "ImageMagick `import`)"
            )
        if backend == "gnome-screenshot":
            cmd = ["gnome-screenshot", "-f", str(tmp), "-p"]
        elif backend == "scrot":
            cmd = ["scrot", "-p", str(tmp)]
        else:  # ImageMagick `import` grabs the root window of $DISPLAY
            cmd = ["import", "-window", "root", str(tmp)]
        proc = subprocess.run(cmd, env=self._env(), capture_output=True, timeout=30)
        if proc.returncode != 0 or not tmp.exists():
            raise ComputerError(
                f"{backend} failed to capture {self.display}: "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )

    def _save(self, data: bytes, prefix: str) -> str:
        """Write ``data`` to a unique file under ``CU_SAVE_DIR`` and return the
        path. Saved files are intentionally left in place (host-owned)."""
        save_dir = Path(os.environ.get("CU_SAVE_DIR", DEFAULT_SAVE_DIR))
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"{prefix}-{uuid.uuid4().hex}.png"
        path.write_bytes(data)
        return str(path)

    def screenshot(self, save_to_disk: bool = False) -> Capture:
        """Capture the target display and return a base64 PNG :class:`Capture`.

        Records the screenshot->coordinate session (:attr:`last_capture`) from the
        *measured* capture geometry, downscales to the model resolution when the
        measured size matches a scaling target (mirroring the Anthropic
        reference, ``convert <path> -resize WxH!``), and honours ``save_to_disk``.
        """
        tmp = self._tmp("screenshot")
        try:
            self._run_backend(tmp)
            raw = tmp.read_bytes()
            dev_w, dev_h = _png_size(raw)
            target = self._scaling_target(dev_w, dev_h)
            if target is not None and shutil.which("convert"):
                tw, th = target
                # Best-effort: if convert fails we keep the native capture.
                subprocess.run(
                    ["convert", str(tmp), "-resize", f"{tw}x{th}!", str(tmp)],
                    env=self._env(), capture_output=True, timeout=30, check=False,
                )
                data = tmp.read_bytes()
            else:
                data = raw
            img_w, img_h = _png_size(data)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        # Derive BOTH the downscale and the coordinate mapping from the measured
        # geometry, so they can never use different bases.
        self.last_capture = CaptureSession(dev_w, dev_h, img_w, img_h)
        path = self._save(data, "screenshot") if save_to_disk else None
        return Capture(base64.b64encode(data).decode(), img_w, img_h, path)

    @staticmethod
    def _validate_region(region, img_w: int, img_h: int) -> tuple[int, int, int, int]:
        """Validate and clamp an ``(x0, y0, x1, y1)`` image-space region."""
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise ComputerError("region must be [x0, y0, x1, y1]")
        try:
            x0, y0, x1, y1 = (int(round(float(v))) for v in region)
        except (TypeError, ValueError):
            raise ComputerError(f"region values must be numbers: {region!r}")
        if x1 <= x0 or y1 <= y0:
            raise ComputerError(
                f"invalid region {list(region)}: need x1 > x0 and y1 > y0"
            )
        cx0, cx1 = max(0, min(x0, img_w)), max(0, min(x1, img_w))
        cy0, cy1 = max(0, min(y0, img_h)), max(0, min(y1, img_h))
        if cx1 <= cx0 or cy1 <= cy0:
            raise ComputerError(
                f"region {list(region)} is outside the screenshot bounds "
                f"{img_w}x{img_h}"
            )
        return cx0, cy0, cx1, cy1

    def zoom(self, region, save_to_disk: bool = False) -> Capture:
        """Return a magnified full-res PNG of ``region`` of the last screenshot.

        ``region`` is ``(x0, y0, x1, y1)`` in image space (the coordinate space of
        the most recent screenshot). It is mapped to device pixels via the session
        and cropped from a *fresh* full-resolution capture — genuine magnification
        versus the downscaled screenshot. Establishes the session lazily if none
        exists yet, and never mutates it (so it cannot move the click origin, AC2).
        """
        if self.last_capture is None:
            self.screenshot()  # establish the session (sets last_capture)
        session = self.last_capture
        x0, y0, x1, y1 = self._validate_region(
            region, session.image_width, session.image_height
        )
        dx0, dy0 = session.to_device(x0, y0)
        dx1, dy1 = session.to_device(x1, y1)
        dev_x, dev_y = dx0, dy0
        dev_w, dev_h = max(1, dx1 - dx0), max(1, dy1 - dy0)

        tmp = self._tmp("zoom-raw")
        out = self._tmp("zoom")
        try:
            self._run_backend(tmp)  # fresh full-resolution capture
            if shutil.which("convert") is None:
                raise ComputerError(
                    "ImageMagick `convert` is required to crop a zoom region"
                )
            proc = subprocess.run(
                ["convert", str(tmp), "-crop",
                 f"{dev_w}x{dev_h}+{dev_x}+{dev_y}", "+repage", str(out)],
                env=self._env(), capture_output=True, timeout=30,
            )
            if proc.returncode != 0 or not out.exists():
                raise ComputerError(
                    "convert failed to crop the zoom region: "
                    f"{proc.stderr.decode(errors='replace').strip()}"
                )
            data = out.read_bytes()
        finally:
            for p in (tmp, out):
                try:
                    p.unlink()
                except OSError:
                    pass
        img_w, img_h = _png_size(data)
        path = self._save(data, "zoom") if save_to_disk else None
        # last_capture is deliberately left untouched.
        return Capture(base64.b64encode(data).decode(), img_w, img_h, path)

    # --- keyboard (SCRUM-1403) -------------------------------------------
    # Commands are fixed by the design-of-record
    # (the-assistant/docs/plugins/computer-use.md). The `--` separator stops
    # xdotool from parsing text/keys that begin with `-` as flags.
    def type_text(self, text: str) -> str:
        """Type ``text`` at the current focus (``xdotool type``)."""
        self.run_xdotool(["type", "--delay", "12", "--", text])
        return f"typed {len(text)} character(s)"

    def press_key(self, text: str, repeat: int = 1) -> str:
        """Press a key or chord, optionally ``repeat`` times (``xdotool key``).

        ``repeat`` maps to ``--repeat`` and defaults to 1 (a single press).
        """
        self.run_xdotool(["key", "--repeat", repeat, "--", text])
        return f"pressed {text}" + (f" x{repeat}" if repeat != 1 else "")

    def hold_key(self, text: str, duration: float) -> str:
        """Hold ``text`` down for ``duration`` seconds (keydown -> sleep -> keyup).

        The wait lives in this (persistent) process via :func:`time.sleep`, **not**
        inside an ``xdotool`` subprocess, so a long hold (up to ~100s) never trips
        :meth:`run_xdotool`'s subprocess timeout. ``keyup`` runs in a ``finally``
        so a held key/modifier is always released (no stranded modifier), even if
        the wait is interrupted.
        """
        self.run_xdotool(["keydown", "--", text])
        try:
            time.sleep(duration)
        finally:
            self.run_xdotool(["keyup", "--", text])
        return f"held {text} for {duration}s"
