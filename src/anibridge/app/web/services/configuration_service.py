"""Utilities for reading and writing AniBridge configuration documents."""

import asyncio
from collections.abc import Mapping
from operator import attrgetter
from pathlib import Path
from typing import TypedDict

import yaml
from anibridge.utils.cache import cache
from pydantic import BaseModel, SecretStr

from anibridge.app.config.settings import (
    AnibridgeConfig,
    _ConfigDumper,
    find_yaml_config_file,
    get_config,
)
from anibridge.app.exceptions import SchedulerUnavailableError
from anibridge.app.logging import get_logger
from anibridge.app.web.state import get_app_state

__all__ = ["ConfigurationService", "get_configuration_service"]

log = get_logger(__name__)

_RESTART_REQUIRED_FIELDS: tuple[str, ...] = (
    "log_level",
    "provider_classes",
    "threads",
    "web.enabled",
    "web.host",
    "web.path_prefix",
    "web.port",
    "web.basic_auth",
)


class ConfigDocumentPayload(TypedDict):
    config_path: str
    file_exists: bool
    content: str
    mtime: int | None
    settings: dict[str, object] | None
    settings_error: str | None


def _normalize_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return {
            field_name: _normalize_value(getattr(value, field_name))
            for field_name in value.__class__.model_fields
        }
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_value(item) for item in value]
    return value


