"""Tests for cross-repository development commands."""

import subprocess
import sys
from pathlib import Path

import pytest

from anibridge.scripts import dev


def test_root_resolution_ignores_nested_package_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_path = tmp_path / "src/anibridge/scripts/dev.py"
    script_path.parent.mkdir(parents=True)
    script_path.touch()
    package_path = tmp_path / "packages/provider"
    package_path.mkdir(parents=True)
    (package_path / "pyproject.toml").touch()
    monkeypatch.chdir(package_path)

    assert dev._resolve_root_dir() == tmp_path


@pytest.mark.parametrize("target", [None, "backend", "frontend", "both"])
def test_test_command_runs_selected_suites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str | None
) -> None:
    package_path = tmp_path / "packages/provider"
    monkeypatch.setattr(dev, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["ab-test", *([target] if target else [])])
    monkeypatch.setattr(dev, "_iter_testable_package_dirs", lambda: [package_path])
    calls: list[tuple[list[str], Path]] = []

    def run(command: list[str], *, cwd: Path, check: bool) -> None:
        assert check
        calls.append((command, cwd))

    monkeypatch.setattr(dev.subprocess, "run", run)

    dev.test()

    expected = []
    if target in (None, "backend", "both"):
        expected.extend(
            ([sys.executable, "-m", "pytest"], project)
            for project in (tmp_path, package_path)
        )
    if target in (None, "frontend", "both"):
        expected.append((["pnpm", "test"], tmp_path / "frontend"))
    assert calls == expected


@pytest.mark.parametrize(
    ("command", "target", "expected"),
    [
        ("test", None, "both"),
        ("test", "both", "both"),
        ("test", "frontend", "frontend"),
        ("lint", None, "both"),
    ],
)
def test_main_forwards_targets_and_defaults_to_both(
    monkeypatch: pytest.MonkeyPatch, command: str, target: str | None, expected: str
) -> None:
    monkeypatch.setattr(sys, "argv", ["dev", command, *([target] if target else [])])
    targets: list[str] = []

    def capture_target() -> None:
        targets.append(dev.parse_target_args())

    monkeypatch.setattr(dev, command, capture_target)

    dev.main()

    assert targets == [expected]


def test_test_command_stops_after_a_suite_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dev, "parse_target_args", lambda **kwargs: "both")
    monkeypatch.setattr(dev, "_iter_testable_package_dirs", lambda: [])
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs) -> None:
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(dev.subprocess, "run", run)

    with pytest.raises(SystemExit) as error:
        dev.test()

    assert error.value.code == 1
    assert calls == [[sys.executable, "-m", "pytest"]]
