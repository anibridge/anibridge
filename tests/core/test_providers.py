"""Tests for provider loader helpers."""

from logging import Logger
from types import SimpleNamespace
from typing import cast

import pytest
from anibridge.provider.base import Account, Provider

import anibridge.app.core.providers as providers_module
from anibridge.app.exceptions import ProfileConfigError


class DummyConfig(SimpleNamespace):
    """Minimal config object exposing provider class overrides."""

    def __init__(self, provider_classes):
        super().__init__(provider_classes=provider_classes)


def test_collect_class_overrides_returns_empty_for_none() -> None:
    """No provider_classes should yield an empty override set."""
    config = DummyConfig(provider_classes=None)

    assert (
        providers_module._collect_class_overrides(
            cast("providers_module.AnibridgeConfig", config)
        )
        == set()
    )


def test_collect_class_overrides_returns_set_for_values() -> None:
    """Provider class overrides should be returned as a set."""
    config = DummyConfig(provider_classes=["pkg.a.A", "pkg.b.B"])

    assert providers_module._collect_class_overrides(
        cast("providers_module.AnibridgeConfig", config)
    ) == {"pkg.a.A", "pkg.b.B"}


def test_register_classes_skips_duplicates_and_blanks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only new, non-empty classes should be imported once."""
    calls: list[str] = []

    class FakeProvider(Provider):
        DISPLAY_NAME = "Fake"
        NAMESPACE = "fake"

        def account(self) -> Account | None:
            return None

    def fake_import(module: str) -> SimpleNamespace:
        calls.append(module)
        return SimpleNamespace(Provider=FakeProvider)

    monkeypatch.setattr(providers_module, "import_module", fake_import)
    monkeypatch.setattr(providers_module, "_LOADED_CLASSES", set())

    register_calls: list[type] = []

    class DummyRegistry:
        def register(self, provider_cls: type) -> None:
            register_calls.append(provider_cls)

    monkeypatch.setattr(providers_module, "provider_registry", DummyRegistry())

    providers_module._register_classes(
        ["mod.a.Provider", "", "mod.a.Provider", "mod.b.Provider"]
    )

    assert calls == ["mod.a", "mod.b"]
    assert len(register_calls) == 2


def test_build_provider_raises_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing providers should raise ProfileConfigError."""
    profile = SimpleNamespace(
        parent=DummyConfig(provider_classes=[]),
    )

    def fake_create(_namespace: str, logger: Logger, config=None):
        raise LookupError("missing")

    monkeypatch.setattr(providers_module.provider_registry, "create", fake_create)

    with pytest.raises(ProfileConfigError):
        providers_module.build_provider(
            "missing",
            {},
            cast("providers_module.AnibridgeProfileConfig", profile),
        )


def test_build_provider_registers_default_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AniList and Plex should be available without explicit provider_classes."""
    registered: list[str] = []
    profile = SimpleNamespace(parent=DummyConfig(provider_classes=[]))

    def fake_register_classes(class_paths) -> None:
        registered.extend(class_paths)

    class DummyRegistry:
        def get(self, namespace: str) -> type[Provider]:
            raise LookupError(namespace)

        def create(self, namespace: str, **_kwargs) -> Provider:
            raise LookupError(namespace)

    monkeypatch.setattr(providers_module, "_register_classes", fake_register_classes)
    monkeypatch.setattr(providers_module, "provider_registry", DummyRegistry())

    with pytest.raises(ProfileConfigError):
        providers_module.build_provider(
            "missing",
            {},
            cast("providers_module.AnibridgeProfileConfig", profile),
        )

    assert registered == list(providers_module._DEFAULT_PROVIDER_CLASSES)


def test_build_provider_selects_namespace_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider instances should receive only their namespace-specific config."""
    profile = SimpleNamespace(parent=DummyConfig(provider_classes=[]))
    captured_config: dict[str, object] | None = None

    class FakeProvider(Provider):
        DISPLAY_NAME = "Fake"
        NAMESPACE = "plex"

        def account(self) -> Account | None:
            return None

    class DummyRegistry:
        def get(self, namespace: str) -> type[Provider]:
            assert namespace == "plex"
            return FakeProvider

        def create(self, namespace: str, **kwargs) -> Provider:
            nonlocal captured_config
            assert namespace == "plex"
            captured_config = kwargs["config"]
            return FakeProvider(
                logger=kwargs["logger"],
                config=cast(dict[str, object], kwargs["config"]),
            )

    monkeypatch.setattr(providers_module, "_register_classes", lambda _classes: None)
    monkeypatch.setattr(providers_module, "provider_registry", DummyRegistry())

    provider = providers_module.build_provider(
        "plex",
        {
            "plex": {"url": "http://plex:32400", "token": "plex-token"},
            "emby": {"url": "http://emby:8096"},
        },
        cast("providers_module.AnibridgeProfileConfig", profile),
    )

    assert isinstance(provider, FakeProvider)
    assert captured_config == {"url": "http://plex:32400", "token": "plex-token"}
    assert provider.config == {"url": "http://plex:32400", "token": "plex-token"}


@pytest.mark.parametrize(
    "class_path",
    ["missing-separator", "package.only."],
)
def test_register_classes_rejects_invalid_class_paths(class_path: str) -> None:
    """Malformed class paths should fail fast before import resolution."""
    with pytest.raises(ProfileConfigError, match="Invalid provider class path"):
        providers_module._register_classes([class_path])


def test_register_classes_wraps_import_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Import failures should be translated into ProfileConfigError."""
    monkeypatch.setattr(providers_module, "_LOADED_CLASSES", set())
    monkeypatch.setattr(
        providers_module,
        "import_module",
        lambda _module: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(ProfileConfigError, match="Failed to import provider class"):
        providers_module._register_classes(["pkg.module.Provider"])


def test_register_classes_rejects_non_class_and_unknown_bases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolved objects must be classes inheriting from a supported provider base."""
    monkeypatch.setattr(providers_module, "_LOADED_CLASSES", set())
    monkeypatch.setattr(
        providers_module,
        "import_module",
        lambda _module: SimpleNamespace(Provider="not-a-class"),
    )

    with pytest.raises(ProfileConfigError, match="does not resolve to a class"):
        providers_module._register_classes(["pkg.module.Provider"])

    class Other:
        pass

    monkeypatch.setattr(
        providers_module,
        "import_module",
        lambda _module: SimpleNamespace(Provider=Other),
    )
    with pytest.raises(ProfileConfigError, match="must inherit from"):
        providers_module._register_classes(["pkg.module.Provider"])
