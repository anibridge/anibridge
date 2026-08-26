"""Provider loader helpers."""

from collections.abc import Iterable
from importlib import import_module

from anibridge.library import LibraryProvider
from anibridge.list import ListProvider
from anibridge.utils.registry import ProviderRegistry

from anibridge.app.config.settings import AnibridgeConfig, AnibridgeProfileConfig
from anibridge.app.exceptions import ProfileConfigError
from anibridge.app.logging import get_logger

__all__ = [
    "build_library_provider",
    "build_list_provider",
]

log = get_logger(__name__)

_ROOT_LIBRARY_PACKAGE = "anibridge.providers.library"
_ROOT_LIST_PACKAGE = "anibridge.providers.list"

# Map provider namespaces to their default class paths for on-demand loading.
_DEFAULT_LIBRARY_CLASSES_BY_NS: dict[str, str] = {
    "emby": f"{_ROOT_LIBRARY_PACKAGE}.emby.EmbyLibraryProvider",
    "jellyfin": f"{_ROOT_LIBRARY_PACKAGE}.jellyfin.JellyfinLibraryProvider",
    "plex": f"{_ROOT_LIBRARY_PACKAGE}.plex.PlexLibraryProvider",
}
_DEFAULT_LIST_CLASSES_BY_NS: dict[str, str] = {
    "anilist": f"{_ROOT_LIST_PACKAGE}.anilist.AnilistListProvider",
    "mal": f"{_ROOT_LIST_PACKAGE}.mal.MalListProvider",
}
_LOADED_CLASSES: set[str] = set()

library_registry: ProviderRegistry[LibraryProvider] = ProviderRegistry()
list_registry: ProviderRegistry[ListProvider] = ProviderRegistry()


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
            provider_cls = getattr(module, class_name)
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
            if issubclass(provider_cls, LibraryProvider):
                library_registry.register(provider_cls)
            elif issubclass(provider_cls, ListProvider):
                list_registry.register(provider_cls)
            else:
                raise ProfileConfigError(
                    f"Provider class '{class_path}' must inherit from "
                    "LibraryProvider or ListProvider."
                )
        except ValueError as exc:
            raise ProfileConfigError(
                f"Failed to register provider class '{class_path}': {exc}"
            ) from exc

        _LOADED_CLASSES.add(class_path)


def _collect_class_overrides(config: AnibridgeConfig) -> set[str]:
    """Gather class paths requested globally and by the profile."""
    classes: set[str] = set(config.provider_classes or [])
    if not config.provider_classes:
        return classes
    classes.update(config.provider_classes)
    return classes


def _ensure_default_provider(namespace: str) -> None:
    """Import the default provider class for a namespace if not yet loaded."""
    class_path = _DEFAULT_LIBRARY_CLASSES_BY_NS.get(
        namespace
    ) or _DEFAULT_LIST_CLASSES_BY_NS.get(namespace)
    if class_path and class_path not in _LOADED_CLASSES:
        _register_classes((class_path,))


def build_library_provider(profile: AnibridgeProfileConfig) -> LibraryProvider:
    """Instantiate the configured library provider for the profile.

    Args:
        profile (AnibridgeProfileConfig): The profile configuration.

    Returns:
        LibraryProvider: The instantiated library provider.
    """
    _register_classes(_collect_class_overrides(profile.parent))
    _ensure_default_provider(profile.library_provider)

    namespace = profile.library_provider
    config = profile.library_provider_config.get(namespace)
    try:
        provider_cls = library_registry.get(namespace)
    except LookupError:
        logger = log
    else:
        logger = get_logger(provider_cls.__module__)

    try:
        return library_registry.create(namespace, logger=logger, config=config)
    except LookupError as exc:
        raise ProfileConfigError(
            f"No library provider registered for namespace '{namespace or 'None'}'. "
            "Ensure the provider package is installed and listed under "
            "provider_classes."
        ) from exc


def build_list_provider(profile: AnibridgeProfileConfig) -> ListProvider:
    """Instantiate the configured list provider for the profile.

    Args:
        profile (AnibridgeProfileConfig): The profile configuration.

    Returns:
        ListProvider: The instantiated list provider.
    """
    _register_classes(_collect_class_overrides(profile.parent))
    _ensure_default_provider(profile.list_provider)

    namespace = profile.list_provider
    config = profile.list_provider_config.get(namespace)
    try:
        provider_cls = list_registry.get(namespace)
    except LookupError:
        logger = log
    else:
        logger = get_logger(provider_cls.__module__)

    try:
        return list_registry.create(namespace, logger=logger, config=config)
    except LookupError as exc:
        raise ProfileConfigError(
            f"No list provider registered for namespace '{namespace}'. "
            "Ensure the provider package is installed and listed under "
            "provider_classes."
        ) from exc
