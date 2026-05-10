"""AniBridge Configuration Settings."""

import os
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import yaml
from anibridge.utils.cache import cache, lru_cache
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic.json_schema import (
    DEFAULT_REF_TEMPLATE,
    GenerateJsonSchema,
    JsonSchemaMode,
)
from pydantic_core import PydanticUndefined
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from anibridge.app.config.sync_rules import (
    BaseStrEnum,
    SyncRuleDefinition,
    SyncRulesConfig,
    SyncRuleTemplateId,
)
from anibridge.app.exceptions import ProfileConfigError, ProfileNotFoundError
from anibridge.app.logging import get_logger
from anibridge.app.utils.cron import CronStr

__all__ = [
    "AnibridgeConfig",
    "AnibridgeProfileConfig",
    "LogLevel",
    "ScanMode",
    "SyncField",
    "SyncRuleDefinition",
    "SyncRuleTemplateId",
    "SyncRulesConfig",
    "get_config",
]

log = get_logger(__name__)

_SCHEMA_EXTRA_BEHAVIOR_KEY = "x-anibridge-extraBehavior"


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
    """Enumeration of list fields that can be synchronized with the list provider."""

    STATUS = "status"  # Watch status (watching, completed, etc.)
    PROGRESS = "progress"  # Number of episodes/movies watched
    REPEATS = "repeats"  # Number of times rewatched
    REVIEW = "review"  # User's review/comments (text)
    USER_RATING = "user_rating"  # User's rating/score
    STARTED_AT = "started_at"  # When the user started watching (date)
    FINISHED_AT = "finished_at"  # When the user finished watching (date)

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """Get a tuple of all sync field names.

        Returns:
            tuple[str, ...]: All sync field names as strings.
        """
        return tuple(field.value for field in cls)


