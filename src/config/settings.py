"""AniBridge Configuration Settings."""

import os
from enum import StrEnum
from functools import cached_property, lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from src.exceptions import ProfileConfigError, ProfileNotFoundError
from src.utils.logging import _get_logger

__all__ = [
    "AniBridgeConfig",
    "AniBridgeProfileConfig",
    "LogLevel",
    "ScanMode",
    "SyncField",
    "get_config",
]

_log = _get_logger(__name__)


def find_yaml_config_file() -> Path:
    """Find the YAML configuration file in the data path.

    Returns:
        Path: The path to an existing YAML configuration file or the default location.
    """
    data_path = Path(os.getenv("AB_DATA_PATH", "./data")).resolve()

    for ext in ("yaml", "yml"):
        yaml_file = data_path / f"config.{ext}"
        if yaml_file.exists():
            _log.debug(f"Using YAML config file: {yaml_file.resolve()}")
            return yaml_file.resolve()
    return data_path / "config.yaml"


class BaseStrEnum(StrEnum):
    """Base class for string-based enumerations with a custom __repr__ method.

    Provides case-insensitive lookup functionality and consistent string
    representation for enumeration values.
    """

    @classmethod
    def _missing_(cls, value: object) -> BaseStrEnum | None:
        """Handle case-insensitive lookup for enum values.

        Args:
            value: The value to look up in the enumeration

        Returns:
            BaseStrEnum | None: The matching enum member if found, None otherwise
        """
        value = value.lower() if isinstance(value, str) else value
        for member in cls:
            if member.lower() == value:
                return member
        return None

    def __repr__(self) -> str:
        """Return the string value of the enum member."""
        return self.value

    def __str__(self) -> str:
        """Return the string representation of the enum member."""
        return repr(self)


class LogLevel(BaseStrEnum):
    """Enumeration of available logging levels.

    Standard Python logging levels used to control log output verbosity.
    Ordered from most verbose (DEBUG) to least verbose (CRITICAL).

    Note: SUCCESS is a custom level used by this application.
    """

    DEBUG = "DEBUG"  # Detailed information for debugging
    INFO = "INFO"  # General information about program execution
    SUCCESS = "SUCCESS"  # Successful operations (custom level)
    WARNING = "WARNING"  # Potential problems or issues
    ERROR = "ERROR"  # Error that prevented an operation
    CRITICAL = "CRITICAL"  # Error that prevents further program execution


class SyncField(BaseStrEnum):
    """Enumeration of AniList fields that can be synchronized with Plex.

    These fields represent the data that can be synchronized between Plex
    and AniList for each media entry. Each enum value corresponds to an
    AniList API field name in snake_case format.
    """

    STATUS = "status"  # Watch status (watching, completed, etc.)
    PROGRESS = "progress"  # Number of episodes/movies watched
    REPEATS = "repeats"  # Number of times rewatched
    REVIEW = "review"  # User's review/comments (text)
    USER_RATING = "user_rating"  # User's rating/score
    STARTED_AT = "started_at"  # When the user started watching (date)
    FINISHED_AT = "finished_at"  # When the user finished watching (date)


class ScanMode(BaseStrEnum):
    """Synchronization execution modes.

    Multiple modes can be enabled simultaneously by specifying a list.

    periodic: Periodic scans every `scan_interval` seconds
    poll: Poll for incremental changes every 30 seconds
    webhook: External webhook-triggered syncs, dependent on `ab_web_enabled`
    """

    PERIODIC = "periodic"
    POLL = "poll"
    WEBHOOK = "webhook"


class BasicAuthConfig(BaseModel):
    """Configuration for authentication settings."""

    username: str | None = Field(
        default=None, description="Username for authentication"
    )
    password: SecretStr | None = Field(
        default=None, description="Password for authentication"
    )
    htpasswd_path: Path | None = Field(
        default=None, description="Path to an htpasswd file for authentication"
    )
    realm: str = Field(
        default="AniBridge", description="Realm for HTTP Basic Authentication"
    )


class WebConfig(BaseModel):
    """Configuration for the embedded web server."""

    enabled: bool = Field(default=True, description="Enable the AniBridge web server")
    host: str = Field(default="0.0.0.0", description="Host for the web server")
    port: int = Field(default=4848, description="Port for the web server")
    basic_auth: BasicAuthConfig = Field(
        default_factory=BasicAuthConfig, description="Authentication settings"
    )


