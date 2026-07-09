"""Tests for the configuration editing service."""

import os
from pathlib import Path

import pytest

from anibridge.app.config.settings import AnibridgeConfig
from anibridge.app.web.services import (
    configuration_service as configuration_service_module,
)
from anibridge.app.web.services.configuration_service import ConfigurationService
from anibridge.app.web.state import get_app_state


def _config_text() -> str:
    return (
        "mappings_url: https://example.com/mappings.json\n"
        "profiles:\n"
        "  default:\n"
        "    source_provider: mocklib\n"
        "    target_provider: mocklist\n"
    )


def test_read_reports_missing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    assert service.read() == {
        "config_path": str(config_path),
        "file_exists": False,
        "content": "",
        "mtime": None,
        "settings": {},
        "settings_error": None,
    }


def test_read_preserves_raw_yaml_parse_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("profiles:\n  default: [\n", encoding="utf-8")
    service = ConfigurationService(config_path=config_path)

    payload = service.read()

    assert payload["content"] == "profiles:\n  default: [\n"
    assert payload["settings"] is None
    assert payload["settings_error"] is not None


@pytest.mark.asyncio
async def test_save_text_validates_persists_and_checks_mtime(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)
    text = _config_text()

    result = await service.save_text(text)

    assert result["config"].profiles["default"].source_provider == "mocklib"
    assert config_path.read_text(encoding="utf-8") == text
    assert result["mtime"] is not None
    assert result["requires_restart"] is True

    await service.save_text(text, expected_mtime=result["mtime"])

    config_path.write_text(text + "# comment\n", encoding="utf-8")
    stat = config_path.stat()
    os.utime(config_path, (stat.st_atime, stat.st_mtime + 1))
    with pytest.raises(FileExistsError):
        await service.save_text(text, expected_mtime=result["mtime"])


@pytest.mark.asyncio
async def test_save_text_rejects_invalid_documents(tmp_path: Path) -> None:
    service = ConfigurationService(config_path=tmp_path / "config.yaml")

    with pytest.raises(ValueError, match="mapping at the root"):
        await service.save_text("- not a mapping")

    with pytest.raises(ValueError, match="Unable to parse configuration"):
        await service.save_text("profiles:\n  broken: []\n")


@pytest.mark.asyncio
async def test_save_settings_rewrites_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    result = await service.save_settings(
        {
            "mappings_url": "https://example.com/mappings.json",
            "profiles": {
                "default": {
                    "source_provider": "mocklib",
                    "target_provider": "mocklist",
                    "scan_interval": 120,
                }
            },
        }
    )

    saved = config_path.read_text(encoding="utf-8")
    assert result["config"].profiles["default"].scan_interval == 120
    assert result["mtime"] is not None
    assert result["requires_restart"] is True
    assert "scan_interval: 120" in saved
    assert saved.endswith("\n")


@pytest.mark.asyncio
async def test_save_settings_applies_profile_changes_without_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current_config = AnibridgeConfig.model_validate(
        {
            "mappings_url": "https://example.com/mappings.json",
            "profiles": {
                "default": {
                    "source_provider": "mocklib",
                    "target_provider": "mocklist",
                }
            },
        }
    )
    app_state = get_app_state()
    reinitialized: list[str] = []
    removed: list[str] = []
    notified = False

    class _Scheduler:
        def __init__(self) -> None:
            self.global_config = current_config

        async def reinitialize_profile(self, profile_name: str) -> None:
            reinitialized.append(profile_name)

        async def remove_profile(self, profile_name: str) -> None:
            removed.append(profile_name)

    def _notify_status_change() -> None:
        nonlocal notified
        notified = True

    monkeypatch.setattr(app_state, "scheduler", _Scheduler())
    monkeypatch.setattr(app_state, "notify_status_change", _notify_status_change)
    service = ConfigurationService(config_path=tmp_path / "config.yaml")

    result = await service.save_settings(
        {
            "mappings_url": "https://example.com/mappings.json",
            "profiles": {
                "default": {
                    "source_provider": "mocklib",
                    "target_provider": "mocklist",
                    "scan_interval": 120,
                }
            },
        }
    )

    assert result["requires_restart"] is False
    assert reinitialized == ["default"]
    assert removed == []
    assert notified is True
    assert app_state.scheduler.global_config.profiles["default"].scan_interval == 120


@pytest.mark.asyncio
async def test_save_settings_requires_restart_for_process_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current_config = AnibridgeConfig.model_validate(
        {
            "mappings_url": "https://example.com/mappings.json",
            "profiles": {
                "default": {
                    "source_provider": "mocklib",
                    "target_provider": "mocklist",
                }
            },
        }
    )
    app_state = get_app_state()

    class _Scheduler:
        global_config = current_config

        async def reinitialize_profile(self, profile_name: str) -> None:
            raise AssertionError("profile should not be reinitialized")

    monkeypatch.setattr(app_state, "scheduler", _Scheduler())
    service = ConfigurationService(config_path=tmp_path / "config.yaml")

    result = await service.save_settings(
        {
            "mappings_url": "https://example.com/updated.json",
            "profiles": {
                "default": {
                    "source_provider": "mocklib",
                    "target_provider": "mocklist",
                    "scan_interval": 120,
                }
            },
        }
    )

    assert result["requires_restart"] is True
    assert app_state.scheduler.global_config is current_config


def test_configuration_service_exposes_config_path_and_mtime(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    assert service.config_path == config_path.resolve()
    assert service.read()["mtime"] is None


def test_get_configuration_service_returns_singleton(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        configuration_service_module,
        "find_yaml_config_file",
        lambda: tmp_path / "config.yaml",
    )
    configuration_service_module.get_configuration_service.cache_clear()

    first = configuration_service_module.get_configuration_service()
    second = configuration_service_module.get_configuration_service()

    assert first is second
    configuration_service_module.get_configuration_service.cache_clear()
