"""Path resolution helpers for AniBridge."""

from pathlib import Path

from anibridge.app import __file__ as src_file

__all__ = ["PROJECT_ROOT", "find_project_root"]


def find_project_root(anchor: Path, marker: str = "pyproject.toml") -> Path | None:
    """Find the repository root by walking parents until marker is found.

    Args:
        anchor (Path): Starting path (file or directory)
        marker (str): File or directory name expected at the project root

    Returns:
        Path | None: Path to root containing marker, or None if not found
    """
    current = anchor if anchor.is_dir() else anchor.parent

    for candidate in (current, *current.parents):
        if (candidate / marker).exists():
            return candidate

    return None


PROJECT_ROOT = (
    find_project_root(Path(src_file).resolve()) or Path(src_file).resolve().parents[3]
)
