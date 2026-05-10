"""Tests for settings configuration utilities."""

from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import SecretStr

from anibridge.app.config import settings as settings_module
from anibridge.app.config.settings import (
    AnibridgeConfig,
    AnibridgeProfileConfig,
    BasicAuthConfig,
    ScanMode,
    SyncField,
    SyncRulesConfig,
    SyncRuleTemplateId,
    WebConfig,
    find_yaml_config_file,
)
from anibridge.app.core.sync.rules import SyncRuleEngine
from anibridge.app.exceptions import (
    ProfileConfigError,
    ProfileNotFoundError,
)


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


def test_profile_parent_requires_assignment() -> None:
    """Test that accessing parent on unassigned profile raises ProfileConfigError."""
    profile = AnibridgeProfileConfig(
        library_provider_config={
            "plex": {
                "token": SecretStr("plex-token"),
                "user": "eliasbenb",
                "url": "http://plex:32400",
            },
        },
        list_provider_config={"anilist": {"token": SecretStr("anilist-token")}},
    )

    with pytest.raises(ProfileConfigError):
        _ = profile.parent


def test_config_creates_default_profile_from_globals() -> None:
    """Test that AnibridgeConfig creates a default profile from global settings."""
    config = AnibridgeConfig(
        global_config=AnibridgeProfileConfig(
            library_provider_config={
                "plex": {
                    "token": "plex-token",
                    "user": "eliasbenb",
                    "url": "http://plex:32400",
                    "sections": ["Anime"],
                },
            },
            list_provider_config={"anilist": {"token": "anilist-token"}},
        )
    )

    profile = config.get_profile("default")

    assert profile.parent is config
    assert profile.list_provider_config["anilist"]["token"] == "anilist-token"
    assert profile.library_provider_config["plex"]["token"] == "plex-token"
    assert profile.library_provider_config["plex"]["user"] == "eliasbenb"
    assert profile.library_provider_config["plex"]["url"] == "http://plex:32400"
    assert profile.library_provider_config["plex"]["sections"] == ["Anime"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("", "", id="empty"),
        pytest.param("/", "", id="root"),
        pytest.param("anibridge", "/anibridge", id="adds-leading-slash"),
        pytest.param("/anibridge/", "/anibridge", id="strips-trailing-slash"),
        pytest.param(" /nested/path/ ", "/nested/path", id="trims-whitespace"),
    ],
)
def test_web_config_normalizes_path_prefix(raw: str, expected: str) -> None:
    config = WebConfig(path_prefix=raw)

    assert config.path_prefix == expected


def test_config_profile_inherits_global_values() -> None:
    """Test that a profile inherits global settings from AnibridgeConfig."""
    config = AnibridgeConfig(
        global_config=AnibridgeProfileConfig(
            library_provider_config={
                "plex": {"url": "http://global"},
            }
        ),
        profiles={
            "primary": AnibridgeProfileConfig(
                library_provider_config={
                    "anilist": {"token": "anilist-token"},
                }
            )
        },
    )

    profile = config.get_profile("primary")

    assert profile.library_provider_config["plex"]["url"] == "http://global"


def test_provider_config_merges_one_level_per_namespace() -> None:
    """Test provider config merge keeps global keys and applies profile overrides."""
    config = AnibridgeConfig(
        global_config=AnibridgeProfileConfig(
            library_provider_config={
                "plex": {
                    "url": "http://global",
                    "token": "global-token",
                    "advanced": {"timeout": 30, "retry": 2},
                }
            }
        ),
        profiles={
            "primary": AnibridgeProfileConfig(
                library_provider_config={
                    "plex": {
                        "sections": ["Anime"],
                        "advanced": {"timeout": 60},
                    }
                }
            )
        },
    )

    profile = config.get_profile("primary")

    assert profile.library_provider_config["plex"]["url"] == "http://global"
    assert profile.library_provider_config["plex"]["token"] == "global-token"
    assert profile.library_provider_config["plex"]["sections"] == ["Anime"]
    assert profile.library_provider_config["plex"]["advanced"] == {"timeout": 60}


