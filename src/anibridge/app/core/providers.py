"""Provider loader helpers."""

from collections.abc import Iterable
from importlib import import_module

from anibridge.provider.base import Provider, Role
from anibridge.utils.registry import ProviderRegistry

from anibridge.app.config.settings import (
    AnibridgeConfig,
    AnibridgeProfileConfig,
    ProviderNamespaceConfigMap,
)
from anibridge.app.exceptions import ProfileConfigError
from anibridge.app.logging import get_logger

__all__ = [
    "build_profile_providers",
    "build_provider",
]

log = get_logger(__name__)

_LOADED_CLASSES: set[str] = set()
_DEFAULT_PROVIDER_CLASSES: tuple[str, ...] = (
    "anibridge.providers.anilist.provider.AnilistProvider",
    "anibridge.providers.emby.provider.EmbyProvider",
    "anibridge.providers.jellyfin.provider.JellyfinProvider",
    "anibridge.providers.mal.provider.MalProvider",
    "anibridge.providers.plex.provider.PlexProvider",
    "anibridge.providers.simkl.provider.SimklProvider",
    "anibridge.providers.trakt.provider.TraktProvider",
)

provider_registry: ProviderRegistry[Provider] = ProviderRegistry()


def _register_classes(class_paths: Iterable[str]) -> None:
    """Import and register provider classes, ensuring each class loads once."""
    for class_path in class_paths:
        if not class_path or class_path in _LOADED_CLASSES:
            continue

        module_path, separator, class_name = class_path.rpartition(".")
        if not separator or not module_path or not class_name:
            raise ProfileConfigError(
                f"Invalid provider class path '{class_path}'. "
                "Expected a fully qualified class path like "
                "'package.module.ProviderClass'."
            )

        try:
            module = import_module(module_path)
            module_exports = vars(module)
            if class_name not in module_exports:
                raise AttributeError(class_name)
            provider_cls = module_exports[class_name]
        except Exception as exc:
            log.error("Failed to import provider class '%s'", class_path)
            log.exception("Provider class import error details")

            raise ProfileConfigError(
                f"Failed to import provider class '{class_path}'. "
                "Ensure the dependency is installed and the class path is valid."
            ) from exc

        if not isinstance(provider_cls, type):
            raise ProfileConfigError(
                f"Provider class path '{class_path}' does not resolve to a class."
            )

        try:
            if not issubclass(provider_cls, Provider):
                raise ProfileConfigError(
                    f"Provider class '{class_path}' must inherit from Provider."
                )
            provider_registry.register(provider_cls)
        except ValueError as exc:
            raise ProfileConfigError(
                f"Failed to register provider class '{class_path}': {exc}"
            ) from exc

        _LOADED_CLASSES.add(class_path)


def _collect_class_overrides(config: AnibridgeConfig | None) -> set[str]:
    """Return custom provider class paths from root configuration."""
    return set(config.provider_classes or ()) if config is not None else set()


def build_provider(
    namespace: str,
    config: ProviderNamespaceConfigMap,
    provider_classes: Iterable[str] = (),
) -> Provider:
    """Instantiate provider endpoint for a profile."""
    _register_classes(_DEFAULT_PROVIDER_CLASSES)
    _register_classes(set(provider_classes))

    if not namespace:
        raise ProfileConfigError("Provider namespace must be configured")

    provider_config = config.get(namespace, {})

    try:
        provider_cls = provider_registry.get(namespace)
    except LookupError:
        logger = log
    else:
        logger = get_logger(provider_cls.__module__)

    try:
        return provider_registry.create(
            namespace,
            logger=logger,
            config=provider_config,
        )
    except LookupError as exc:
        raise ProfileConfigError(
            f"No provider registered for namespace '{namespace or 'None'}'. "
            "Ensure the provider package is installed and listed under "
            "provider_classes."
        ) from exc


def build_profile_providers(
    profile: AnibridgeProfileConfig,
    config: AnibridgeConfig | None = None,
) -> dict[Role, Provider]:
    """Instantiate the configured source and target providers for a profile."""
    provider_classes = _collect_class_overrides(config)
    source_provider = build_provider(
        profile.source_provider, profile.source_provider_config, provider_classes
    )
    target_provider = build_provider(
        profile.target_provider, profile.target_provider_config, provider_classes
    )
    return {Role.SOURCE: source_provider, Role.TARGET: target_provider}
