"""Tests for logging utilities."""

import logging
from logging.handlers import RotatingFileHandler

import colorama
import pytest

import anibridge.app.utils.logging as logging_module
import anibridge.app.utils.terminal as terminal_module
from anibridge.app.utils.logging import CleanFormatter, ColorFormatter, Logger


def test_color_formatter_applies_color_codes():
    """Test that ColorFormatter applies color codes to marked sections."""
    formatter = ColorFormatter("%(levelname)s:%(message)s")
    original_message = "$$'value'$$ $${key: value}$$ message"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=original_message,
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)

    assert str(colorama.Fore.GREEN) in formatted
    assert str(colorama.Fore.LIGHTBLUE_EX) in formatted
    assert str(colorama.Style.DIM) in formatted
    assert record.msg == original_message


def test_clean_formatter_removes_markers():
    """Test that CleanFormatter removes special markers from the message."""
    formatter = CleanFormatter("%(message)s")
    original_message = "wrapped $$'value'$$ and $${key: 1}$$"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=original_message,
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)

    assert "$$" not in formatted
    assert "'value'" in formatted
    assert "{key: 1}" in formatted
    assert record.msg == original_message


def test_logger_prefixes_class_name():
    """Test that Logger prefixes messages with the class name."""
    logger = Logger("test")
    logger.setLevel(logging.DEBUG)
    captured = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    logger.addHandler(ListHandler())

    class Sample:
        def __init__(self, bound_logger: Logger):
            self.log = bound_logger

        def run(self):
            self.log.info("hello")

    Sample(logger).run()

    assert captured and captured[0] == "Sample - hello"


def test_logger_prefixes_classmethod_calls():
    """Logger should prefix classmethod calls using the cls variable when debug."""
    logger = Logger("test")
    logger.setLevel(logging.DEBUG)
    captured: list[str] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    logger.addHandler(CaptureHandler())

    class Sample:
        log = logger

        @classmethod
        def run(cls):
            cls.log.info("clazz")

    Sample.run()

    assert captured and captured[0] == "Sample - clazz"


def test_logger_success_level_records_message():
    """Test that Logger logs messages at SUCCESS level."""
    logger = Logger("test")
    logger.setLevel(Logger.SUCCESS)
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger.addHandler(CaptureHandler())

    logger.success("operation complete")

    assert records, "Expected at least one log record"
    record = records[0]
    assert record.levelno == Logger.SUCCESS
    assert record.levelname == "SUCCESS"
    assert record.getMessage() == "operation complete"


def test_clean_formatter_handles_non_string_messages():
    """CleanFormatter should delegate to base formatter for non-str messages."""
    formatter = CleanFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg={"value": 1},
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)

    assert formatted == "{'value': 1}"


def test_logger_setup_creates_file_and_console_handlers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup should honor SUCCESS level and configure both handlers."""
    logger = Logger("setup-test")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    monkeypatch.setattr(terminal_module, "supports_color", lambda: True)
    monkeypatch.setattr(logging_module.sys, "platform", "linux")
    monkeypatch.setattr(logging_module.colorama, "init", lambda: None)
    monkeypatch.setattr(
        logging_module.colorama, "just_fix_windows_console", lambda: None
    )

    logger.setup("SUCCESS", log_dir=str(tmp_path))

    log_file = tmp_path / "setup-test.SUCCESS.log"
    assert log_file.exists()
    handler_types = {type(handler) for handler in logger.handlers}
    assert any(issubclass(htype, RotatingFileHandler) for htype in handler_types)
    assert any(issubclass(htype, logging.StreamHandler) for htype in handler_types)

    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)


def test_logger_setup_handles_color_detection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup continues even if supports_color raises an error."""
    logger = Logger("color-test")
    logger.addHandler(logging.NullHandler())

    def _raise_os_error():
        raise OSError("boom")

    monkeypatch.setattr(terminal_module, "supports_color", _raise_os_error)

    logger.setup("INFO")

    # Existing handler should be replaced by console handler.
    assert len(logger.handlers) == 1
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
