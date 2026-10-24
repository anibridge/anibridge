"""Tests for provider loader helpers."""

from types import SimpleNamespace
from typing import cast

import pytest
from anibridge.utils.registry import ProviderRegistry

import anibridge.app.core.providers as providers_module
from anibridge.app.exceptions import ProfileConfigError


class DummyConfig(SimpleNamespace):
    """Minimal config object exposing provider class overrides."""

    def __init__(self, provider_classes):
        super().__init__(provider_classes=provider_classes)


@pytest.fixture(autouse=True)
def reset_provider_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers_module, "library_registry", ProviderRegistry())
    monkeypatch.setattr(providers_module, "list_registry", ProviderRegistry())
    monkeypatch.setattr(providers_module, "_LOADED_CLASSES", set())


def test_collect_class_overrides_returns_empty_for_none() -> None:
    """No provider_classes should yield no overrides."""
    config = DummyConfig(provider_classes=None)

    assert (
        providers_module._collect_class_overrides(
            cast("providers_module.AnibridgeConfig", config)
        )
        == ()
    )


def test_collect_class_overrides_preserves_unique_registration_order() -> None:
    config = DummyConfig(provider_classes=["pkg.b.B", "pkg.a.A", "pkg.b.B"])

    assert providers_module._collect_class_overrides(
        cast("providers_module.AnibridgeConfig", config)
    ) == ("pkg.b.B", "pkg.a.A")


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

    with pytest.raises(ProfileConfigError):
        providers_module.build_library_provider(
            cast("providers_module.AnibridgeProfileConfig", profile)
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

    with pytest.raises(ProfileConfigError):
        providers_module.build_list_provider(
            cast("providers_module.AnibridgeProfileConfig", profile)
        )


@pytest.mark.parametrize(("kind", "namespace"), [("library", "plex"), ("list", "mal")])
def test_configured_provider_takes_precedence_over_default(
    monkeypatch: pytest.MonkeyPatch, kind: str, namespace: str
) -> None:
    provider_base = type("ProviderBase", (), {})

    class CustomProvider(provider_base):
        NAMESPACE = namespace

        def __init__(self, *, logger, config):
            self.config = config

    monkeypatch.setattr(
        providers_module,
        "LibraryProvider" if kind == "library" else "ListProvider",
        provider_base,
    )
    imports: list[str] = []

    def fake_import(module: str) -> SimpleNamespace:
        imports.append(module)
        return SimpleNamespace(Provider=CustomProvider)

    monkeypatch.setattr(providers_module, "import_module", fake_import)
    profile = SimpleNamespace(
        parent=DummyConfig(provider_classes=["custom.Provider"]),
        **{
            f"{kind}_provider": namespace,
            f"{kind}_provider_config": {namespace: {"x": 1}},
        },
    )
    build = getattr(providers_module, f"build_{kind}_provider")

    instance = build(profile)

    assert isinstance(instance, CustomProvider)
    assert instance.config == {"x": 1}
    assert imports == ["custom"]


@pytest.mark.parametrize(("kind", "namespace"), [("library", "plex"), ("list", "mal")])
def test_provider_constructor_lookup_errors_are_not_registration_errors(
    kind: str, namespace: str
) -> None:
    class BrokenProvider:
        NAMESPACE = namespace

        def __init__(self, **kwargs):
            raise LookupError("provider initialization failed")

    registry = getattr(providers_module, f"{kind}_registry")
    registry.register(BrokenProvider)
    profile = SimpleNamespace(
        parent=DummyConfig(provider_classes=[]),
        **{f"{kind}_provider": namespace, f"{kind}_provider_config": {}},
    )
    build = getattr(providers_module, f"build_{kind}_provider")

    with pytest.raises(LookupError, match="provider initialization failed"):
        build(profile)


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
    monkeypatch.setattr(
        providers_module, "LibraryProvider", type("LibraryBase", (), {})
    )
    monkeypatch.setattr(providers_module, "ListProvider", type("ListBase", (), {}))

    with pytest.raises(ProfileConfigError, match="must inherit from"):
        providers_module._register_classes(["pkg.module.Provider"])
