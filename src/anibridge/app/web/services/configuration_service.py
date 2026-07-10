"""Read and write AniBridge configuration documents."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

import yaml
from anibridge.utils.cache import cache

from anibridge.app.config.settings import (
    AnibridgeConfig,
    _ConfigDumper,
    find_yaml_config_file,
    get_config,
)
from anibridge.app.logging import get_logger
from anibridge.app.web.state import get_app_state

__all__ = ["ConfigurationService", "get_configuration_service"]

log = get_logger(__name__)


class ConfigDocumentPayload(TypedDict):
    config_path: str
    file_exists: bool
    content: str
    mtime: int | None
    settings: dict[str, object] | None
    settings_error: str | None


class ConfigSaveResult(TypedDict):
    config: AnibridgeConfig
    mtime: int | None
    requires_restart: bool


class ConfigurationService:
    """Persist YAML configuration documents with validation."""

    def __init__(self, config_path: Path | None = None) -> None:
        """Create a service bound to the active configuration file."""
        self._config_path = (config_path or find_yaml_config_file()).resolve()
        self._lock = asyncio.Lock()

    @property
    def config_path(self) -> Path:
        """Return the resolved configuration path."""
        return self._config_path

    def read(self) -> ConfigDocumentPayload:
        """Return raw YAML, file metadata, and a best-effort structured view."""
        file_exists = self._config_path.exists()
        content = self._config_path.read_text(encoding="utf-8") if file_exists else ""
        settings, settings_error = self._read_settings(content)
        return {
            "config_path": str(self._config_path),
            "file_exists": file_exists,
            "content": content,
            "mtime": self._mtime_ms(),
            "settings": settings,
            "settings_error": settings_error,
        }

    async def save_text(
        self, content: str, *, expected_mtime: int | None = None
    ) -> ConfigSaveResult:
        """Validate and persist a complete YAML document."""
        async with self._lock:
            self._check_mtime(expected_mtime)
            payload = self._parse_yaml(content)
            config = self._validate_config(payload)
            requires_restart = self._requires_restart(config)
            self._write(content if content.endswith("\n") else f"{content}\n", config)
            if not requires_restart:
                await self._apply_runtime_config(config)
            return {
                "config": config,
                "mtime": self._mtime_ms(),
                "requires_restart": requires_restart,
            }

    async def save_settings(
        self,
        settings: Mapping[str, object],
        *,
        expected_mtime: int | None = None,
    ) -> ConfigSaveResult:
        """Render, validate, and persist a structured configuration payload."""
        payload = {str(key): value for key, value in settings.items()}
        content = self._render_yaml(payload)
        async with self._lock:
            self._check_mtime(expected_mtime)
            config = self._validate_config(payload)
            requires_restart = self._requires_restart(config)
            self._write(content, config)
            if not requires_restart:
                await self._apply_runtime_config(config)
            return {
                "config": config,
                "mtime": self._mtime_ms(),
                "requires_restart": requires_restart,
            }

    def _mtime_ms(self) -> int | None:
        try:
            return int(self._config_path.stat().st_mtime * 1000)
        except FileNotFoundError:
            return None

    def _check_mtime(self, expected_mtime: int | None) -> None:
        if expected_mtime is None:
            return
        current_mtime = self._mtime_ms()
        if current_mtime is not None and current_mtime != expected_mtime:
            raise FileExistsError(
                "Configuration file modified on disk; reload to continue."
            )

    def _read_settings(
        self, content: str
    ) -> tuple[dict[str, object] | None, str | None]:
        if not content.strip():
            return {}, None
        try:
            return dict(self._parse_yaml(content)), None
        except ValueError as exc:
            return None, str(exc)

    def _parse_yaml(self, content: str) -> Mapping[str, object]:
        try:
            parsed = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML syntax: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise ValueError("Configuration file must contain a mapping at the root")
        return {str(key): value for key, value in parsed.items()}

    def _validate_config(self, payload: Mapping[str, object]) -> AnibridgeConfig:
        try:
            return AnibridgeConfig.model_validate(dict(payload))
        except Exception as exc:
            raise ValueError(f"Unable to parse configuration: {exc}") from exc

    def _render_yaml(self, payload: Mapping[str, object]) -> str:
        content = yaml.dump(
            dict(payload),
            Dumper=_ConfigDumper,
            sort_keys=False,
            allow_unicode=False,
            default_flow_style=False,
        )
        return content if content.endswith("\n") else f"{content}\n"

    def _write(self, content: str, config: AnibridgeConfig) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(content, encoding="utf-8")
        log.info(
            "Configuration saved with %s profile(s) at %s",
            len(config.profiles),
            self._config_path,
        )

    def _requires_restart(self, config: AnibridgeConfig) -> bool:
        current_config = self._runtime_config()
        if current_config is None:
            return True

        for field in (
            "log_level",
            "mappings_url",
            "provider_classes",
            "threads",
            "web",
        ):
            if getattr(current_config, field) != getattr(config, field):
                return True
        return False

    def _runtime_config(self) -> AnibridgeConfig | None:
        scheduler = get_app_state().scheduler
        if scheduler is not None:
            return scheduler.global_config
        try:
            return get_config()
        except Exception:
            return None

    async def _apply_runtime_config(self, config: AnibridgeConfig) -> None:
        scheduler = get_app_state().scheduler
        if scheduler is None:
            get_config.cache_clear()
            return

        previous_config = scheduler.global_config
        removed_profiles = set(previous_config.profiles) - set(config.profiles)
        changed_profiles = {
            profile_name
            for profile_name, profile_config in config.profiles.items()
            if previous_config.profiles.get(profile_name) is None
            or previous_config.profiles[profile_name].model_dump(mode="python")
            != profile_config.model_dump(mode="python")
        }

        scheduler.global_config = config
        get_config.cache_clear()
        for profile_name in removed_profiles:
            await scheduler.remove_profile(profile_name)
        for profile_name in changed_profiles:
            await scheduler.reinitialize_profile(profile_name)
        get_app_state().notify_status_change()


@cache
def get_configuration_service() -> ConfigurationService:
    """Get the singleton ConfigurationService instance."""
    return ConfigurationService()
