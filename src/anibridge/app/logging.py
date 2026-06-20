"""Application logging configuration and helpers."""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import ClassVar, cast

import colorama
from colorama import Fore, Style

from anibridge.app.utils.terminal import supports_color

__all__ = [
    "APP_LOGGER_NAME",
    "SUCCESS",
    "CleanFormatter",
    "ColorFormatter",
    "Logger",
    "attach_handler",
    "configure_logging",
    "detach_handler",
    "get_logger",
    "reset_logging",
]

APP_LOGGER_NAME = "anibridge"
SUCCESS = logging.INFO + 5
_MANAGED_HANDLER_ATTRIBUTE = "_anibridge_managed_handler"


logging.addLevelName(SUCCESS, "SUCCESS")


class ColorFormatter(logging.Formatter):
    """Custom formatter that adds terminal colors to log messages."""

    COLORS: ClassVar[dict[str, object]] = {
        "DEBUG": Fore.CYAN,
        "INFO": Fore.GREEN,
        "SUCCESS": Fore.GREEN + Style.BRIGHT,
        "WARNING": Fore.YELLOW,
        "ERROR": Fore.RED,
        "CRITICAL": Fore.RED + Style.BRIGHT,
    }
    QUOTED_PATTERN = re.compile(r"\$\$'((?:[^']|'(?!\$\$))*)'\$\$")
    BRACED_PATTERN = re.compile(r"\$\$\{(.*?)\}\$\$")

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record with ANSI colors for terminal output."""
        orig_msg = record.msg
        orig_args = record.args
        orig_levelname = record.levelname

        record.levelname = (
            f"{self.COLORS.get(record.levelname, '')}{record.levelname}"
            f"{Style.RESET_ALL}"
        )

        message = record.getMessage()
        if isinstance(message, str):
            message = self.QUOTED_PATTERN.sub(
                f"{Fore.LIGHTBLUE_EX}'\\1'{Style.RESET_ALL}", message
            )
            message = self.BRACED_PATTERN.sub(
                f"{Style.DIM}{{\\1}}{Style.RESET_ALL}", message
            )

        record.msg = message
        record.args = ()

        try:
            return super().format(record)
        finally:
            record.levelname = orig_levelname
            record.msg = orig_msg
            record.args = orig_args


class CleanFormatter(logging.Formatter):
    """Formatter that strips color markers from log messages."""

    QUOTED_PATTERN = re.compile(r"\$\$'((?:[^']|'(?!\$\$))*)'\$\$")
    BRACED_PATTERN = re.compile(r"\$\$\{(.*?)\}\$\$")

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record with marker syntax stripped for plain sinks."""
        orig_msg = record.msg
        orig_args = record.args

        message = record.getMessage()
        if isinstance(message, str):
            record.msg = self.QUOTED_PATTERN.sub("'\\1'", message)
            record.msg = self.BRACED_PATTERN.sub("{\\1}", record.msg)
            record.args = ()

            try:
                return super().format(record)
            finally:
                record.msg = orig_msg
                record.args = orig_args

        return super().format(record)


class Logger(logging.Logger):
    """Application logger with SUCCESS support."""

    SUCCESS = SUCCESS

    def success(self, msg, *args, stacklevel=1, **kwargs):
        """Log a message at the custom SUCCESS level."""
        self.log(self.SUCCESS, msg, *args, stacklevel=stacklevel + 1, **kwargs)

    def setup(self, log_level: str | int, log_dir: str | Path | None = None) -> None:
        """Configure console and optional rotating-file handlers for this logger."""
        level = _resolve_level(log_level)
        self.setLevel(level)

        handlers_to_remove = (
            [
                handler
                for handler in self.handlers
                if getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False)
            ]
            if self.name == APP_LOGGER_NAME
            else list(self.handlers)
        )
        for handler in handlers_to_remove:
            self.removeHandler(handler)
            handler.close()

        self.propagate = False

        console_handler = logging.StreamHandler()
        _mark_managed(console_handler)
        console_handler.setLevel(level)
        console_handler.setFormatter(_build_console_formatter(level))
        self.addHandler(console_handler)

        if log_dir is not None:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)
            log_file = log_path / f"{self.name}.{logging.getLevelName(level)}.log"
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
            )
            _mark_managed(file_handler)
            file_handler.setLevel(level)
            file_handler.setFormatter(_build_file_formatter(level))
            self.addHandler(file_handler)


logging.setLoggerClass(Logger)


def _log_format(level: int) -> str:
    if level <= logging.DEBUG:
        return (
            "%(asctime)s - %(levelname)s - %(name)s - %(filename)s:%(lineno)d - "
            "%(message)s"
        )
    return "%(asctime)s - %(levelname)s - %(name)s - %(message)s"


def _build_console_formatter(level: int) -> logging.Formatter:
    try:
        if supports_color():
            if sys.platform == "win32":
                colorama.just_fix_windows_console()
            else:
                colorama.init()
            return ColorFormatter(_log_format(level), datefmt="%Y-%m-%d %H:%M:%S")
    except AttributeError, ImportError, OSError:
        pass

    return CleanFormatter(_log_format(level), datefmt="%Y-%m-%d %H:%M:%S")


def _build_file_formatter(level: int) -> logging.Formatter:
    return CleanFormatter(_log_format(level), datefmt="%Y-%m-%d %H:%M:%S")


def _mark_managed(handler: logging.Handler) -> None:
    setattr(handler, _MANAGED_HANDLER_ATTRIBUTE, True)


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level

    normalized = level.upper()
    if normalized == "SUCCESS":
        return SUCCESS

    resolved = logging.getLevelNamesMapping().get(normalized)
    if not isinstance(resolved, int):
        raise ValueError(f"Unknown log level: {level}")
    return resolved


def get_logger(name: str | None = None) -> Logger:
    """Return an application logger under the AniBridge logger hierarchy."""
    return cast(Logger, logging.getLogger(name or APP_LOGGER_NAME))


def configure_logging(
    *,
    level: str | int = "INFO",
    log_dir: str | Path | None = None,
) -> Logger:
    """Configure and return the root AniBridge application logger."""
    logger = get_logger(APP_LOGGER_NAME)
    logger.setup(level, log_dir)
    return logger


def attach_handler(handler: logging.Handler) -> None:
    """Attach an additional handler to the root AniBridge logger."""
    logger = get_logger(APP_LOGGER_NAME)
    if handler not in logger.handlers:
        logger.addHandler(handler)


def detach_handler(handler: logging.Handler) -> None:
    """Detach a handler from the root AniBridge logger if it is present."""
    logger = get_logger(APP_LOGGER_NAME)
    if handler in logger.handlers:
        logger.removeHandler(handler)


def reset_logging() -> None:
    """Remove handlers from the root logger and restore default propagation."""
    logger = get_logger(APP_LOGGER_NAME)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
