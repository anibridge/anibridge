"""Tests for settings configuration utilities."""

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from anibridge.app.config import settings as settings_module
from anibridge.app.config.settings import (
    AnibridgeConfig,
    AnibridgeProfileConfig,
    BasicAuthConfig,
    SyncRulesConfig,
    WebConfig,
    find_yaml_config_file,
)
from anibridge.app.config.sync_rules import (
    SyncRuleDefinition,
    SyncRuleSelector,
    SyncRuleTemplateId,
    SyncRuleTemplateItem,
)
from anibridge.app.exceptions import ProfileNotFoundError


@pytest.fixture(autouse=True)
def isolate_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Set the working directory to a temporary path for each test."""
    monkeypatch.chdir(tmp_path)


def test_find_yaml_config_file_prefers_data_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that find_yaml_config_file prefers AB_DATA_PATH environment variable."""
    monkeypatch.setenv("AB_DATA_PATH", str(tmp_path))
    config_file = tmp_path / "config.yaml"
    config_file.write_text("root: true", encoding="utf-8")

    result = find_yaml_config_file()

    assert result == config_file.resolve()


def test_environment_values_override_yaml_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AB_ environment values should take precedence over YAML config files."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("threads: 2\n", encoding="utf-8")
    monkeypatch.setenv("AB_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("AB_THREADS", "8")

    config = AnibridgeConfig()

    assert config.threads == 8


def test_profile_provider_config_is_explicit_per_profile() -> None:
    """Provider config should come directly from the profile payload."""
    config = AnibridgeConfig(
        profiles={
            "primary": AnibridgeProfileConfig(
                source_provider_config={
                    "plex": {
                        "url": "http://profile",
                        "token": "profile-token",
                        "sections": ["Anime"],
                        "advanced": {"timeout": 60},
                    }
                }
            )
        },
    )

    profile = config.get_profile("primary")
    source_config = cast(dict[str, Any], profile.source_provider_config["plex"])

    assert source_config["url"] == "http://profile"
    assert source_config["token"] == "profile-token"
    assert source_config["sections"] == ["Anime"]
    assert source_config["advanced"] == {"timeout": 60}


def test_global_config_merges_into_profiles() -> None:
    """Global profile config should provide defaults for explicit profiles."""
    config = AnibridgeConfig(
        global_config=AnibridgeProfileConfig(
            source_provider="plex",
            source_provider_config={
                "plex": {
                    "url": "http://global",
                    "token": "global-token",
                }
            },
        ),
        profiles={
            "primary": AnibridgeProfileConfig(
                target_provider="anilist",
                source_provider_config={"plex": {"sections": ["Anime"]}},
            )
        },
    )

    profile = config.get_profile("primary")
    source_config = cast(dict[str, Any], profile.source_provider_config["plex"])

    assert profile.source_provider == "plex"
    assert profile.target_provider == "anilist"
    assert source_config == {
        "url": "http://global",
        "token": "global-token",
        "sections": ["Anime"],
    }


def test_profile_provider_compatibility_aliases() -> None:
    """Legacy library/list provider names should populate provider settings."""
    profile = AnibridgeProfileConfig(
        library_provider="plex",
        list_provider="anilist",
        library_provider_config={"plex": {"url": "http://plex"}},
        list_provider_config={"anilist": {"token": "anilist-token"}},
    )

    assert profile.source_provider == "plex"
    assert profile.target_provider == "anilist"
    assert profile.source_provider_config == {"plex": {"url": "http://plex"}}
    assert profile.target_provider_config == {"anilist": {"token": "anilist-token"}}


def test_provider_compatibility_aliases_merge_from_global_config() -> None:
    """Aliased global provider names should merge into explicit profiles."""
    config = AnibridgeConfig(
        global_config=AnibridgeProfileConfig(
            library_provider="plex",
            list_provider="anilist",
            library_provider_config={"plex": {"url": "http://global"}},
            list_provider_config={"anilist": {"token": "global-token"}},
        ),
        profiles={
            "primary": AnibridgeProfileConfig(
                dry_run=True,
                library_provider_config={"plex": {"sections": ["Anime"]}},
            )
        },
    )

    profile = config.get_profile("primary")

    assert profile.source_provider == "plex"
    assert profile.target_provider == "anilist"
    assert profile.source_provider_config == {
        "plex": {"url": "http://global", "sections": ["Anime"]}
    }
    assert profile.target_provider_config == {"anilist": {"token": "global-token"}}
    assert profile.dry_run is True


def test_provider_config_requires_namespace_mapping() -> None:
    """Provider config payloads should be keyed by provider namespace."""
    with pytest.raises(ValueError):
        AnibridgeProfileConfig(
            source_provider_config={
                "url": "http://plex:32400",
                "token": "plex-token",
            }  # ty:ignore[invalid-argument-type]
        )


def test_get_profile_raises_for_unknown_name(
    tmp_path: Path,
) -> None:
    """Test that get_profile raises ProfileNotFoundError for unknown profile names."""
    config = AnibridgeConfig()

    with pytest.raises(ProfileNotFoundError):
        config.get_profile("missing")


def test_sync_rules_accepts_template_and_rule_items() -> None:
    """Sync rules should validate ordered template and rule items."""
    rules = SyncRulesConfig.model_validate(
        [
            {"template": "prevent-regression"},
            {"template": "promote-rewatch"},
            {"template": "require-completed-for-rating"},
            {
                "name": "Promote rewatch",
                "selector": "record.status",
                "if": (
                    "dst.status in (Status.COMPLETED, Status.REPEATING) "
                    "and src.status == Status.ACTIVE"
                ),
                "value": "Status.REPEATING",
            },
        ]
    )

    first = rules.root[0]
    second = rules.root[1]
    third = rules.root[2]
    fourth = rules.root[3]

    assert isinstance(first, SyncRuleTemplateItem)
    assert isinstance(second, SyncRuleTemplateItem)
    assert isinstance(third, SyncRuleTemplateItem)
    assert isinstance(fourth, SyncRuleDefinition)
    assert first.template == SyncRuleTemplateId.PREVENT_REGRESSION
    assert second.template == SyncRuleTemplateId.PROMOTE_REWATCH
    assert third.template == SyncRuleTemplateId.REQUIRE_COMPLETED_FOR_RATING
    assert fourth.if_expr.startswith("dst.status")
    assert fourth.value == "Status.REPEATING"


def test_sync_rules_accepts_if_alias_and_single_action_rules() -> None:
    """Rules should allow conditions and one action."""
    rules = SyncRulesConfig.model_validate(
        [
            {
                "name": "Promote rewatch",
                "selector": "record.status",
                "if": "dst.status == Status.COMPLETED",
                "value": "Status.REPEATING",
            },
            {
                "selector": "event.delete",
                "skip": True,
            },
            {
                "selector": "node.*",
                "if": "node.kind == 'movie'",
                "skip": True,
            },
        ]
    )

    first_rule = rules.root[0]
    second_rule = rules.root[1]
    third_rule = rules.root[2]

    assert isinstance(first_rule, SyncRuleDefinition)
    assert isinstance(second_rule, SyncRuleDefinition)
    assert isinstance(third_rule, SyncRuleDefinition)
    assert first_rule.if_expr == "dst.status == Status.COMPLETED"
    assert first_rule.value == "Status.REPEATING"
    assert second_rule.selector == SyncRuleSelector.EVENT_DELETE
    assert second_rule.skip is True
    assert third_rule.selector == SyncRuleSelector.NODE_ANY


def test_sync_rules_defaults_to_prevent_regression() -> None:
    """Defaults should prevent record field regression."""
    rules = SyncRulesConfig()

    assert rules.root == [
        SyncRuleTemplateItem(template=SyncRuleTemplateId.PREVENT_REGRESSION)
    ]


def test_sync_rules_rejects_unknown_template_ids() -> None:
    """Unknown built-in template IDs should fail validation."""
    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate([{"template": "missing-template"}])


def test_sync_rules_rejects_unknown_selectors() -> None:
    """Unknown rule selectors should fail validation."""
    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate([{"selector": "event.watch", "skip": True}])

    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate([{"selector": "notes", "skip": True}])

    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate([{"selector": "node.upsert", "skip": True}])


def test_sync_rules_rejects_rule_without_single_action() -> None:
    """Sync rules should define exactly one action key."""
    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate(
            [{"selector": "record.notes", "if": "src.notes is None"}]
        )

    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate(
            [{"selector": "record.notes", "skip": True, "value": "src.notes"}]
        )


def test_web_config_reports_auth_configuration_state() -> None:
    """WebConfig should correctly report whether authentication is configured."""
    default = WebConfig()
    assert default.has_auth is False
    assert default.allows_config_api is False

    with_credentials = WebConfig(
        basic_auth=BasicAuthConfig(username="admin", password=SecretStr("secret"))
    )
    assert with_credentials.has_auth is True
    assert with_credentials.allows_config_api is True

    with_htpasswd = WebConfig(basic_auth=BasicAuthConfig(htpasswd_path=Path("users")))
    assert with_htpasswd.has_auth is True
    assert with_htpasswd.allows_config_api is True

    without_auth_override = WebConfig(allow_config_without_auth=True)
    assert without_auth_override.has_auth is False
    assert without_auth_override.allows_config_api is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("", "", id="empty"),
        pytest.param("/", "", id="root"),
        pytest.param("anibridge", "/anibridge", id="adds-leading-slash"),
        pytest.param("/anibridge/", "/anibridge", id="strips-trailing-slash"),
    ],
)
def test_web_config_normalizes_path_prefix(raw: str, expected: str) -> None:
    assert WebConfig(path_prefix=raw).path_prefix == expected


