"""Shared pytest configuration and fixtures for the test suite."""

import atexit
import importlib
import os
import shutil
import sys
import tempfile
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Protocol

import pytest
import yaml
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="ab-tests-"))
os.environ["AB_DATA_PATH"] = str(_TEST_DATA_DIR)
_TEST_CONFIG_FILE = _TEST_DATA_DIR / "config.yaml"

_TEST_CONFIG_FILE.write_text(
    yaml.safe_dump(
        {
            "profiles": {
                "default": {
                    "source_provider": "plex",
                    "target_provider": "anilist",
                    "source_provider_config": {
                        "plex": {
                            "token": "plex-token",
                            "user": "eliasbenb",
                            "url": "http://plex:32400",
                        }
                    },
                    "target_provider_config": {"anilist": {"token": "anilist-token"}},
                },
            },
        },
        sort_keys=False,
    ),
    encoding="utf-8",
)

from anibridge.utils.limiter import Limiter  # noqa: E402

import anibridge.app.logging as logging_module  # noqa: E402
from anibridge.app import initialize_runtime  # noqa: E402
from anibridge.app.config import settings as settings_module  # noqa: E402
from anibridge.app.config.database import db as db_factory  # noqa: E402
from anibridge.app.models.db.base import Base  # noqa: E402
from anibridge.app.web.state import get_app_state  # noqa: E402

settings_module.get_config.cache_clear()
logging_module.reset_logging()
db_factory.cache_clear()

src_module = sys.modules.get("anibridge.app")
if src_module is None:
    src_module = importlib.import_module("anibridge.app")

config = initialize_runtime()
logging_module.configure_logging(
    level=config.log_level,
    log_dir=config.data_path / "logs",
)

Limiter.DISABLED = True


class _SessionContextOwner(Protocol):
    _session_ctx: ContextVar[Session | None]


class _SQLiteTestDb:
    """Small SQLite-backed DB stub that mirrors AniBridgeDb session semantics."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._session_ctx: ContextVar[Session | None] = ContextVar(
            "test_db_session",
            default=None,
        )
        self._session_tokens: ContextVar[tuple[Token[Session | None], ...]] = (
            ContextVar("test_db_session_tokens", default=())
        )

    def __enter__(self):
        session = self._session_factory()
        token = self._session_ctx.set(session)
        tokens = self._session_tokens.get()
        self._session_tokens.set((*tokens, token))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        session = self._session_ctx.get()
        if session is not None:
            session.close()

        tokens = self._session_tokens.get()
        if not tokens:
            self._session_ctx.set(None)
            return

        token = tokens[-1]
        self._session_tokens.set(tokens[:-1])
        self._session_ctx.reset(token)

    @property
    def session(self):
        session = self._session_ctx.get()
        if session is None:
            session = self._session_factory()
            token = self._session_ctx.set(session)
            tokens = self._session_tokens.get()
            self._session_tokens.set((*tokens, token))
        return session

    def close(self) -> None:
        self.__exit__(None, None, None)


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


@pytest.fixture
def sqlite_db_factory():
    """Build SQLite-backed DB stubs that share one in-memory database per test."""
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
    resources: list[_SQLiteTestDb] = []

    def _factory() -> _SQLiteTestDb:
        db_instance = _SQLiteTestDb(session_factory)
        resources.append(db_instance)
        return db_instance

    yield _factory

    for db_instance in reversed(resources):
        db_instance.close()
    engine.dispose()


@pytest.fixture
def in_memory_db_factory(sqlite_db_factory):
    """Build disposable SQLite-backed DB stubs patched into target modules."""

    def _factory(
        monkeypatch: pytest.MonkeyPatch,
        *modules: object,
    ) -> _SessionContextOwner:
        db_instance = sqlite_db_factory()

        for module in modules:
            monkeypatch.setattr(module, "db", lambda: db_instance)

        return db_instance

    yield _factory


@atexit.register
def _cleanup_test_data_dir() -> None:
    """Remove the temporary test data directory after the test session."""
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
