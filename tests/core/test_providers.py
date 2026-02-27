"""Tests for provider loader helpers."""

from logging import Logger
from types import SimpleNamespace
from typing import cast

import pytest

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
            cast("providers_module.AniBridgeConfig", config)
        )
        == set()
    )


def test_collect_class_overrides_returns_set_for_values() -> None:
    """Provider class overrides should be returned as a set."""
    config = DummyConfig(provider_classes=["pkg.a.A", "pkg.b.B"])

    assert providers_module._collect_class_overrides(
        cast("providers_module.AniBridgeConfig", config)
    ) == {"pkg.a.A", "pkg.b.B"}


def test_register_classes_skips_duplicates_and_blanks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only new, non-empty classes should be imported once."""
    calls: list[str] = []

    class FakeLibraryProvider:
        NAMESPACE = "fake"

    def fake_import(module: str) -> SimpleNamespace:
        calls.append(module)
        return SimpleNamespace(Provider=FakeLibraryProvider)

    monkeypatch.setattr(providers_module, "import_module", fake_import)
    monkeypatch.setattr(providers_module, "_LOADED_CLASSES", set())
    monkeypatch.setattr(providers_module, "LibraryProvider", object)
    monkeypatch.setattr(providers_module, "ListProvider", type("ListBase", (), {}))

    register_calls: list[type] = []

    class DummyRegistry:
        def register(self, provider_cls: type) -> None:
            register_calls.append(provider_cls)

    monkeypatch.setattr(providers_module, "library_registry", DummyRegistry())
    monkeypatch.setattr(providers_module, "list_registry", DummyRegistry())

    providers_module._register_classes(
        ["mod.a.Provider", "", "mod.a.Provider", "mod.b.Provider"]
    )

    assert calls == ["mod.a", "mod.b"]
    assert len(register_calls) == 2


def test_build_library_provider_raises_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing library providers should raise ProfileConfigError."""
    profile = SimpleNamespace(
        library_provider="missing",
        library_provider_config={},
        parent=DummyConfig(provider_classes=[]),
    )

    def fake_create(_namespace: str, logger: Logger, config=None):
        raise LookupError("missing")

    monkeypatch.setattr(providers_module.library_registry, "create", fake_create)

    with pytest.raises(ProfileConfigError):
        providers_module.build_library_provider(
            cast("providers_module.AniBridgeProfileConfig", profile)
        )


def test_build_list_provider_raises_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing list providers should raise ProfileConfigError."""
    profile = SimpleNamespace(
        list_provider="missing",
        list_provider_config={},
        parent=DummyConfig(provider_classes=[]),
    )

    def fake_create(_namespace: str, logger: Logger, config=None):
        raise LookupError("missing")

    monkeypatch.setattr(providers_module.list_registry, "create", fake_create)

    with pytest.raises(ProfileConfigError):
        providers_module.build_list_provider(
            cast("providers_module.AniBridgeProfileConfig", profile)
        )