class ScanMode(BaseStrEnum):
    """Synchronization execution modes.

    Multiple modes can be enabled simultaneously by specifying a list.

    periodic: Periodic scans every `scan_interval` seconds
    poll: Poll for incremental changes every `poll_interval` seconds
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
    host: str = Field(default="", description="Host for the web server")
    port: int = Field(default=4848, description="Port for the web server")
    path_prefix: str = Field(
        default="",
        description="Web serverpath prefix that AniBridge should be served from",
    )
    allow_config_without_auth: bool = Field(
        default=False,
        description=(
            "Expose the configuration API (read and write) without requiring "
            "authentication. Enable only if access is restricted by other means (e.g., "
            "reverse proxy firewall). Ignored if authentication is configured via "
            " basic_auth."
        ),
    )
    basic_auth: BasicAuthConfig = Field(
        default_factory=BasicAuthConfig, description="Authentication settings"
    )

    @field_validator("path_prefix", mode="before")
    @classmethod
    def normalize_path_prefix(cls, value: object) -> object:
        """Normalize optional path prefixes to a root-relative form."""
        if value is None:
            return ""
        if not isinstance(value, str):
            return value

        normalized = value.strip()
        if not normalized or normalized == "/":
            return ""
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized.rstrip("/")

    @property
    def has_auth(self) -> bool:
        """Whether web authentication is configured.

        Returns:
            bool: True if authentication is configured, False otherwise.
        """
        return bool(
            (
                self.basic_auth.username
                and self.basic_auth.password is not None
                and self.basic_auth.password.get_secret_value()
            )
            or self.basic_auth.htpasswd_path
        )


class AnibridgeProfileConfig(BaseModel):
    """Configuration for a single AniBridge profile."""

    library_provider: str = Field(
        default="",
        description="Namespace of the library provider to use",
    )
    list_provider: str = Field(
        default="",
        description="Namespace of the list provider to use",
    )
    scan_modes: list[ScanMode] = Field(
        default_factory=lambda: [ScanMode.PERIODIC, ScanMode.POLL, ScanMode.WEBHOOK],
        description="List of enabled scan modes (periodic, poll, webhook)",
    )
    scan_interval: int | CronStr = Field(
        default=86400,
        description="Scan interval in seconds or as a cron expression",
    )
    poll_interval: int | CronStr = Field(
        default=60,
        description="Poll scan interval in seconds or as a cron expression",
    )
    full_scan: bool = Field(
        default=False, description="Perform full library scans, even on unwatched items"
    )
    destructive_sync: bool = Field(
        default=False,
        description="Allow decreasing watch progress and removing list entries",
    )
    empty_sync: bool = Field(
        default=False,
        description=(
            "When enabled, entries with no watch activity/history are synced as "
            "planning instead of being skipped"
        ),
    )
    sync_rules: SyncRulesConfig = Field(
        default_factory=SyncRulesConfig,
        description=(
            "Declarative rule-based sync overrides. Each field can be disabled "
            "or configured with an ordered list of conditions that decide how "
            "the computed value should be transformed or whether it should sync."
        ),
    )
    backup_retention_days: int = Field(
        default=30,
        ge=-1,
        description=(
            "Days to retain list backups before cleanup (0 retains indefinitely; "
            "-1 disables backup creation)"
        ),
    )
    batch_requests: bool = Field(
        default=False, description="Batch API requests for better performance"
    )
    search_fallback_threshold: int = Field(
        default=-1, ge=-1, le=100, description="Fuzzy search threshold"
    )
    dry_run: bool = Field(
        default=False, description="Log changes without applying them"
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

    _parent: AnibridgeConfig | None = None

    @property
    def parent(self) -> AnibridgeConfig:
        """Get the parent multi-config instance.

        Returns:
            AnibridgeConfig: Parent configuration.

        Raises:
            ProfileConfigError: If this config is not part of a multi-config.
        """
        if not self._parent:
            raise ProfileConfigError(
                "This configuration is not part of a multi-config instance"
            )
        return self._parent

    def _merge_globals(self) -> AnibridgeProfileConfig:
        """Merge global settings from the parent config into this profile config."""
        if not self._parent:
            return self

        for field in self.__class__.model_fields:
            if field in ("library_provider_config", "list_provider_config"):
                # Special handling to merge provider configs one level deep.
                global_providers = getattr(self._parent.global_config, field)
                profile_providers = getattr(self, field)
                merged_providers = {**global_providers}
                for provider_namespace, provider_settings in profile_providers.items():
                    global_settings = global_providers.get(provider_namespace)
                    if isinstance(global_settings, dict) and isinstance(
                        provider_settings, dict
                    ):
                        merged_providers[provider_namespace] = {
                            **global_settings,
                            **provider_settings,
                        }
                    else:
                        merged_providers[provider_namespace] = provider_settings
                setattr(self, field, merged_providers)
            elif field in self.model_fields_set:  # Field set on profile level
                continue
            else:  # Inherit from global if not set
                if field not in self._parent.global_config.model_fields_set:
                    continue
                global_value = getattr(self._parent.global_config, field)
                setattr(self, field, global_value)
        return self


def _iter_model_types(annotation: Any) -> set[type[BaseModel]]:
    model_types: set[type[BaseModel]] = set()
    origin = get_origin(annotation)

    if origin is None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            model_types.add(annotation)
        return model_types

    for arg in get_args(annotation):
        model_types.update(_iter_model_types(arg))

    return model_types


def _collect_nested_model_types(root_model: type[BaseModel]) -> set[type[BaseModel]]:
    discovered: set[type[BaseModel]] = set()
    pending = [root_model]

    while pending:
        model_cls = pending.pop()
        if model_cls in discovered:
            continue

        discovered.add(model_cls)
        for field in model_cls.model_fields.values():
            pending.extend(_iter_model_types(field.annotation))

    return discovered


def _resolve_extra_behavior(model_cls: type[BaseModel]) -> str:
    extra_behavior = model_cls.model_config.get("extra")
    if extra_behavior in {"allow", "ignore", "forbid"}:
        return extra_behavior
    return "ignore"


class AnibridgeConfig(BaseSettings):
    """Multi-configuration manager for AniBridge application.

    Configuration is sourced from a YAML file (optionally combined with
    parameters passed directly to the model). Global settings are shared across
    all profiles, while profile-specific settings override those defaults.
    """

    global_config: AnibridgeProfileConfig = Field(
        default_factory=AnibridgeProfileConfig,
        description="Global configuration settings",
    )
    profiles: dict[str, AnibridgeProfileConfig] = Field(
        default_factory=dict, description="AniBridge profile configurations"
    )
    log_level: LogLevel = Field(
        default=LogLevel.INFO, description="Logging level for the application"
    )
    mappings_url: str | None = Field(
        default="https://github.com/anibridge/anibridge-mappings/releases/download/v3/mappings.json.zst",
        description=(
            "URL to JSON or YAML file to use as the upstream mappings source. "
            "Additionally accepts Zstandard compressed (.zst) files. "
            "If not set, no upstream mappings will be used."
        ),
    )
    provider_classes: list[str] = Field(
        default_factory=list,
        description="Additional class paths to register provider implementations from",
    )
    threads: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Number of threads to use for parallel operations. "
            "Defaults to len(profiles) + 1 when not set."
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
    def validate_global_config(self) -> AnibridgeConfig:
        """Validates global configuration settings.

        Returns:
            AnibridgeConfig: Self with validated settings.

        Raises:
            ValueError: If required global settings are missing or invalid.
        """
        if (
            not self.model_fields_set
            and not self.profiles
            and not self.global_config.model_fields_set
            and not self.web.has_auth
        ):
            self.web.allow_config_without_auth = True

        # If there are no explicit profiles, attempt to bootstrap a default from globals
        if not self.profiles and self.global_config.model_fields_set:
            log.info(
                "No profiles configured; creating implicit 'default' profile from "
                "globals"
            )
            self.profiles["default"] = self.global_config.model_copy()

        # Merge global settings into each profile
        for profile in self.profiles.values():
            profile._parent = self
            profile._merge_globals()

        # Default thread count to len(profiles) + 1 when not explicitly set
        if self.threads is None:
            self.threads = len(self.profiles) + 1

        if (not self.web.basic_auth.username) != (not self.web.basic_auth.password):
            log.warning(
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

    def get_profile(self, name: str) -> AnibridgeProfileConfig:
        """Get a specific profile configuration.

        Args:
            name: Profile name

        Returns:
            AnibridgeProfileConfig: The profile configuration.

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
            EnvSettingsSource(
                settings_cls,
                env_prefix="AB_",
                env_nested_delimiter="__",
                env_parse_none_str="null",
            ),
        )

    @classmethod
    @lru_cache(maxsize=1, per_instance=False)
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = DEFAULT_REF_TEMPLATE,
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        """Generate the config schema with editor-specific extra-behavior metadata."""
        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )
        definitions = schema.get("$defs", {})

        for nested_model in _collect_nested_model_types(cls):
            target = (
                schema
                if nested_model is cls
                else definitions.get(nested_model.__name__)
            )
            if not isinstance(target, dict) or target.get("type") != "object":
                continue

            target[_SCHEMA_EXTRA_BEHAVIOR_KEY] = _resolve_extra_behavior(nested_model)

        return schema

    model_config = SettingsConfigDict(extra="ignore")


