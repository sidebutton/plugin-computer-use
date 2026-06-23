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