def test_get_profile_raises_for_unknown_name(
    tmp_path: Path,
) -> None:
    """Test that get_profile raises ProfileNotFoundError for unknown profile names."""
    config = AnibridgeConfig()

    with pytest.raises(ProfileNotFoundError):
        config.get_profile("missing")


def test_sync_rules_accept_declarative_field_rules() -> None:
    """Declarative sync rules should validate and preserve runtime expressions."""
    rules = SyncRulesConfig.model_validate(
        {
            "vars": {
                "has_review": (
                    "computed.review is not None and len(computed.review) > 0"
                ),
                "is_special_item": 'ctx.item.title == "Movie"',
            },
            "status": [
                {
                    "name": "Promote rewatch",
                    "if": (
                        "current.status == ListStatus.COMPLETED and "
                        "computed.status == ListStatus.CURRENT"
                    ),
                    "set": "ListStatus.REPEATING",
                }
            ],
            "review": [
                {
                    "name": "Clear empty review",
                    "if": "not vars.has_review",
                    "set": None,
                }
            ],
        }
    )

    field_rules = rules.field_rules()
    status_rules = cast(list[dict[str, object]], field_rules["status"])
    review_rules = cast(list[dict[str, object]], field_rules["review"])

    assert status_rules[0]["if"] == (
        "current.status == ListStatus.COMPLETED and "
        "computed.status == ListStatus.CURRENT"
    )
    assert status_rules[0]["set"] == "ListStatus.REPEATING"
    assert "set" in review_rules[0]
    assert review_rules[0]["set"] is None


def test_sync_rules_user_rules_precede_template_rules() -> None:
    """User field rules should run before built-in template fallback rules."""
    rules = SyncRulesConfig.model_validate(
        {
            "templates": [SyncRuleTemplateId.PROMOTE_REWATCH],
            "status": [
                {
                    "name": "User rule",
                    "if": "computed.status == current.status",
                    "set": "current.status",
                }
            ],
        }
    )

    status_rules = cast(list[dict[str, object]], rules.field_rules()["status"])

    assert status_rules[0]["name"] == "User rule"
    assert status_rules[1]["name"] == "Promote rewatch to repeating"


def test_sync_rules_disable_dropped_and_paused_template_adds_status_guard() -> None:
    """Dropped/paused template should add the expected status guard rule."""
    rules = SyncRulesConfig.model_validate(
        {
            "templates": [SyncRuleTemplateId.DISABLE_DROPPED_AND_PAUSED],
        }
    )

    status_rules = cast(list[dict[str, object]], rules.field_rules()["status"])

    assert status_rules[0]["name"] == "Don't sync dropped or paused status changes"
    assert status_rules[0]["if"] == (
        "computed.status in (ListStatus.DROPPED, ListStatus.PAUSED)"
    )
    assert status_rules[0]["set"] == (
        "ListStatus.CURRENT if current.status is None else current.status"
    )


def test_sync_rules_promote_rewatch_template_adds_status_promotion_rule() -> None:
    """Promote rewatch template should add the status promotion rule."""
    rules = SyncRulesConfig.model_validate(
        {
            "templates": [SyncRuleTemplateId.PROMOTE_REWATCH],
        }
    )

    status_rules = cast(list[dict[str, object]], rules.field_rules()["status"])

    assert status_rules[0]["name"] == "Promote rewatch to repeating"
    assert status_rules[0]["if"] == (
        "current.status in (ListStatus.COMPLETED, ListStatus.REPEATING) and "
        "computed.status == ListStatus.CURRENT"
    )
    assert status_rules[0]["set"] == "ListStatus.REPEATING"


