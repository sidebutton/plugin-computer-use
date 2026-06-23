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

        # --- session grant state (request_access / clipboard / open_application)
        # Lives in memory because the service engine keeps ONE long-lived child
        # and serializes calls (the-assistant packages/server service-manager),
        # so cross-call state is safe. XFCE/Xvfb has no compositor approval
        # dialog, so request_access auto-grants; these flags exist so the call
        # shapes match the native macOS contract (clipboard/systemKeyCombos
        # grants), and clipboard reads/writes are gated on them.
        self._allowlist: set[str] = set()
        self._clipboard_read = False
        self._clipboard_write = False
        self._system_key_combos = False

        # --- held pointer state (SCRUM-1402 left_mouse_down/up)
        # Button 1 is held ACROSS calls by left_mouse_down until left_mouse_up.
        # Lives here (not on the X server) so the persistent child can guard
        # against a double press and auto-release a button stranded by a turn
        # that ended before left_mouse_up (see reset_held_state).
        self._left_button_down = False

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

    # --- clipboard + session (SCRUM-1404) --------------------------------
    # The macOS session/permission model has no XFCE/Xvfb equivalent, so these
    # degrade gracefully (never crash) instead of erroring — keeping cross-runner
    # (macOS-authored) skills working. Behaviour follows the design of record
    # (the-assistant docs/plugins/computer-use.md "Divergences"): request_access
    # auto-grants and reports screenshotFiltering=false, switch_display is a
    # single-display no-op, open_application is best-effort window focus, and the
    # clipboard tools shell out to xclip gated on the clipboardRead/Write grants.

    def build_xclip(self, args) -> tuple[list[str], dict]:
        """Return ``(argv, env)`` for an xclip invocation WITHOUT running it
        (exposed so unit tests can assert command construction with no desktop)."""
        return (["xclip", *[str(a) for a in args]], self._env())

    def build_wmctrl(self, args) -> tuple[list[str], dict]:
        """Return ``(argv, env)`` for a wmctrl invocation WITHOUT running it."""
        return (["wmctrl", *[str(a) for a in args]], self._env())

    def request_access(
        self,
        apps=None,
        reason=None,
        clipboardRead: bool = False,
        clipboardWrite: bool = False,
        systemKeyCombos: bool = False,
    ) -> dict:
        """Auto-grant the requested apps + flags (Linux has no compositor dialog).

        Grants are additive across calls, mirroring native ("previously granted
        apps remain granted"). ``reason`` is accepted for call-shape parity but
        has no Linux surface (no approval dialog). Returns the cumulative grant.
        """
        self._allowlist.update(str(a) for a in (apps or []))
        self._clipboard_read = self._clipboard_read or bool(clipboardRead)
        self._clipboard_write = self._clipboard_write or bool(clipboardWrite)
        self._system_key_combos = self._system_key_combos or bool(systemKeyCombos)
        return {
            "grantedApplications": sorted(self._allowlist),
            "deniedApplications": [],
            "screenshotFiltering": False,
            "clipboardRead": self._clipboard_read,
            "clipboardWrite": self._clipboard_write,
            "systemKeyCombos": self._system_key_combos,
        }

    def list_granted_applications(self) -> dict:
        """Echo the current allowlist + active grant flags (no side effects)."""
        return {
            "applications": sorted(self._allowlist),
            "clipboardRead": self._clipboard_read,
            "clipboardWrite": self._clipboard_write,
            "systemKeyCombos": self._system_key_combos,
            "coordinateMode": "screenshot",
        }

    def read_clipboard(self) -> str:
        """Return the X clipboard contents. Gated on the ``clipboardRead`` grant."""
        if not self._clipboard_read:
            raise ComputerError(
                "read_clipboard requires the clipboardRead grant; call "
                "request_access with clipboardRead=true first"
            )
        if shutil.which("xclip") is None:
            raise ComputerError(
                "xclip is not installed on this host (required for clipboard "
                "access; declare it in the plugin system_deps)"
            )
        argv, env = self.build_xclip(["-selection", "clipboard", "-o"])
        proc = subprocess.run(argv, env=env, capture_output=True, timeout=15)
        if proc.returncode != 0:
            # An empty/unowned clipboard reports "target STRING not available" —
            # treat that as empty rather than a hard failure.
            stderr = proc.stderr.decode(errors="replace").strip()
            if "target" in stderr.lower() or not stderr:
                return ""
            raise ComputerError(f"xclip read failed: {stderr}")
        return proc.stdout.decode(errors="replace")

    def write_clipboard(self, text: str) -> None:
        """Write ``text`` to the X clipboard. Gated on the ``clipboardWrite`` grant."""
        if not self._clipboard_write:
            raise ComputerError(
                "write_clipboard requires the clipboardWrite grant; call "
                "request_access with clipboardWrite=true first"
            )
        if shutil.which("xclip") is None:
            raise ComputerError(
                "xclip is not installed on this host (required for clipboard "
                "access; declare it in the plugin system_deps)"
            )
        argv, env = self.build_xclip(["-selection", "clipboard", "-i"])
        # xclip -i forks a child to own the selection; if we capture stdout/stderr
        # the inherited pipe keeps run() blocked until the selection is replaced.
        # Send them to /dev/null so run() returns once the parent has forked.
        proc = subprocess.run(
            argv,
            input=text.encode(),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if proc.returncode != 0:
            raise ComputerError(f"xclip write failed (exit {proc.returncode})")

    def open_application(self, app: str) -> dict:
        """Best-effort focus of ``app``'s window (wmctrl -a, then xdotool).

        The primary target is the single RDP window, which is already present.
        Degrades to a no-op (never crashes) when neither binary is installed.
        """
        tried: list[str] = []
        if shutil.which("wmctrl"):
            argv, env = self.build_wmctrl(["-a", app])
            proc = subprocess.run(argv, env=env, capture_output=True, timeout=15)
            if proc.returncode == 0:
                return {"app": app, "focused": True, "via": "wmctrl"}
            tried.append("wmctrl")
        if shutil.which("xdotool"):
            argv, env = self.build_xdotool(
                ["search", "--name", app, "windowactivate"]
            )
            proc = subprocess.run(argv, env=env, capture_output=True, timeout=15)
            if proc.returncode == 0:
                return {"app": app, "focused": True, "via": "xdotool"}
            tried.append("xdotool")
        if not tried:
            note = (
                "neither wmctrl nor xdotool is installed; window focus is a "
                "best-effort no-op on this host"
            )
        else:
            note = f"no window matching {app!r} found via {', '.join(tried)}"
        return {"app": app, "focused": False, "note": note}

    def switch_display(self, display=None) -> dict:
        """No-op on the single Xvfb display; report the current display.

        Accepts ``"auto"`` (native's reset-to-automatic) as the same no-op.
        """
        return {
            "display": self.display,
            "switched": False,
            "requested": display,
            "note": (
                f"single display {self.display}; switch_display is a no-op on "
                "Linux/Xvfb"
            ),
        }

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

    # --- click (SCRUM-1401) ----------------------------------------------
    # Pointer clicks at a screenshot-session coordinate, optionally holding
    # modifier key(s) for the duration of the click. Button numbers are the
    # xdotool/X11 convention (1=left, 2=middle, 3=right). The `--` separator and
    # the keydown -> try -> keyup-in-`finally` release are the same machinery the
    # keyboard group proves (no stranded modifier if the click raises).
    _BUTTONS = {"left": 1, "middle": 2, "right": 3}

    @staticmethod
    def _coordinate(coordinate) -> tuple[int, int]:
        """Validate an ``[x, y]`` image-space coordinate (two numbers)."""
        if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
            raise ComputerError("coordinate must be [x, y]")
        try:
            x, y = (int(round(float(v))) for v in coordinate)
        except (TypeError, ValueError):
            raise ComputerError(
                f"coordinate values must be numbers: {coordinate!r}"
            )
        return x, y

    @staticmethod
    def _click_verb(button: str, count: int) -> str:
        """The canonical action name for a (button, count) click, for the ack."""
        if button == "left" and count == 2:
            return "double_click"
        if button == "left" and count == 3:
            return "triple_click"
        base = f"{button}_click"
        return base if count == 1 else f"{base} x{count}"

    def click(
        self,
        coordinate,
        button: str = "left",
        count: int = 1,
        text: str | None = None,
    ) -> str:
        """Click ``button`` ``count`` times at an image-space ``coordinate``.

        The coordinate is mapped to real device pixels via the recorded screenshot
        session (:meth:`to_device`), so a click before any screenshot raises the
        clear "no screenshot session yet" error — look before you click (AC3).
        Optional ``text`` modifier(s) (e.g. ``'ctrl'``, ``'shift+alt'``) are held
        down for the click and ALWAYS released (``keyup`` in ``finally``), the same
        guarantee as :meth:`hold_key` — no stranded modifier if the click raises.
        """
        if button not in self._BUTTONS:
            raise ComputerError(
                f"unknown button {button!r} (expected left/middle/right)"
            )
        x, y = self._coordinate(coordinate)
        dx, dy = self.to_device(x, y)
        argv = ["mousemove", "--sync", dx, dy, "click"]
        if count > 1:
            # Explicit inter-click delay so double/triple register as one gesture.
            argv += ["--repeat", count, "--delay", 100]
        argv.append(self._BUTTONS[button])
        if text:
            # keydown/keyup are separate invocations with keyup in `finally`: the
            # modifier is held across the click and released even if it raises.
            self.run_xdotool(["keydown", "--", text])
            try:
                self.run_xdotool(argv)
            finally:
                self.run_xdotool(["keyup", "--", text])
        else:
            self.run_xdotool(argv)
        ack = f"{self._click_verb(button, count)} at ({dx}, {dy})"
        return ack + (f" holding {text}" if text else "")

    # --- move / drag / scroll (SCRUM-1402) -------------------------------
    # Pointer motion, press-drag-release, scroll wheel, and the stateful
    # left_mouse_down/up pair that holds button 1 ACROSS calls in this
    # persistent process. Image-space coordinates are mapped to device pixels
    # via the screenshot session (to_device), exactly like the click group, so a
    # move before any screenshot raises the same "no screenshot session" guard.
    _SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}

    def mouse_move(self, coordinate) -> str:
        """Move the pointer to an image-space ``coordinate`` without clicking."""
        x, y = self._coordinate(coordinate)
        dx, dy = self.to_device(x, y)
        self.run_xdotool(["mousemove", "--sync", dx, dy])
        return f"moved to ({dx}, {dy})"

    def left_click_drag(self, coordinate, start_coordinate=None) -> str:
        """Press the left button at ``start_coordinate`` (or the current pointer
        position) and drag to ``coordinate`` before releasing.

        Built as ONE xdotool invocation so the press/drag/release land as a single
        gesture and the button is never left down (it is self-contained — it does
        not touch the :attr:`_left_button_down` flag, which only tracks the
        explicit left_mouse_down/up pair). ``start_coordinate`` is optional: when
        omitted the drag begins wherever the pointer currently is.
        """
        x, y = self._coordinate(coordinate)
        dx, dy = self.to_device(x, y)
        argv: list = []
        if start_coordinate is not None:
            sx, sy = self._coordinate(start_coordinate)
            sdx, sdy = self.to_device(sx, sy)
            argv += ["mousemove", "--sync", sdx, sdy]
            origin = f"({sdx}, {sdy})"
        else:
            origin = "the current position"
        argv += ["mousedown", 1, "mousemove", "--sync", dx, dy, "mouseup", 1]
        self.run_xdotool(argv)
        return f"dragged from {origin} to ({dx}, {dy})"

    def scroll(self, coordinate, scroll_direction, scroll_amount, text=None) -> str:
        """Scroll ``scroll_amount`` clicks in ``scroll_direction`` at an
        image-space ``coordinate``, optionally holding ``text`` modifier(s).

        Directions map to X11 wheel buttons (up=4, down=5, left=6, right=7); the
        amount drives xdotool ``click --repeat``. Optional ``text`` modifier(s)
        (e.g. ``'ctrl'``, ``'shift'``) are held for the scroll and ALWAYS released
        (``keyup`` in ``finally``) — the same no-stranded-modifier guarantee as
        :meth:`click` / :meth:`hold_key`.
        """
        if scroll_direction not in self._SCROLL_BUTTONS:
            raise ComputerError(
                f"unknown scroll_direction {scroll_direction!r} "
                "(expected up/down/left/right)"
            )
        try:
            amount = int(scroll_amount)
        except (TypeError, ValueError):
            raise ComputerError(
                f"scroll_amount must be an integer: {scroll_amount!r}"
            )
        if amount < 1:
            raise ComputerError(f"scroll_amount must be >= 1: {amount}")
        x, y = self._coordinate(coordinate)
        dx, dy = self.to_device(x, y)
        button = self._SCROLL_BUTTONS[scroll_direction]
        argv = ["mousemove", "--sync", dx, dy, "click", "--repeat", amount, button]
        if text:
            self.run_xdotool(["keydown", "--", text])
            try:
                self.run_xdotool(argv)
            finally:
                self.run_xdotool(["keyup", "--", text])
        else:
            self.run_xdotool(argv)
        ack = f"scrolled {scroll_direction} x{amount} at ({dx}, {dy})"
        return ack + (f" holding {text}" if text else "")

    def left_mouse_down(self, coordinate=None) -> str:
        """Press and HOLD the left button (button 1), keeping it down across calls
        until :meth:`left_mouse_up`. Raises if a button is already held (the caller
        must release before pressing again). ``coordinate`` optionally moves the
        pointer there first.

        The held flag is set only AFTER the press lands, so a failed dispatch
        (e.g. xdotool missing) never leaves the flag stranded True.
        """
        if self._left_button_down:
            raise ComputerError(
                "left button is already held (call left_mouse_up before pressing "
                "again)"
            )
        argv: list = []
        pos = "the current position"
        if coordinate is not None:
            x, y = self._coordinate(coordinate)
            dx, dy = self.to_device(x, y)
            argv += ["mousemove", "--sync", dx, dy]
            pos = f"({dx}, {dy})"
        argv += ["mousedown", 1]
        self.run_xdotool(argv)
        self._left_button_down = True
        return f"left button down at {pos}"

    def left_mouse_up(self, coordinate=None) -> str:
        """Release a left button held by :meth:`left_mouse_down`. Safe (a no-op)
        when nothing is held. ``coordinate`` optionally moves the pointer first.

        The held flag is cleared only AFTER the release lands, so a failed
        dispatch keeps the flag True and the next reset/up can retry.
        """
        if not self._left_button_down:
            return "left button was not held (no-op)"
        argv: list = []
        pos = "the current position"
        if coordinate is not None:
            x, y = self._coordinate(coordinate)
            dx, dy = self.to_device(x, y)
            argv += ["mousemove", "--sync", dx, dy]
            pos = f"({dx}, {dy})"
        argv += ["mouseup", 1]
        self.run_xdotool(argv)
        self._left_button_down = False
        return f"left button up at {pos}"

    # --- held-state reset (SCRUM-1402 / Q3) ------------------------------
    def reset_held_state(self) -> None:
        """Drop a held left button and clear session grants — idempotent and it
        NEVER raises (it runs on the disconnect/stop, idle, and signal paths).

        This is the auto-release that stops a turn which ended before
        left_mouse_up from stranding a pressed button: the persistent child calls
        it on session idle, on disconnect/stop, and on restart, so the next
        session starts from a clean pointer + grant state.
        """
        if self._left_button_down:
            try:
                self.run_xdotool(["mouseup", "1"])
            except Exception:  # noqa: BLE001 — best-effort release, never raise
                pass
            self._left_button_down = False
        self._allowlist.clear()
        self._clipboard_read = False
        self._clipboard_write = False
        self._system_key_combos = False

    def clear_stranded_button(self) -> None:
        """Best-effort ``mouseup 1`` at startup to clear a button a CRASHED
        predecessor may have left pressed before we took the single-owner lock —
        our own :attr:`_left_button_down` is False on a fresh process, so
        :meth:`reset_held_state` would skip it. ``mouseup`` on an un-pressed button
        is a harmless X no-op. Never raises.
        """
        try:
            self.run_xdotool(["mouseup", "1"])
        except Exception:  # noqa: BLE001 — best-effort, xdotool may be absent
            pass
