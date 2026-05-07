"""Tests for the configuration editing service."""

import os
from pathlib import Path

import pytest

from anibridge.app.web.services import (
    configuration_service as configuration_service_module,
)
from anibridge.app.web.services.configuration_service import ConfigurationService


def _config_text(
    *,
    mappings_url: str | None = "https://example.com/mappings-a.json",
    threads: int | None = None,
    profiles: str = (
        "profiles:\n"
        "  default:\n"
        "    library_provider: mocklib\n"
        "    list_provider: mocklist\n"
    ),
) -> str:
    lines: list[str] = []
    if mappings_url is not None:
        lines.append(f"mappings_url: {mappings_url}")
    if threads is not None:
        lines.append(f"threads: {threads}")
    lines.append(profiles.rstrip())
    return "\n".join(lines) + "\n"


def _runtime_config(text: str):
    payload = configuration_service_module.yaml.safe_load(text)
    return configuration_service_module.AnibridgeConfig.model_validate(payload)


class _SchedulerStub:
    def __init__(self) -> None:
        self.removed_profiles: list[str] = []
        self.reinitialized_profiles: list[str] = []
        self.database_sync_sources: list[str] = []
        self.shared_animap_client = type(
            "Animap",
            (),
            {
                "upstream_url": None,
                "mappings_client": type("Mappings", (), {"upstream_url": None})(),
            },
        )()

    async def remove_profile(self, profile_name: str) -> None:
        self.removed_profiles.append(profile_name)

    async def reinitialize_profile(self, profile_name: str) -> None:
        self.reinitialized_profiles.append(profile_name)

    async def trigger_database_sync(self, source: str = "manual:database") -> None:
        self.database_sync_sources.append(source)


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
async def test_save_document_text_validates_and_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Saving text writes to disk and enforces optimistic locking."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)
    text = _config_text()
    runtime_config = _runtime_config(text)

    monkeypatch.setattr(
        configuration_service_module, "get_config", lambda: runtime_config
    )
    monkeypatch.setattr(
        configuration_service_module,
        "get_app_state",
        lambda: type("State", (), {"scheduler": None})(),
    )

    config, requires_restart, mtime = await service.save_document_text(text)
    assert config.profiles["default"].library_provider == "mocklib"
    assert requires_restart is False
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
async def test_save_document_text_rejects_invalid_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Invalid YAML or payloads that are not mappings raise ValueError."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    monkeypatch.setattr(
        configuration_service_module,
        "get_config",
        lambda: configuration_service_module.AnibridgeConfig.model_validate({}),
    )
    monkeypatch.setattr(
        configuration_service_module,
        "get_app_state",
        lambda: type("State", (), {"scheduler": None})(),
    )

    with pytest.raises(ValueError):
        await service.save_document_text("- not a mapping")


@pytest.mark.asyncio
async def test_save_document_text_applies_live_profile_and_mapping_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Live-safe profile and mappings changes should update runtime state."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    initial_text = _config_text(mappings_url="https://example.com/mappings-a.json")
    updated_text = _config_text(
        mappings_url="https://example.com/mappings-b.json",
        profiles=(
            "profiles:\n"
            "  default:\n"
            "    library_provider: mocklib\n"
            "    list_provider: mocklist\n"
            "    scan_interval: 120\n"
        ),
    )
    runtime_config = _runtime_config(initial_text)
    scheduler = _SchedulerStub()

    monkeypatch.setattr(
        configuration_service_module, "get_config", lambda: runtime_config
    )
    monkeypatch.setattr(
        configuration_service_module,
        "get_app_state",
        lambda: type("State", (), {"scheduler": scheduler})(),
    )

    _, requires_restart, _ = await service.save_document_text(updated_text)

    assert requires_restart is False
    assert runtime_config.mappings_url == "https://example.com/mappings-b.json"
    assert runtime_config.profiles["default"].scan_interval == 120
    assert scheduler.reinitialized_profiles == ["default"]
    assert scheduler.database_sync_sources == ["api:config:mappings_url"]
    assert (
        scheduler.shared_animap_client.mappings_client.upstream_url
        == "https://example.com/mappings-b.json"
    )