def test_sync_rules_disable_review_and_rating_template_overrides_defaults() -> None:
    """The disable template should force review and user_rating off."""
    rules = SyncRulesConfig.model_validate(
        {"templates": [SyncRuleTemplateId.DISABLE_USER_RATING_AND_REVIEW]}
    )

    assert rules.field_rules()["review"] is False
    assert rules.field_rules()["user_rating"] is False
    assert rules.templates == [SyncRuleTemplateId.DISABLE_USER_RATING_AND_REVIEW]


def test_sync_rules_default_templates_disable_review_and_gate_ratings() -> None:
    """Defaults should disable reviews and ratings without user overrides."""
    rules = SyncRulesConfig()

    field_rules = rules.field_rules()

    assert rules.templates[:2] == [
        SyncRuleTemplateId.USER_RATING_REQUIRES_COMPLETED,
        SyncRuleTemplateId.DISABLE_USER_RATING_AND_REVIEW,
    ]
    assert field_rules["review"] is False
    assert field_rules["user_rating"] is False


def test_sync_rules_prevent_regressions_template_adds_guard_rules() -> None:
    """The regression template should add keep-current rules for decreasing fields."""
    rules = SyncRulesConfig.model_validate(
        {
            "templates": [SyncRuleTemplateId.PREVENT_REGRESSIONS],
        }
    )
    progress_rules = cast(list[dict[str, object]], rules.field_rules()["progress"])
    status_rules = cast(list[dict[str, object]], rules.field_rules()["status"])

    assert progress_rules[0]["if"] == (
        "current.progress is not None and "
        "(computed.progress is None or computed.progress < current.progress)"
    )
    assert progress_rules[0]["set"] == "current.progress"
    assert status_rules[0]["if"] == (
        "current.status is not None and "
        "(computed.status is None or computed.status < current.status)"
    )


def test_sync_rules_explicit_false_overrides_template_field_rules() -> None:
    """Explicit field disables should still beat template-provided rule lists."""
    rules = SyncRulesConfig.model_validate(
        {
            "templates": [SyncRuleTemplateId.PREVENT_REGRESSIONS],
            "progress": False,
        }
    )

    assert rules.field_rules()["progress"] is False


def test_sync_rules_reject_unknown_template_ids() -> None:
    """Unknown built-in template IDs should fail validation."""
    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate({"templates": ["missing-template"]})


def test_sync_rules_reject_reserved_ctx_variable_name() -> None:
    """sync_rules.vars cannot redefine the ctx namespace."""
    with pytest.raises(ValueError):
        SyncRulesConfig(vars={"ctx": "True"})


def test_sync_rules_reject_none_field_values() -> None:
    """Declarative sync rule fields should not accept null values."""
    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate({"status": None})


def test_sync_rules_reject_rule_without_set() -> None:
    """Declarative sync rules must provide an explicit set value."""
    with pytest.raises(ValueError):
        SyncRulesConfig.model_validate(
            {
                "review": [
                    {
                        "if": "computed.review is not None",
                    }
                ]
            }
        )


def test_sync_rules_reject_invalid_variable_names() -> None:
    """sync_rules.vars names must be safe Python identifiers."""
    with pytest.raises(ValueError):
        SyncRulesConfig(vars={"current": "True"})


@pytest.mark.parametrize(
    ("yaml_set_value", "expected_rule_set"),
    [("null", None), ("None", "None")],
)
def test_sync_rules_yaml_set_values_preserve_null_and_none_semantics(
    yaml_set_value: str,
    expected_rule_set: object,
) -> None:
    """YAML null and bare None should preserve their expected sync-rule meaning."""
    payload = yaml.safe_load(
        "global_config:\n"
        "  sync_rules:\n"
        "    review:\n"
        "      - name: Clear review\n"
        f"        set: {yaml_set_value}\n"
    )

    rules = SyncRulesConfig.model_validate(payload["global_config"]["sync_rules"])
    review_rules = cast(list[dict[str, object]], rules.field_rules()["review"])

    assert review_rules[0]["set"] == expected_rule_set

    decision = SyncRuleEngine(
        variables=rules.resolved_vars(),
        field_rules=rules.field_rules(),
    ).evaluate_field(
        field_name="review",
        current_values={"review": "existing"},
        computed_values={"review": "computed"},
    )

    assert decision.allowed is True
    assert decision.value is None
    assert decision.reason == "Clear review"