def test_config_schema_includes_extra_behavior_metadata() -> None:
    """Config schema should expose extra-handling metadata for the editor."""
    schema = AnibridgeConfig.model_json_schema()
    definitions = schema["$defs"]

    assert schema["x-anibridge-extraBehavior"] == "ignore"
    assert (
        definitions["AnibridgeProfileConfig"]["x-anibridge-extraBehavior"] == "ignore"
    )
    assert definitions["WebConfig"]["x-anibridge-extraBehavior"] == "ignore"
    assert definitions["BasicAuthConfig"]["x-anibridge-extraBehavior"] == "ignore"


def test_config_data_path_uses_environment_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cached data_path property should resolve AB_DATA_PATH."""
    monkeypatch.setenv("AB_DATA_PATH", str(tmp_path))

    assert AnibridgeConfig().data_path == tmp_path.resolve()


def test_partial_basic_auth_credentials_are_cleared() -> None:
    """Half-configured static auth credentials should be ignored."""
    config = AnibridgeConfig(
        web=WebConfig(
            basic_auth=BasicAuthConfig(username="admin", password=None),
        )
    )

    assert config.web.basic_auth.username is None
    assert config.web.basic_auth.password is None


def test_missing_htpasswd_file_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.htpasswd"

    with pytest.raises(ValueError, match="htpasswd file does not exist"):
        AnibridgeConfig(
            web=WebConfig(basic_auth=BasicAuthConfig(htpasswd_path=missing))
        )


def test_config_string_and_default_template_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config helpers should render readable summaries and create default templates."""
    monkeypatch.setenv("AB_DATA_PATH", str(tmp_path))
    config = AnibridgeConfig(profiles={"alpha": AnibridgeProfileConfig()})
    assert "alpha" in str(config)
    assert "1 profile" in str(config)

    template = settings_module._render_default_config_template()
    assert template.startswith("################################################")
    assert "# profiles:" in template

    created = settings_module._ensure_default_config_file()
    assert created.exists()
    assert created.read_text(encoding="utf-8").startswith("################")
    assert settings_module._ensure_default_config_file() == created


