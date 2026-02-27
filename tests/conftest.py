"""Shared pytest configuration and fixtures for the test suite."""

import atexit
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="ab-tests-"))
os.environ["AB_DATA_PATH"] = str(_TEST_DATA_DIR)
_TEST_CONFIG_FILE = _TEST_DATA_DIR / "config.yaml"

_TEST_CONFIG_FILE.write_text(
    yaml.safe_dump(
        {
            "providers": {
                "anilist": {"token": "anilist-token"},
                "plex": {
                    "token": "plex-token",
                    "user": "eliasbenb",
                    "url": "http://plex:32400",
                },
            },
        },
        sort_keys=False,
    ),
    encoding="utf-8",
)

from anibridge.utils.limiter import Limiter  # noqa: E402

from anibridge.app.config import settings as settings_module  # noqa: E402
from anibridge.app.config.database import db as db_factory  # noqa: E402
from anibridge.app.utils import logging as logging_module  # noqa: E402
from anibridge.app.web.state import get_app_state  # noqa: E402

settings_module.get_config.cache_clear()
logging_module.get_logger.cache_clear()
db_factory.cache_clear()

src_module = sys.modules.get("anibridge.app")
if src_module is None:
    src_module = importlib.import_module("anibridge.app")

src_module.config = settings_module.get_config()  # type: ignore
src_module.log = logging_module.get_logger()  # type: ignore

Limiter.DISABLED = True


def pytest_sessionstart() -> None:
    """Fail fast if tests are configured to use the real data directory."""
    data_path = Path(os.getenv("AB_DATA_PATH", "./data")).resolve()
    repo_data_path = (Path(__file__).resolve().parents[1] / "data").resolve()
    if data_path == repo_data_path or data_path == Path("./data").resolve():
        raise RuntimeError(
            "Refusing to run tests against the real data directory. "
            "Set AB_DATA_PATH to a temporary location."
        )


@pytest.fixture(autouse=True)
def _reset_app_state():
    """Ensure each test interacts with a fresh AppState instance."""
    get_app_state.cache_clear()
    state = get_app_state()
    yield state
    get_app_state.cache_clear()


@atexit.register
def _cleanup_test_data_dir() -> None:
    """Remove the temporary test data directory after the test session."""
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
