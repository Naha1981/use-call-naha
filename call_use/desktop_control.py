"""Safe Windows desktop primitives for the Naha desktop agent."""

from __future__ import annotations

import base64
import io
import os
import subprocess
import webbrowser
from dataclasses import dataclass
from typing import Any

from PIL import Image


class DesktopActionError(ValueError):
    """Raised when a desktop action is invalid or intentionally blocked."""


SAFE_APPS = {
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "explorer": ["explorer.exe"],
    "terminal": ["wt.exe"],
}

SAFE_HOTKEYS = {
    ("ctrl", "a"),
    ("ctrl", "c"),
    ("ctrl", "v"),
    ("ctrl", "x"),
    ("ctrl", "f"),
    ("ctrl", "z"),
    ("ctrl", "y"),
    ("alt", "tab"),
    ("win", "d"),
}

BLOCKED_HOTKEYS = {
    ("alt", "f4"),
    ("ctrl", "alt", "delete"),
    ("ctrl", "shift", "esc"),
}


@dataclass(frozen=True)
class DesktopActionResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None


def capture_screen_jpeg(quality: int = 75) -> bytes:
    """Capture the primary monitor without writing a permanent screenshot."""
    try:
        import mss

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            raw = sct.grab(monitor)
            image = Image.frombytes("RGB", raw.size, raw.rgb)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            return output.getvalue()
    except Exception as exc:
        raise DesktopActionError(f"Screen capture failed: {exc}") from exc


def execute_desktop_action(action: dict[str, Any]) -> DesktopActionResult:
    """Execute an allow-listed desktop action."""
    try:
        import pyautogui
    except ImportError as exc:
        raise DesktopActionError(
            "pyautogui is not installed; run the desktop setup script first"
        ) from exc

    action_type = str(action.get("type", "")).strip().lower()

    if action_type == "open_url":
        url = str(action.get("url", "")).strip()
        if not (url.startswith("https://") or url.startswith("http://")):
            raise DesktopActionError("Only http(s) URLs may be opened")
        webbrowser.open(url)
        return DesktopActionResult(True, f"Opened {url}")

    if action_type == "open_app":
        app = str(action.get("app", "")).strip().lower()
        command = SAFE_APPS.get(app)
        if not command:
            raise DesktopActionError(f"Application {app!r} is not on the safe allow-list")
        subprocess.Popen(command, shell=False)
        return DesktopActionResult(True, f"Opened {app}")

    if action_type == "type_text":
        text = str(action.get("text", ""))
        if not text:
            raise DesktopActionError("type_text requires text")
        if len(text) > 5000:
            raise DesktopActionError("type_text is limited to 5000 characters")
        pyautogui.write(text, interval=0.002)
        return DesktopActionResult(True, f"Typed {len(text)} characters")

    if action_type in {"click", "move"}:
        x = action.get("x")
        y = action.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise DesktopActionError(f"{action_type} requires numeric x and y")
        if action_type == "click":
            pyautogui.click(float(x), float(y))
            return DesktopActionResult(True, f"Clicked at {int(x)},{int(y)}")
        pyautogui.moveTo(float(x), float(y), duration=0.15)
        return DesktopActionResult(True, f"Moved pointer to {int(x)},{int(y)}")

    if action_type == "hotkey":
        raw_keys = action.get("keys")
        if not isinstance(raw_keys, list):
            raise DesktopActionError("hotkey requires a list of keys")
        keys = tuple(str(key).lower().strip() for key in raw_keys)
        if keys in BLOCKED_HOTKEYS:
            raise DesktopActionError("That hotkey is blocked by the safety policy")
        if keys not in SAFE_HOTKEYS:
            raise DesktopActionError(f"Hotkey {keys!r} is not on the safe allow-list")
        pyautogui.hotkey(*keys)
        return DesktopActionResult(True, f"Pressed {"+".join(keys)}")

    raise DesktopActionError(f"Unknown desktop action: {action_type!r}")


def screenshot_base64() -> str:
    return base64.b64encode(capture_screen_jpeg()).decode("ascii")


def is_windows() -> bool:
    return os.name == "nt"
