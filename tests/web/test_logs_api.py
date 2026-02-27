"""Tests for log history API file discovery."""

import logging

from anibridge.app.web.routes.api import logs as logs_api


def test_list_log_files_case_insensitive_filename_match(tmp_path, monkeypatch):
    """File discovery is case-insensitive, but only lowercase filename is active."""
    mixed_case = tmp_path / "AniBridge.INFO.log"
    mixed_case.write_text("line\n", encoding="utf-8")
    lower_case = tmp_path / "anibridge.INFO.log"
    lower_case.write_text("line\n", encoding="utf-8")

    monkeypatch.setattr(logs_api, "LOG_DIR", tmp_path)

    logger = logging.getLogger("anibridge")
    original_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        result = logs_api.list_log_files()
    finally:
        logger.setLevel(original_level)

    mixed_entry = next(item for item in result if item.name == "AniBridge.INFO.log")
    lower_entry = next(item for item in result if item.name == "anibridge.INFO.log")

    assert mixed_entry.current is False
    assert lower_entry.current is True
