"""Utilities for reading and writing AniBridge configuration documents."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from anibridge.utils.cache import cache

from anibridge.app import log
from anibridge.app.config.settings import AniBridgeConfig, find_yaml_config_file

__all__ = ["ConfigurationService", "get_configuration_service"]


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

    def _load_raw_text(self) -> str:
        """Return the raw YAML content of the configuration file."""
        if not self._config_path.exists():
            return ""
        return self._config_path.read_text(encoding="utf-8")

    def _get_mtime_ms(self) -> int | None:
        """Return the modification time of the configuration file in milliseconds."""
        try:
            return int(self._config_path.stat().st_mtime * 1000)
        except FileNotFoundError:
            return None

    def _parse_yaml(self, content: str) -> Mapping[str, Any]:
        """Parse YAML content into a mapping structure."""
        try:
            parsed = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML syntax: {exc}") from exc

        if not isinstance(parsed, Mapping):
            raise ValueError("Configuration file must contain a mapping at the root")

        return parsed

    def _build_config_instance(self, payload: Mapping[str, Any]) -> AniBridgeConfig:
        """Build and validate an AniBridgeConfig instance from the provided payload."""
        try:
            return AniBridgeConfig.model_validate(dict(payload))
        except Exception as exc:
            raise ValueError(f"Unable to parse configuration: {exc}") from exc

    def load_document_text(self) -> dict[str, Any]:
        """Return the raw YAML content alongside file metadata."""
        file_exists = self._config_path.exists()
        content = self._load_raw_text()
        return {
            "config_path": str(self._config_path),
            "file_exists": file_exists,
            "content": content,
            "mtime": self._get_mtime_ms(),
        }

    async def save_document_text(
        self, content: str, *, expected_mtime: int | None = None
    ) -> tuple[AniBridgeConfig, int | None]:
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

            return config, self._get_mtime_ms()


@cache
def get_configuration_service() -> ConfigurationService:
    """Get the singleton ConfigurationService instance.

    Returns:
        ConfigurationService: The configuration service instance.
    """
    return ConfigurationService()
