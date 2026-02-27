"""Tests for the configuration editing service."""

import os
from pathlib import Path

import pytest

from anibridge.app.web.services.configuration_service import ConfigurationService


def test_load_document_text_reports_missing_file(tmp_path: Path):
    """Missing configuration files return empty content with metadata."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    payload = service.load_document_text()
    assert payload["config_path"] == str(config_path)
    assert payload["file_exists"] is False
    assert payload["content"] == ""
    assert payload["mtime"] is None


@pytest.mark.asyncio
async def test_save_document_text_validates_and_persists(tmp_path: Path):
    """Saving text writes to disk and enforces optimistic locking."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)
    text = (
        "profiles:\n"
        "  default:\n"
        "    library_provider: mocklib\n"
        "    list_provider: mocklist\n"
    )

    config, mtime = await service.save_document_text(text)
    assert config.profiles["default"].library_provider == "mocklib"
    assert mtime is not None

    # Matching mtime succeeds
    await service.save_document_text(text, expected_mtime=mtime)

    # Diverging mtime raises an error
    config_path.write_text(text + "# comment\n", encoding="utf-8")
    stat = config_path.stat()
    os.utime(config_path, (stat.st_atime, stat.st_mtime + 1))
    with pytest.raises(FileExistsError):
        await service.save_document_text(text, expected_mtime=mtime)


@pytest.mark.asyncio
async def test_save_document_text_rejects_invalid_yaml(tmp_path: Path):
    """Invalid YAML or payloads that are not mappings raise ValueError."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    with pytest.raises(ValueError):
        await service.save_document_text("- not a mapping")