class AniBridgeProfileConfig(BaseModel):
    """Configuration for a single AniBridge profile."""

    library_provider: str = Field(
        default="",
        description="Namespace of the library provider to use",
    )
    list_provider: str = Field(
        default="",
        description="Namespace of the list provider to use",
    )

    library_provider_config: dict[str, dict] = Field(
        default_factory=dict,
        exclude=True,
        repr=False,
        description="Library provider configuration by namespace",
    )
    list_provider_config: dict[str, dict] = Field(
        default_factory=dict,
        exclude=True,
        repr=False,
        description="List provider configuration by namespace",
    )

    scan_interval: int = Field(
        default=86400, ge=0, description="Scan interval in seconds"
    )
    scan_modes: list[ScanMode] = Field(
        default_factory=lambda: [ScanMode.PERIODIC, ScanMode.POLL, ScanMode.WEBHOOK],
        description="List of enabled scan modes (periodic, poll, webhook)",
    )
    full_scan: bool = Field(
        default=False, description="Perform full library scans, even on unwatched items"
    )
    destructive_sync: bool = Field(
        default=False,
        description="Allow decreasing watch progress and removing list entries",
    )
    excluded_sync_fields: list[SyncField] = Field(
        default_factory=lambda: [SyncField.REVIEW, SyncField.USER_RATING],
        description="Fields to exclude from synchronization",
    )
    dry_run: bool = Field(
        default=False, description="Log changes without applying them"
    )
    batch_requests: bool = Field(
        default=False, description="Batch API requests for better performance"
    )
    search_fallback_threshold: int = Field(
        default=-1, ge=-1, le=100, description="Fuzzy search threshold"
    )
    backup_retention_days: int = Field(
        default=30,
        ge=0,
        description=("Days to retain list backups before cleanup (0 disables cleanup)"),
    )

    _parent: AniBridgeConfig | None = None

    @property
    def parent(self) -> AniBridgeConfig:
        """Get the parent multi-config instance.

        Returns:
            AniBridgeConfig: Parent configuration.

        Raises:
            ProfileConfigError: If this config is not part of a multi-config.
        """
        if not self._parent:
            raise ProfileConfigError(
                "This configuration is not part of a multi-config instance"
            )
        return self._parent

    def _merge_globals(self) -> AniBridgeProfileConfig:
        """Merge global settings from the parent config into this profile config."""
        if not self._parent:
            return self

        for field in self.__class__.model_fields:
            if field in ("library_provider_config", "list_provider_config"):
                # Special handling to do 1-level dict merge for provider configs
                global_providers = getattr(self._parent.global_config, field)
                profile_providers = getattr(self, field)
                setattr(self, field, {**global_providers, **profile_providers})
            elif field in self.model_fields_set:  # Field set on profile level
                continue
            else:  # Inherit from global if not set
                if field not in self._parent.global_config.model_fields_set:
                    continue
                global_value = getattr(self._parent.global_config, field)
                setattr(self, field, global_value)
        return self


class AniBridgeConfig(BaseSettings):
    """Multi-configuration manager for AniBridge application.

    Configuration is sourced from a YAML file (optionally combined with
    parameters passed directly to the model). Global settings are shared across
    all profiles, while profile-specific settings override those defaults.
    """

    global_config: AniBridgeProfileConfig = Field(
        default_factory=AniBridgeProfileConfig,
        description="Global configuration settings",
    )
    profiles: dict[str, AniBridgeProfileConfig] = Field(
        default_factory=dict, description="AniBridge profile configurations"
    )
    provider_modules: list[str] = Field(
        default_factory=list,
        description="Additional module paths to load provider implementations from",
    )
    log_level: LogLevel = Field(
        default=LogLevel.INFO, description="Logging level for the application"
    )
    mappings_url: str | None = Field(
        default="https://github.com/anibridge/anibridge-mappings/releases/latest/download/mappings.json.zst",
        description=(
            "URL to JSON or YAML file to use as the upstream mappings source. "
            "Additionally accepts Zstandard compressed (.zst) files. "
            "If not set, no upstream mappings will be used."
        ),
    )
    web: WebConfig = Field(
        default_factory=WebConfig, description="Embedded web server configuration"
    )

    @cached_property
    def data_path(self) -> Path:
        """Get the data path for AniBridge.

        Returns:
            Path: The data path resolved from the environment or default location.
        """
        return Path(os.getenv("AB_DATA_PATH", "./data")).resolve()

    @model_validator(mode="after")
    def validate_global_config(self) -> AniBridgeConfig:
        """Validates global configuration settings.

        Returns:
            AniBridgeConfig: Self with validated settings.

        Raises:
            ValueError: If required global settings are missing or invalid.
        """
        # If there are no explicit profiles, attempt to bootstrap a default from globals
        if not self.profiles and self.global_config.model_fields_set:
            _log.info(
                "No profiles configured; creating implicit 'default' profile from "
                "globals"
            )
            self.profiles["default"] = self.global_config.model_copy()

        # Merge global settings into each profile
        for profile in self.profiles.values():
            profile._parent = self
            profile._merge_globals()

        if (not self.web.basic_auth.username) != (not self.web.basic_auth.password):
            _log.warning(
                "Both web.basic_auth.username and web.basic_auth.password must be set "
                "to enable static HTTP Basic Authentication credentials; ignoring "
                "partial values"
            )
            self.web.basic_auth.username = None
            self.web.basic_auth.password = None

        if (
            self.web.basic_auth.htpasswd_path
            and not self.web.basic_auth.htpasswd_path.is_file()
        ):
            raise ValueError(
                "web.basic_auth.htpasswd_path must point to an existing file"
            )

        return self

    def get_profile(self, name: str) -> AniBridgeProfileConfig:
        """Get a specific profile configuration.

        Args:
            name: Profile name

        Returns:
            AniBridgeProfileConfig: The profile configuration.

        Raises:
            ProfileNotFoundError: If profile doesn't exist.
        """
        if name not in self.profiles:
            raise ProfileNotFoundError(
                f"Profile '{name}' not found. Available profiles: "
                f"{list(self.profiles.keys())}"
            )
        return self.profiles[name]

    def __str__(self) -> str:
        """Creates a human-readable representation of the configuration.

        Returns:
            str: Configuration summary with profile count and global settings.
        """
        profile_count = len(self.profiles)
        profile_names = ", ".join(self.profiles.keys())

        return (
            f"AniBridge Config: {profile_count} profile(s) [{profile_names}], "
            f"DATA_PATH: {self.data_path}, LOG_LEVEL: {self.log_level}"
        )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Customize the order of configuration sources."""
        return (
            init_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=find_yaml_config_file()),
        )

    model_config = SettingsConfigDict(extra="ignore")


@lru_cache(maxsize=1)
def get_config() -> AniBridgeConfig:
    """Get the singleton instance of AniBridgeConfig.

    Returns:
        AniBridgeConfig: The singleton configuration instance.
    """
    return AniBridgeConfig()
