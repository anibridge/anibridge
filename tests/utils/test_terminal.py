"""Tests for terminal capability helpers."""

import locale
import os
import sys
from types import SimpleNamespace

import colorama
import pytest

from anibridge.app.utils.terminal import supports_color, supports_utf8


@pytest.fixture(autouse=True)
def clear_terminal_caches() -> None:
    """Clear the caches for terminal capability functions before each test."""
    supports_utf8.cache_clear()
    supports_color.cache_clear()


def _fake_stdout(*, encoding: str | None, isatty: bool) -> SimpleNamespace:
    """Create a fake stdout object with specified encoding and isatty behavior."""
    return SimpleNamespace(encoding=encoding, isatty=lambda: isatty)


def test_supports_utf8_true_with_stdout_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that supports_utf8 returns True when stdout encoding is UTF-8."""
    monkeypatch.setattr(sys, "stdout", _fake_stdout(encoding="UTF-8", isatty=True))

    assert supports_utf8()


def test_supports_utf8_uses_locale_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that supports_utf8 falls back to locale encoding when encoding is None."""
    monkeypatch.setattr(sys, "stdout", _fake_stdout(encoding=None, isatty=True))
    monkeypatch.setattr(locale, "getpreferredencoding", lambda _: "latin-1")

    assert not supports_utf8()


def test_supports_color_false_when_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that supports_color returns False when stdout is not a TTY."""
    monkeypatch.setattr(sys, "stdout", _fake_stdout(encoding="UTF-8", isatty=False))
    monkeypatch.setattr(sys, "platform", "linux")

    assert not supports_color()


def test_supports_color_true_on_linux_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that supports_color returns True on Linux TTY with UTF-8 encoding."""
    monkeypatch.setattr(sys, "stdout", _fake_stdout(encoding="UTF-8", isatty=True))
    monkeypatch.setattr(sys, "platform", "linux")

    assert supports_color()


def test_supports_color_windows_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that supports_color returns True on Windows with WT_SESSION set."""
    monkeypatch.setattr(sys, "stdout", _fake_stdout(encoding="UTF-8", isatty=True))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("WT_SESSION", "1")

    assert supports_color()


def test_supports_color_windows_without_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that supports_color returns False on Windows without terminal."""
    monkeypatch.setattr(sys, "stdout", _fake_stdout(encoding="UTF-8", isatty=True))
    monkeypatch.setattr(sys, "platform", "win32")
    for key in ["WT_SESSION", "ANSICON", "TERM_PROGRAM"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(os.environ, "copy", lambda: {})

    assert not supports_color()


def test_supports_color_windows_registry_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry flag enables color on Windows when other signals are absent."""
    monkeypatch.setattr(sys, "stdout", _fake_stdout(encoding="UTF-8", isatty=True))
    monkeypatch.setattr(sys, "platform", "win32")
    for key in ["WT_SESSION", "ANSICON", "TERM_PROGRAM"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(colorama, "fixed_windows_console", False, raising=False)

    class DummyWinReg:
        HKEY_CURRENT_USER = object()

        @staticmethod
        def OpenKey(*_args, **_kwargs):
            return object()

        @staticmethod
        def QueryValueEx(_key, _value_name):
            return (1, None)

    monkeypatch.setitem(sys.modules, "winreg", DummyWinReg)

    assert supports_color()


def test_supports_color_windows_registry_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing registry key yields False when no other hints exist."""
    monkeypatch.setattr(sys, "stdout", _fake_stdout(encoding="UTF-8", isatty=True))
    monkeypatch.setattr(sys, "platform", "win32")
    for key in ["WT_SESSION", "ANSICON", "TERM_PROGRAM"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(colorama, "fixed_windows_console", False, raising=False)

    class MissingWinReg:
        HKEY_CURRENT_USER = object()

        @staticmethod
        def OpenKey(*_args, **_kwargs):
            raise FileNotFoundError

        @staticmethod
        def QueryValueEx(_key, _value_name):
            return (0, None)

    monkeypatch.setitem(sys.modules, "winreg", MissingWinReg)

    assert not supports_color()
