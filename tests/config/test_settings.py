"""Tests for settings configuration utilities."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from anibridge.app.config.settings import (
    AniBridgeConfig,
    AniBridgeProfileConfig,
    BasicAuthConfig,
    SyncField,
    WebConfig,
    find_yaml_config_file,
)
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
    profile = AniBridgeProfileConfig(
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
    """Test that AniBridgeConfig creates a default profile from global settings."""
    config = AniBridgeConfig(
        global_config=AniBridgeProfileConfig(
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


def test_config_profile_inherits_global_values() -> None:
    """Test that a profile inherits global settings from AniBridgeConfig."""
    config = AniBridgeConfig(
        global_config=AniBridgeProfileConfig(
            library_provider_config={
                "plex": {"url": "http://global"},
            }
        ),
        profiles={
            "primary": AniBridgeProfileConfig(
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
    config = AniBridgeConfig(
        global_config=AniBridgeProfileConfig(
            library_provider_config={
                "plex": {
                    "url": "http://global",
                    "token": "global-token",
                    "advanced": {"timeout": 30, "retry": 2},
                }
            }
        ),
        profiles={
            "primary": AniBridgeProfileConfig(
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
    config = AniBridgeConfig()

    with pytest.raises(ProfileNotFoundError):
        config.get_profile("missing")


def test_sync_fields_rejects_unknown_operator() -> None:
    """Unknown sync field operators should fail validation."""
    with pytest.raises(ValueError):
        AniBridgeProfileConfig(sync_fields={SyncField.STATUS: {"_between": False}})


def test_sync_fields_inherit_from_global_profile() -> None:
    """Global sync_fields should be inherited when a profile omits sync_fields."""
    config = AniBridgeConfig.model_validate(
        {
            "global_config": {
                "library_provider": "plex",
                "sync_fields": {"review": False, "status": False},
            },
            "profiles": {
                "anilist": {
                    "list_provider": "anilist",
                    "list_provider_config": {"anilist": {"token": "token"}},
                }
            },
        }
    )

    profile = config.get_profile("anilist")

    assert profile.sync_fields[SyncField.REVIEW] is False
    assert profile.sync_fields[SyncField.STATUS] is False


def test_sync_fields_status_rules_are_case_insensitive() -> None:
    """Status rule keys should normalize to ListStatus values."""
    profile = AniBridgeProfileConfig(
        sync_fields={
            SyncField.STATUS: {"dropped": False, "PAUSED": False, "pLaNNing": False}
        }
    )

    status_rules = profile.sync_fields[SyncField.STATUS]
    assert isinstance(status_rules, dict)
    assert status_rules["dropped"] is False
    assert status_rules["paused"] is False
    assert status_rules["planning"] is False


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