def test_threads_defaults_to_profile_count_plus_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Thread count should default to len(profiles) + 1 when not set."""
    monkeypatch.setenv("AB_DATA_PATH", str(tmp_path))
    config = AnibridgeConfig(
        profiles={
            "a": AnibridgeProfileConfig(),
            "b": AnibridgeProfileConfig(),
            "c": AnibridgeProfileConfig(),
        }
    )

    assert config.threads == 4


def test_threads_defaults_to_one_with_no_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Thread count should be 1 when there are no profiles and threads is unset."""
    monkeypatch.setenv("AB_DATA_PATH", str(tmp_path))
    config = AnibridgeConfig()

    assert config.threads == 1


def test_global_config_creates_implicit_default_profile() -> None:
    """Global-only config should create a default profile."""
    config = AnibridgeConfig(
        global_config=AnibridgeProfileConfig(source_provider="plex")
    )

    assert config.profiles["default"].source_provider == "plex"
    assert config.threads == 2


def test_threads_explicit_value_overrides_default() -> None:
    """Explicitly set threads should not be overridden by the profile-based default."""
    config = AnibridgeConfig(
        threads=8,
        profiles={
            "a": AnibridgeProfileConfig(),
            "b": AnibridgeProfileConfig(),
        },
    )

    assert config.threads == 8


def test_threads_rejects_zero() -> None:
    """Thread count of 0 should be rejected by validation."""
    with pytest.raises(ValueError):
        AnibridgeConfig(threads=0)