class ConfigurationService:
    """Manage persistence and validation of the AniBridge YAML configuration."""

    def __init__(self, config_path: Path | None = None) -> None:
        """Create a service bound to the provided configuration path."""
        self._config_path = (config_path or find_yaml_config_file()).resolve()
        self._lock = asyncio.Lock()

    @property
    def config_path(self) -> Path:
        """Return the resolved configuration path."""
        return self._config_path

    def _get_mtime_ms(self) -> int | None:
        """Return the modification time of the configuration file in milliseconds."""
        try:
            return int(self._config_path.stat().st_mtime * 1000)
        except FileNotFoundError:
            return None

    def _parse_yaml(self, content: str) -> Mapping[str, object]:
        """Parse YAML content into a mapping structure."""
        try:
            parsed = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML syntax: {exc}") from exc

        if not isinstance(parsed, Mapping):
            raise ValueError("Configuration file must contain a mapping at the root")

        return {str(key): value for key, value in parsed.items()}

    def _build_config_instance(self, payload: Mapping[str, object]) -> AnibridgeConfig:
        """Build and validate an AnibridgeConfig instance from the provided payload."""
        try:
            return AnibridgeConfig.model_validate(dict(payload))
        except Exception as exc:
            raise ValueError(f"Unable to parse configuration: {exc}") from exc

    def _build_settings_snapshot(
        self, content: str
    ) -> tuple[dict[str, object] | None, str | None]:
        """Parse the config YAML with error logging."""
        if not content.strip():
            return {}, None
        try:
            payload = self._parse_yaml(content)
            return dict(payload), None
        except ValueError as exc:
            return None, str(exc)

    def _render_settings_payload(self, settings: Mapping[str, object]) -> str:
        """Render structured settings back into YAML format."""
        content = yaml.dump(
            dict(settings),
            Dumper=_ConfigDumper,
            sort_keys=False,
            allow_unicode=False,
            default_flow_style=False,
        )
        return content if content.endswith("\n") else f"{content}\n"

    def load_document_text(self) -> ConfigDocumentPayload:
        """Return the raw YAML content alongside file metadata."""
        file_exists = self._config_path.exists()
        content = (
            "" if not file_exists else self._config_path.read_text(encoding="utf-8")
        )
        settings, settings_error = self._build_settings_snapshot(content)
        return {
            "config_path": str(self._config_path),
            "file_exists": file_exists,
            "content": content,
            "mtime": self._get_mtime_ms(),
            "settings": settings,
            "settings_error": settings_error,
        }

    async def _apply_runtime_config(self, next_config: AnibridgeConfig) -> bool:
        runtime_config = get_config()
        scheduler = get_app_state().scheduler

        requires_restart = any(
            _normalize_value(attrgetter(field_path)(runtime_config))
            != _normalize_value(attrgetter(field_path)(next_config))
            for field_path in _RESTART_REQUIRED_FIELDS
        )
        current_profiles = {
            profile_name: _normalize_value(profile)
            for profile_name, profile in runtime_config.profiles.items()
        }
        next_profiles = {
            profile_name: _normalize_value(profile)
            for profile_name, profile in next_config.profiles.items()
        }

        removed_profiles = sorted(set(current_profiles) - set(next_profiles))
        changed_profiles = sorted(
            profile_name
            for profile_name, profile_snapshot in next_profiles.items()
            if current_profiles.get(profile_name) != profile_snapshot
        )

        mappings_url_changed = runtime_config.mappings_url != next_config.mappings_url
        global_defaults_changed = _normalize_value(
            runtime_config.global_config
        ) != _normalize_value(next_config.global_config)

        if global_defaults_changed:
            runtime_config.global_config = next_config.global_config.model_copy(
                deep=True
            )

        runtime_config.mappings_url = next_config.mappings_url
        runtime_config.web.allow_config_without_auth = (
            next_config.web.allow_config_without_auth
        )

        for profile_name in removed_profiles:
            runtime_config.profiles.pop(profile_name, None)

        for profile_name in changed_profiles:
            profile = next_config.get_profile(profile_name).model_copy(deep=True)
            profile._parent = runtime_config
            runtime_config.profiles[profile_name] = profile

        if scheduler is None:
            return requires_restart

        for profile_name in removed_profiles:
            await scheduler.remove_profile(profile_name)

        for profile_name in changed_profiles:
            await scheduler.reinitialize_profile(profile_name)

        if mappings_url_changed:
            scheduler.shared_animap_client.upstream_url = next_config.mappings_url
            scheduler.shared_animap_client.mappings_client.upstream_url = (
                next_config.mappings_url
            )
            try:
                await scheduler.trigger_database_sync(source="api:config:mappings_url")
            except TimeoutError as exc:
                raise SchedulerUnavailableError(
                    "Timed out while refreshing mappings after config update"
                ) from exc

        return requires_restart

    async def save_document_text(
        self, content: str, *, expected_mtime: int | None = None
    ) -> tuple[AnibridgeConfig, bool, int | None]:
        """Persist YAML text after validation and return the updated config."""
        async with self._lock:
            if expected_mtime is not None:
                current_mtime = self._get_mtime_ms()
                if current_mtime is not None and current_mtime != expected_mtime:
                    raise FileExistsError(
                        "Configuration file modified on disk; reload to continue."
                    )

            payload = self._parse_yaml(content)
            config = self._build_config_instance(payload)

            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            normalized_content = content if content.endswith("\n") else f"{content}\n"
            self._config_path.write_text(normalized_content, encoding="utf-8")
            log.info(
                f"Configuration updated with {len(config.profiles)} profile(s) at "
                f"{self._config_path}",
            )

            requires_restart = await self._apply_runtime_config(config)
            return config, requires_restart, self._get_mtime_ms()

    async def save_settings_payload(
        self,
        settings: Mapping[str, object],
        *,
        expected_mtime: int | None = None,
    ) -> tuple[AnibridgeConfig, bool, int | None]:
        """Persist structured settings by rewriting the YAML document."""
        async with self._lock:
            if expected_mtime is not None:
                current_mtime = self._get_mtime_ms()
                if current_mtime is not None and current_mtime != expected_mtime:
                    raise FileExistsError(
                        "Configuration file modified on disk; reload to continue."
                    )

            payload = {str(key): value for key, value in settings.items()}
            config = self._build_config_instance(payload)
            rendered = self._render_settings_payload(payload)

            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(rendered, encoding="utf-8")
            log.info(
                f"Configuration updated with {len(config.profiles)} profile(s) at "
                f"{self._config_path}",
            )

            requires_restart = await self._apply_runtime_config(config)
            return config, requires_restart, self._get_mtime_ms()


@cache
def get_configuration_service() -> ConfigurationService:
    """Get the singleton ConfigurationService instance.

    Returns:
        ConfigurationService: The configuration service instance.
    """
    return ConfigurationService()
