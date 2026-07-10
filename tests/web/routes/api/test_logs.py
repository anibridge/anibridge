"""Tests for log history API file discovery."""

import logging
from collections.abc import Callable
from typing import cast

import pytest
from litestar.response import File

from anibridge.app.exceptions import InvalidLogFileNameError
from anibridge.app.web.routes.api import logs as logs_api


def test_list_log_files_case_insensitive_filename_match(tmp_path, monkeypatch):
    """File discovery is case-insensitive, but only lowercase filename is active."""
    list_log_files = cast(
        Callable[[], list[logs_api.LogFileModel]],
        logs_api.list_log_files.fn,
    )
    mixed_case = tmp_path / "AniBridge.INFO.log"
    mixed_case.write_text("line\n", encoding="utf-8")
    lower_case = tmp_path / "anibridge.INFO.log"
    lower_case.write_text("line\n", encoding="utf-8")

    monkeypatch.setattr(logs_api, "LOG_DIR", tmp_path)

    logger = logging.getLogger("anibridge")
    original_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        result = list_log_files()
    finally:
        logger.setLevel(original_level)

    mixed_entry = next(item for item in result if item.name == "AniBridge.INFO.log")
    lower_entry = next(item for item in result if item.name == "anibridge.INFO.log")

    assert mixed_entry.current is False
    assert lower_entry.current is True


def test_get_log_file_returns_tail_when_length_limited(tmp_path, monkeypatch):
    """Length-limited reads should return the final N lines, oldest to newest."""
    get_log_file = cast(
        Callable[[str, int], list[logs_api.LogEntryModel]],
        logs_api.get_log_file.fn,
    )
    log_file = tmp_path / "anibridge.DEBUG.log"
    log_file.write_text(
        "\n".join([f"line-{i}" for i in range(1, 11)]) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(logs_api, "LOG_DIR", tmp_path)

    result = get_log_file("anibridge.DEBUG.log", 3)

    assert [entry.message for entry in result] == ["line-8", "line-9", "line-10"]


def test_get_log_file_returns_all_lines_when_unlimited(tmp_path, monkeypatch):
    """A lines=0 request should return every line in file order."""
    get_log_file = cast(
        Callable[[str, int], list[logs_api.LogEntryModel]],
        logs_api.get_log_file.fn,
    )
    log_file = tmp_path / "anibridge.DEBUG.log"
    log_file.write_text(
        "\n".join([f"line-{i}" for i in range(1, 6)]) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(logs_api, "LOG_DIR", tmp_path)

    result = get_log_file("anibridge.DEBUG.log", 0)

    assert [entry.message for entry in result] == [
        "line-1",
        "line-2",
        "line-3",
        "line-4",
        "line-5",
    ]


def test_download_log_file_returns_raw_attachment(tmp_path, monkeypatch):
    """Raw downloads should return the requested log file as an attachment."""
    download_log_file = cast(Callable[[str], File], logs_api.download_log_file.fn)
    log_file = tmp_path / "anibridge.DEBUG.log"
    log_file.write_text("original raw log line\n", encoding="utf-8")

    monkeypatch.setattr(logs_api, "LOG_DIR", tmp_path)

    result = download_log_file("anibridge.DEBUG.log")

    assert result.filename == "anibridge.DEBUG.log"
    assert result.media_type == "text/plain"
    assert result.content_disposition_type == "attachment"


def test_get_log_file_rejects_symlink_escape(tmp_path, monkeypatch):
    """Log file resolution should reject symlinks escaping the log directory."""
    get_log_file = cast(
        Callable[[str, int], list[logs_api.LogEntryModel]],
        logs_api.get_log_file.fn,
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    escaped_dir = tmp_path / "logs_evil"
    escaped_dir.mkdir()
    escaped_file = escaped_dir / "anibridge.DEBUG.log"
    escaped_file.write_text("line\n", encoding="utf-8")
    (log_dir / "anibridge.DEBUG.log").symlink_to(escaped_file)

    monkeypatch.setattr(logs_api, "LOG_DIR", log_dir)

    with pytest.raises(InvalidLogFileNameError):
        get_log_file("anibridge.DEBUG.log", 1)


def test_get_log_file_tail_without_trailing_newline(tmp_path, monkeypatch):
    """Tail reads should work when the file does not end with a newline."""
    get_log_file = cast(
        Callable[[str, int], list[logs_api.LogEntryModel]],
        logs_api.get_log_file.fn,
    )
    log_file = tmp_path / "anibridge.DEBUG.log"
    log_file.write_text("\n".join([f"line-{i}" for i in range(1, 6)]), encoding="utf-8")

    monkeypatch.setattr(logs_api, "LOG_DIR", tmp_path)

    result = get_log_file("anibridge.DEBUG.log", 2)

    assert [entry.message for entry in result] == ["line-4", "line-5"]
