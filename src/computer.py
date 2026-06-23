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
import uuid
from pathlib import Path

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

    # --- screenshot -> base64 PNG ----------------------------------------
    def screenshot(self) -> str:
        """Capture the target display and return a base64-encoded PNG."""
        backend = detect_screenshot_backend()
        if backend is None:
            raise ComputerError(
                "no screenshot backend found (need gnome-screenshot, scrot, or "
                "ImageMagick `import`)"
            )
        tmp = Path(tempfile.gettempdir()) / f"cu-screenshot-{uuid.uuid4().hex}.png"
        env = self._env()
        try:
            if backend == "gnome-screenshot":
                cmd = ["gnome-screenshot", "-f", str(tmp), "-p"]
            elif backend == "scrot":
                cmd = ["scrot", "-p", str(tmp)]
            else:  # ImageMagick `import` grabs the root window of $DISPLAY
                cmd = ["import", "-window", "root", str(tmp)]
            proc = subprocess.run(cmd, env=env, capture_output=True, timeout=30)
            if proc.returncode != 0 or not tmp.exists():
                raise ComputerError(
                    f"{backend} failed to capture {self.display}: "
                    f"{proc.stderr.decode(errors='replace').strip()}"
                )
            # Downscale to the model-facing resolution, mirroring the Anthropic
            # reference (`convert <path> -resize WxH!`). Best-effort: a missing
            # `convert` or a no-op target just leaves the native capture.
            if self.scaling_enabled and shutil.which("convert"):
                tx, ty = self.scale_coordinates(
                    ScalingSource.COMPUTER, self.width, self.height
                )
                if (tx, ty) != (self.width, self.height):
                    subprocess.run(
                        ["convert", str(tmp), "-resize", f"{tx}x{ty}!", str(tmp)],
                        env=env, capture_output=True, timeout=30, check=False,
                    )
            return base64.b64encode(tmp.read_bytes()).decode()
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
