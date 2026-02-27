"""Tests for version utility helpers."""

import string
import tomllib
from pathlib import Path

import pytest

from anibridge.app.utils import version as version_module


def test_get_pyproject_version_matches_pyproject() -> None:
    """Test that get_pyproject_version matches the version in pyproject.toml."""
    with Path("pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)

    expected_version = pyproject["project"]["version"]

    assert version_module.get_pyproject_version() == expected_version


def test_get_git_hash_returns_hex_or_unknown() -> None:
    """Test that get_git_hash returns a valid git hash or 'unknown'."""
    git_hash = version_module.get_git_hash()

    assert isinstance(git_hash, str)
    if git_hash != "unknown":
        assert len(git_hash) in {7, 40}
        assert all(char in string.hexdigits for char in git_hash)


def test_get_git_hash_returns_unknown_when_git_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git helper should return 'unknown' when .git is missing."""
    monkeypatch.setattr(version_module, "src_file", None)

    assert version_module.get_git_hash() == "unknown"