def test_web_config_reports_auth_configuration_state(tmp_path: Path) -> None:
    """WebConfig should correctly report whether authentication is configured."""
    default = WebConfig()
    assert default.has_auth is False

    with_credentials = WebConfig(
        basic_auth=BasicAuthConfig(username="admin", password=SecretStr("secret"))
    )
    assert with_credentials.has_auth is True

    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("user:$apr1$hash", encoding="utf-8")
    with_htpasswd = WebConfig(basic_auth=BasicAuthConfig(htpasswd_path=htpasswd))
    assert with_htpasswd.has_auth is True


def test_unconfigured_config_allows_config_api_without_auth() -> None:
    """Default/unconfigured app should allow config API access without auth."""
    config = AnibridgeConfig()

    assert config.web.has_auth is False
    assert config.web.allow_config_without_auth is True


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


def test_sync_field_names_returns_all_enum_values() -> None:
    """SyncField.field_names should expose every enum value once."""
    assert SyncField.field_names() == tuple(field.value for field in SyncField)


def test_profile_merge_globals_no_parent_returns_self() -> None:
    """Profile config merge should be a no-op when no parent is assigned."""
    profile = AnibridgeProfileConfig(scan_modes=[ScanMode.POLL])

    assert profile._merge_globals() is profile
    assert profile.scan_modes == [ScanMode.POLL]


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
            allow_config_without_auth=False,
        )
    )

    assert config.web.basic_auth.username is None
    assert config.web.basic_auth.password is None


def test_invalid_htpasswd_path_is_rejected(tmp_path: Path) -> None:
    """Configured htpasswd files must exist on disk."""
    with pytest.raises(ValueError, match="htpasswd_path"):
        AnibridgeConfig(
            web=WebConfig(
                basic_auth=BasicAuthConfig(htpasswd_path=tmp_path / "missing")
            )
        )


def test_config_string_and_default_template_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config helpers should render readable summaries and create default templates."""
    config = AnibridgeConfig(profiles={"alpha": AnibridgeProfileConfig()})
    assert "alpha" in str(config)
    assert "1 profile" in str(config)

    template = settings_module._render_default_config_template()
    assert template.startswith("################################################")
    assert "# profiles:" in template

    monkeypatch.setenv("AB_DATA_PATH", str(tmp_path))
    created = settings_module._ensure_default_config_file()
    assert created.exists()
    assert created.read_text(encoding="utf-8").startswith("################")
    assert settings_module._ensure_default_config_file() == created


def test_threads_defaults_to_profile_count_plus_one() -> None:
    """Thread count should default to len(profiles) + 1 when not set."""
    config = AnibridgeConfig(
        profiles={
            "a": AnibridgeProfileConfig(),
            "b": AnibridgeProfileConfig(),
            "c": AnibridgeProfileConfig(),
        }
    )

    assert config.threads == 4


def test_threads_defaults_to_one_with_no_profiles() -> None:
    """Thread count should be 1 when there are no profiles and threads is unset."""
    config = AnibridgeConfig()

    assert config.threads == 1


def test_threads_defaults_to_two_with_implicit_default_profile() -> None:
    """Implicit default profile from globals should count toward thread default."""
    config = AnibridgeConfig(
        global_config=AnibridgeProfileConfig(
            library_provider_config={"plex": {"url": "http://plex:32400"}},
        )
    )

    assert "default" in config.profiles
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