@pytest.mark.asyncio
async def test_save_document_text_marks_restart_only_fields_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Restart-only fields should be persisted without mutating the runtime config."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    initial_text = _config_text(threads=2)
    updated_text = _config_text(threads=8)
    runtime_config = _runtime_config(initial_text)
    scheduler = _SchedulerStub()

    monkeypatch.setattr(
        configuration_service_module, "get_config", lambda: runtime_config
    )
    monkeypatch.setattr(
        configuration_service_module,
        "get_app_state",
        lambda: type("State", (), {"scheduler": scheduler})(),
    )

    _, requires_restart, _ = await service.save_document_text(updated_text)

    assert requires_restart is True
    assert runtime_config.threads == 2
    assert scheduler.reinitialized_profiles == []
    assert scheduler.database_sync_sources == []


@pytest.mark.asyncio
async def test_save_document_text_marks_nested_restart_only_fields_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nested restart-only fields should also be detected by field path."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    initial_text = _config_text()
    updated_text = (
        "mappings_url: https://example.com/mappings-a.json\n"
        "web:\n"
        "  host: 0.0.0.0\n"
        "profiles:\n"
        "  default:\n"
        "    library_provider: mocklib\n"
        "    list_provider: mocklist\n"
    )
    runtime_config = _runtime_config(initial_text)
    scheduler = _SchedulerStub()

    monkeypatch.setattr(
        configuration_service_module, "get_config", lambda: runtime_config
    )
    monkeypatch.setattr(
        configuration_service_module,
        "get_app_state",
        lambda: type("State", (), {"scheduler": scheduler})(),
    )

    _, requires_restart, _ = await service.save_document_text(updated_text)

    assert requires_restart is True
    assert runtime_config.web.host == ""
    assert scheduler.reinitialized_profiles == []


@pytest.mark.asyncio
async def test_save_document_text_adds_and_removes_profiles_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Added and removed profiles should be reflected immediately at runtime."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    initial_text = _config_text(
        profiles=(
            "profiles:\n"
            "  alpha:\n"
            "    library_provider: mocklib\n"
            "    list_provider: mocklist\n"
            "  beta:\n"
            "    library_provider: mocklib\n"
            "    list_provider: mocklist\n"
        ),
    )
    updated_text = _config_text(
        profiles=(
            "profiles:\n"
            "  alpha:\n"
            "    library_provider: mocklib\n"
            "    list_provider: mocklist\n"
            "  gamma:\n"
            "    library_provider: mocklib\n"
            "    list_provider: mocklist\n"
        ),
    )
    runtime_config = _runtime_config(initial_text)
    scheduler = _SchedulerStub()

    monkeypatch.setattr(
        configuration_service_module, "get_config", lambda: runtime_config
    )
    monkeypatch.setattr(
        configuration_service_module,
        "get_app_state",
        lambda: type("State", (), {"scheduler": scheduler})(),
    )

    _, requires_restart, _ = await service.save_document_text(updated_text)

    assert requires_restart is False
    assert sorted(runtime_config.profiles) == ["alpha", "gamma"]
    assert scheduler.removed_profiles == ["beta"]
    assert scheduler.reinitialized_profiles == ["gamma"]


def test_configuration_service_exposes_config_path_and_mtime(tmp_path: Path) -> None:
    """The service should expose its resolved config path and mtime metadata."""
    config_path = tmp_path / "config.yaml"
    service = ConfigurationService(config_path=config_path)

    assert service.config_path == config_path.resolve()
    assert service.load_document_text()["mtime"] is None


def test_get_configuration_service_returns_singleton(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cached configuration service factory should be stable per process."""
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
