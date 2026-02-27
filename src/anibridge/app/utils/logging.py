"""Logging utilities module."""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import ClassVar

import colorama
from anibridge.utils.cache import cache
from colorama import Fore, Style

__all__ = ["Logger", "get_logger"]


class ColorFormatter(logging.Formatter):
    """Custom formatter that adds terminal colors to log messages.

    Enhances log readability by adding color coding to different components:
    - Log levels are colored according to severity
    - Quoted strings are highlighted in light blue
    - Curly brace values are dimmed

    Color Scheme:
        DEBUG: Cyan
        INFO: Green
        SUCCESS: Light Cyan
        WARNING: Yellow
        ERROR: Red
        CRITICAL: Bright Red
        Quoted values: Light Blue (e.g., $$'example'$$)
        Bracketed values: Dimmed (e.g., $${key: value}$$)
    """

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
        """Formats a log record with ANSI color codes.

        Args:
            record (logging.LogRecord): Log record to format

        Returns:
            str: Color-formatted log message
        """
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
            result = super().format(record)
        finally:
            record.levelname = orig_levelname
            record.msg = orig_msg
            record.args = orig_args

        return result


class CleanFormatter(logging.Formatter):
    """Formatter that strips color markers from log messages.

    Used for file output where color codes are unnecessary and would
    reduce readability. Removes the special markers while preserving
    the content within them.
    """

    QUOTED_PATTERN = re.compile(r"\$\$'((?:[^']|'(?!\$\$))*)'\$\$")
    BRACED_PATTERN = re.compile(r"\$\$\{(.*?)\}\$\$")

    def format(self, record: logging.LogRecord) -> str:
        """Formats a log record by removing color markers.

        Args:
            record (logging.LogRecord): Log record to format

        Returns:
            str: Clean log message without color markers

        """
        orig_msg = record.msg
        orig_args = record.args

        message = record.getMessage()
        if isinstance(message, str):
            cleaned_msg = re.sub(self.QUOTED_PATTERN, "'\\1'", message)
            cleaned_msg = re.sub(self.BRACED_PATTERN, "{\\1}", cleaned_msg)
            record.msg = cleaned_msg
            record.args = ()

            try:
                return super().format(record)
            finally:
                record.msg = orig_msg
                record.args = orig_args

        return super().format(record)


class Logger(logging.Logger):
    """Extended Logger class with class name prefixing and additional log levels."""

    SUCCESS = logging.INFO + 5

    def __init__(self, name, level=logging.NOTSET):
        """Initialize the logger.

        Args:
            name (str): Logger name
            level (int, optional): Initial logging level. Defaults to NOTSET.
        """
        super().__init__(name, level)

        if not hasattr(logging, "SUCCESS"):
            logging.addLevelName(self.SUCCESS, "SUCCESS")

    def _log(
        self,
        level,
        msg,
        args,
        exc_info=None,
        extra=None,
        stack_info=False,
        stacklevel=1,
    ):
        """Override _log to automatically prefix messages with class name.

        Only used for the debug level.

        Inspects the call stack to determine if the log call originated from
        within a class method. If so, prefixes the message with the class name.
        """
        #
        if self.level <= logging.DEBUG:
            try:
                # From _log's perspective, the call stack is:
                # Frame 0: _log itself
                # Frame 1: The logging method (info, debug, success, etc.)
                # Frame 2: User's code (the actual caller we want)
                # We always inspect frame 2 to get the user's class context
                frame = sys._getframe(2)
                class_name = None
                if "self" in frame.f_locals:
                    obj = frame.f_locals["self"]
                    # Make sure it's not the Logger instance itself
                    if not isinstance(obj, logging.Logger):
                        class_name = obj.__class__.__name__

                elif "cls" in frame.f_locals:
                    cls = frame.f_locals["cls"]
                    if isinstance(cls, type):
                        class_name = cls.__name__

                if class_name and isinstance(msg, str):
                    msg = f"{class_name} - {msg}"
            except ValueError, KeyError, AttributeError:
                pass

        # Add 1 to stacklevel to account for this wrapper method
        super()._log(
            level,
            msg,
            args,
            exc_info=exc_info,
            extra=extra,
            stack_info=stack_info,
            stacklevel=stacklevel + 1,
        )

    def success(self, msg, *args, stacklevel=1, **kwargs):
        """Log a message with SUCCESS level.

        Args:
            msg: Message to log
            *args: Variable length argument list
            stacklevel: Stack level for logging
            **kwargs: Arbitrary keyword arguments
        """
        self.log(self.SUCCESS, msg, *args, stacklevel=stacklevel + 1, **kwargs)

    def setup(self, log_level: str, log_dir: str | None = None) -> None:
        """Configure the logger with console and file output.

        Creates a logger that writes to both console (with colors) and a rotating
        log file (without colors). The log format varies based on log level.

        Args:
            log_level (str): Logging level ('DEBUG', 'INFO', 'SUCCESS', etc.)
            log_dir (str | None, optional): Directory where log files will be stored.
        """
        has_color_support = False
        try:
            from anibridge.app.utils.terminal import supports_color

            if supports_color():
                if sys.platform == "win32":
                    colorama.just_fix_windows_console()
                else:
                    colorama.init()
                has_color_support = True
        except AttributeError, ImportError, OSError:
            has_color_support = False

        if log_level == "SUCCESS":
            log_level_literal = self.SUCCESS
        else:
            log_level_literal = getattr(logging, log_level)

        self.setLevel(log_level_literal)

        for handler in self.handlers[:]:
            self.removeHandler(handler)

        log_format = (
            "%(asctime)s - %(levelname)s - %(name)s - %(filename)s:%(lineno)d - "
            "%(message)s"
            if log_level_literal <= logging.DEBUG
            else "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
        )

        file_formatter = CleanFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

        console_formatter = (
            ColorFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")
            if has_color_support
            else CleanFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")
        )

        if log_dir is not None:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)

            log_file = log_path / f"{self.name}.{log_level}.log"
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10MB
                backupCount=5,
            )
            file_handler.setFormatter(file_formatter)
            file_handler.setLevel(log_level_literal)
            self.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(log_level_literal)
        self.addHandler(console_handler)


logging.setLoggerClass(Logger)


def _get_logger(
    log_name: str, log_level: str = "INFO", log_dir: str | Path | None = None
) -> Logger:
    """Get a configured instance of Logger.

    Args:
        log_name (str): Name of the logger and base name for log file.
        log_level (str): Logging level. Defaults to "INFO".
        log_dir (str | Path | None): Directory where log files will be stored.

    Returns:
        Logger: Configured logger instance
    """
    logger = logging.getLogger(log_name)
    log_dir = str(log_dir) if log_dir is not None else None

    if isinstance(logger, Logger):
        logger.setup(log_level, log_dir)
    else:
        logger = Logger(log_name)
        logger.setup(log_level, log_dir)

    return logger


@cache
def get_logger() -> Logger:
    """Get the main application logger.

    Returns:
        Logger: Main application logger instance
    """
    from anibridge.app.config.settings import get_config

    config = get_config()

    return _get_logger(
        log_name="anibridge",
        log_level=config.log_level,
        log_dir=config.data_path / "logs",
    )