class _ConfigDumper(yaml.SafeDumper):
    """Custom YAML dumper to control the formatting of the configuration file."""

    _in_key = False

    def represent_mapping(self, tag, mapping, flow_style=None):
        node = yaml.nodes.MappingNode(tag, [], flow_style=flow_style)
        for k, v in mapping.items():
            self._in_key = True
            k_node = self.represent_data(k)
            self._in_key = False
            v_node = self.represent_data(v)
            node.value.append((k_node, v_node))
        return node


def _repr_flow_seq(
    dumper: yaml.SafeDumper, data: list | tuple
) -> yaml.nodes.SequenceNode:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


def _repr_str(dumper: _ConfigDumper, data: str) -> yaml.nodes.ScalarNode:
    style = None if dumper._in_key else '"'
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _repr_enum(dumper: yaml.SafeDumper, data: BaseStrEnum) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data.value), style=None)


_ConfigDumper.add_representer(list, _repr_flow_seq)
_ConfigDumper.add_representer(tuple, _repr_flow_seq)
_ConfigDumper.add_representer(str, _repr_str)
_ConfigDumper.add_multi_representer(BaseStrEnum, _repr_enum)


def find_yaml_config_file() -> Path:
    """Find the YAML configuration file in the data path.

    Returns:
        Path: The path to an existing YAML configuration file or the default location.
    """
    data_path = Path(os.getenv("AB_DATA_PATH", "./data")).resolve()

    for ext in ("yaml", "yml"):
        yaml_file = data_path / f"config.{ext}"
        if yaml_file.exists():
            log.debug("Using YAML config file: %s", yaml_file.resolve())
            return yaml_file.resolve()

    return data_path / "config.yaml"


def _build_config_template_model(model_cls: type[BaseModel]) -> BaseModel:
    """Recursively build a model with default values for required fields."""
    payload: dict[str, object] = {}
    for name, field in model_cls.model_fields.items():
        if field.default is not PydanticUndefined:
            value = field.default
        elif field.default_factory is not None:
            value = field.default_factory()
        else:
            value = "..."
        payload[name] = value
    return model_cls.model_construct(_fields_set=set(payload), **payload)


def _render_default_config_template() -> str:
    template_model = _build_config_template_model(AnibridgeConfig)

    yaml_text = yaml.dump(
        template_model.model_dump(mode="python"),
        Dumper=_ConfigDumper,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    ).rstrip()

    commented_lines = [
        "############################################################################################################",
        "# This is a template configuration file for AniBridge.                                                     #",  # noqa: E501
        "#                                                                                                          #",  # noqa: E501
        "# Please refer to the documentation for more details: https://anibridge.eliasbenb.dev/configuration/       #",  # noqa: E501
        "############################################################################################################",
        "",
    ]

    commented_lines.extend(
        "#" if not line.strip() else f"# {line}" for line in yaml_text.splitlines()
    )

    return "\n".join(commented_lines) + "\n"


def _ensure_default_config_file() -> Path:
    config_path = find_yaml_config_file()

    if config_path.exists():
        return config_path

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_render_default_config_template(), encoding="utf-8")
    log.info(
        "AnibridgeConfig: Created default configuration template at %s", config_path
    )
    return config_path


@cache
def get_config() -> AnibridgeConfig:
    """Get the singleton instance of AnibridgeConfig.

    Returns:
        AnibridgeConfig: The singleton configuration instance.
    """
    _ensure_default_config_file()
    return AnibridgeConfig()
