"""Tests for provider loader helpers."""

from logging import Logger
from types import SimpleNamespace
from typing import cast

import pytest

import anibridge.app.core.providers as providers_module
from anibridge.app.exceptions import ProfileConfigError


class DummyConfig(SimpleNamespace):
    """Minimal config object exposing provider module overrides."""

    def __init__(self, provider_modules):
        super().__init__(provider_modules=provider_modules)


def test_collect_module_overrides_returns_empty_for_none() -> None:
    """No provider_modules should yield an empty override set."""
    config = DummyConfig(provider_modules=None)

    assert (
        providers_module._collect_module_overrides(
            cast("providers_module.AniBridgeConfig", config)
        )
        == set()
    )


def test_collect_module_overrides_returns_set_for_values() -> None:
    """Provider module overrides should be returned as a set."""
    config = DummyConfig(provider_modules=["mod.a", "mod.b"])

    assert providers_module._collect_module_overrides(
        cast("providers_module.AniBridgeConfig", config)
    ) == {"mod.a", "mod.b"}


def test_import_modules_skips_duplicates_and_blanks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only new, non-empty modules should be imported once."""
    calls: list[str] = []

    def fake_import(module: str) -> None:
        calls.append(module)

    monkeypatch.setattr(providers_module, "import_module", fake_import)
    monkeypatch.setattr(providers_module, "_LOADED_MODULES", set())

    providers_module._import_modules(["mod.a", "", "mod.a", "mod.b"])

    assert calls == ["mod.a", "mod.b"]


def test_build_library_provider_raises_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing library providers should raise ProfileConfigError."""
    profile = SimpleNamespace(
        library_provider="missing",
        library_provider_config={},
        parent=DummyConfig(provider_modules=[]),
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
        parent=DummyConfig(provider_modules=[]),
    )

    def fake_create(_namespace: str, logger: Logger, config=None):
        raise LookupError("missing")

    monkeypatch.setattr(providers_module.list_registry, "create", fake_create)

    with pytest.raises(ProfileConfigError):
        providers_module.build_list_provider(
            cast("providers_module.AniBridgeProfileConfig", profile)
        )
