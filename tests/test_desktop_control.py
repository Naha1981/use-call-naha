from __future__ import annotations

import sys
import types

import pytest

from call_use.desktop_control import DesktopActionError, execute_desktop_action


class FakePyAutoGUI:
    FAILSAFE = False
    PAUSE = 0

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return method


@pytest.fixture()
def fake_gui(monkeypatch: pytest.MonkeyPatch) -> FakePyAutoGUI:
    fake = FakePyAutoGUI()
    module = types.SimpleNamespace(FAILSAFE=False, PAUSE=0)

    for method in [
        "click",
        "doubleClick",
        "rightClick",
        "moveTo",
        "write",
        "scroll",
        "press",
        "hotkey",
    ]:
        setattr(module, method, getattr(fake, method))

    monkeypatch.setitem(sys.modules, "pyautogui", module)
    return fake


def test_safe_hotkey_is_executable(fake_gui: FakePyAutoGUI) -> None:
    result = execute_desktop_action({"type": "hotkey", "keys": ["ctrl", "f"]})
    assert result.ok
    assert fake_gui.calls[-1][0] == "hotkey"


def test_blocked_hotkey_is_rejected(fake_gui: FakePyAutoGUI) -> None:
    with pytest.raises(DesktopActionError):
        execute_desktop_action({"type": "hotkey", "keys": ["alt", "f4"]})


def test_safe_press_is_executable(fake_gui: FakePyAutoGUI) -> None:
    result = execute_desktop_action({"type": "press", "key": "enter"})
    assert result.ok
    assert fake_gui.calls[-1][0] == "press"


def test_unsafe_press_is_rejected(fake_gui: FakePyAutoGUI) -> None:
    with pytest.raises(DesktopActionError):
        execute_desktop_action({"type": "press", "key": "delete"})


def test_coordinate_action_requires_numbers(fake_gui: FakePyAutoGUI) -> None:
    with pytest.raises(DesktopActionError):
        execute_desktop_action({"type": "click", "x": "100", "y": 200})
