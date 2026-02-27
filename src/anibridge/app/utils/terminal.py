"""Terminal Utilities Module."""

import locale
import os
import sys

import colorama
from anibridge.utils.cache import cache

__all__ = ["ARROW", "supports_color", "supports_utf8"]

ARROW = ARROW = "→" if sys.stdout.encoding == "utf-8" else "->"


@cache
def supports_utf8() -> bool:
    """Check if the terminal supports UTF-8 encoding.

    Returns:
        bool: True if the terminal supports UTF-8 encoding, False otherwise
    """
    encoding = sys.stdout.encoding or locale.getpreferredencoding(False)
    return encoding.lower().startswith("utf")


@cache
def supports_color() -> bool:
    """Check if the terminal supports ANSI color codes.

    Detects if the terminal supports ANSI color codes by checking platform-specific
    conditions and environment variables. On Windows, it also checks the Windows
    registry for the VirtualTerminalLevel key.

    Returns:
        bool: True if the terminal supports color, False otherwise
    """

    def vt_codes_enabled_in_windows_registry():
        if sys.platform != "win32":
            return False

        try:
            import winreg
        except ImportError:
            return False

        try:
            reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Console")
            reg_key_value, _ = winreg.QueryValueEx(reg_key, "VirtualTerminalLevel")
            return reg_key_value == 1
        except FileNotFoundError:
            return False

    is_a_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    if not is_a_tty:
        return False

    if sys.platform == "win32":
        return (
            getattr(colorama, "fixed_windows_console", False)
            or "ANSICON" in os.environ
            or "WT_SESSION" in os.environ  # Windows Terminal
            or os.environ.get("TERM_PROGRAM") == "vscode"
            or vt_codes_enabled_in_windows_registry()
        )

    return True
