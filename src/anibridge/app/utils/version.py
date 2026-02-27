"""Version Utilities Module."""

import importlib.metadata
from pathlib import Path

from anibridge.app.utils.paths import PROJECT_ROOT

__all__ = ["get_docker_status", "get_git_hash", "get_pyproject_version"]


def get_pyproject_version() -> str:
    """Get the AniBridge's version from the pyproject.toml file.

    Returns:
        str: AniBridge's version
    """
    try:
        return importlib.metadata.version("anibridge")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def get_git_hash() -> str:
    """Get the git commit hash of the AniBridge repository.

    Returns:
        str: AniBridge's current commit hash
    """
    try:
        if PROJECT_ROOT is None:
            return "unknown"

        git_dir_path = PROJECT_ROOT / ".git"
        if not git_dir_path.exists() or not git_dir_path.is_dir():
            return "unknown"

        with open(git_dir_path / "HEAD") as f:
            head_content = f.read().strip()

        # HEAD is directly pointing to a commit
        if not head_content.startswith("ref:"):
            return head_content

        ref_path = head_content.split("ref: ")[1]

        # HEAD is pointing to a branch
        full_ref_path = git_dir_path / ref_path
        if full_ref_path.exists() and full_ref_path.is_file():
            with open(full_ref_path) as f:
                return f.read().strip()

        # HEAD is pointing to reference in packed-refs
        packed_refs_path = git_dir_path / "packed-refs"
        if packed_refs_path.exists() and packed_refs_path.is_file():
            with open(packed_refs_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and line.endswith(ref_path):
                        return line.split()[0]

        return "unknown"
    except Exception:
        return "unknown"


def get_docker_status() -> bool:
    """Check if AniBridge is running inside a Docker container.

    Returns:
        bool: True if running inside a Docker container, False otherwise
    """
    dockerenv_path = Path("/.dockerenv")
    return dockerenv_path.exists() and dockerenv_path.is_file()
