"""Database Configuration for AniBridge."""

from contextvars import ContextVar, Token
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

import sqlalchemy.event
from anibridge.utils.cache import cache
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker

from anibridge.app.config.settings import get_config
from anibridge.app.exceptions import DataPathError
from anibridge.app.logging import get_logger
from anibridge.app.utils.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from sqlalchemy.connectors.aioodbc import AsyncAdapt_aioodbc_connection
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

__all__ = ["AnibridgeDb", "db"]

log = get_logger(__name__)


class AnibridgeDb:
    """Database manager for AniBridge application.

    Handles the creation, initialization, and migration of the SQLite database,
    including file system operations and schema management. Uses SQLAlchemy for ORM
    and Alembic for database migrations.

    During initialization, this class automatically imports all database models
    and runs any pending migrations.

    Can be used as a context manager to automatically close the database session.
    """

    def __init__(self, data_path: Path) -> None:
        """Initializes the database manager.

        Performs database setup including directory creation, model registration,
        engine creation, session initialization, and migration execution.
        """
        self.data_path = data_path
        self.db_path = data_path / "anibridge.db"

        log.debug("Initializing database at $$'%s'$$", self.db_path)
        self.engine = self._setup_db()
        self._SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
        # Keep the engine/session factory singleton-scoped, but isolate the live
        # Session per request/task context so overlapping requests never share it.
        self._session_ctx: ContextVar[Session | None] = ContextVar(
            "anibridge_db_session",
            default=None,
        )
        self._session_tokens: ContextVar[tuple[Token[Session | None], ...]] = (
            ContextVar("anibridge_db_session_tokens", default=())
        )
        self._do_migrations()

    def _setup_db(self) -> Engine:
        """Creates and initializes the SQLite database."""
        if not self.data_path.exists():
            log.debug(
                "Creating data directory at $$'%s'$$",
                self.data_path,
            )
            self.data_path.mkdir(parents=True, exist_ok=True)
        elif self.data_path.is_file():
            log.error("Invalid data path $$'%s'$$ is a file", self.data_path)
            raise DataPathError(
                f"The path '{self.data_path}' is a file, "
                "please delete it first or choose a different data folder path"
            )

        engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
            future=True,
        )
        log.debug("SQLite engine created at $$'%s'$$", self.db_path)

        @sqlalchemy.event.listens_for(engine, "connect")
        def _set_sqlite_pragma(
            dbapi_connection: AsyncAdapt_aioodbc_connection,
            _: object,
        ) -> None:
            """Set SQLite PRAGMA settings on new connections."""
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA synchronous=NORMAL;")
                cur.execute("PRAGMA temp_store=MEMORY;")
                cur.execute("PRAGMA cache_size=-4000;")
                cur.execute("PRAGMA foreign_keys=ON;")
            finally:
                cur.close()

        return engine

    @staticmethod
    def _get_head_revision() -> str | None:
        """Determine the head migration revision without importing alembic.

        Parses migration filenames (`YYYY-MM-DD-HH-MM_<rev>.py`) to find the
        latest revision.
        """
        versions_dir = PROJECT_ROOT / "alembic" / "versions"
        if not versions_dir.is_dir():
            return None
        migration_files = sorted(
            f
            for f in versions_dir.glob("*.py")
            if "_" in f.stem and not f.stem.startswith("_")
        )
        if not migration_files:
            return None
        return migration_files[-1].stem.split("_", 1)[1]

    def _do_migrations(self) -> None:
        """Executes database migrations using Alembic.

        Configures Alembic to use the SQLite database and runs all pending
        migrations to bring the schema up to the latest version.
        """
        import sqlite3

        # The alembic import is ~12 MB and 100+ modules. We avoid it by only importing
        # if the current DB revision doesn't match the head revision (most cases).
        head_rev = self._get_head_revision()
        if head_rev is not None:
            try:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    row = conn.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone()
                    if row and row[0] == head_rev:
                        log.debug("Database migrations up-to-date")
                        from anibridge.app.models.db import Base

                        Base.metadata.create_all(self.engine)
                        return
                except sqlite3.OperationalError:
                    pass
                finally:
                    conn.close()
            except Exception:
                pass

        from alembic.config import Config

        from alembic import command

        log.debug("Running database migrations")

        cfg = Config()
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")

        try:
            command.upgrade(cfg, "head")
            log.debug("Database migrations up-to-date")
        except Exception as e:
            log.exception("Database migration failed: %s", e)
            raise

        # Ensure ORM metadata tables are present (Alembic is expected to manage
        # schema migrations for the active models).
        from anibridge.app.models.db import Base

        Base.metadata.create_all(self.engine)

    def __enter__(self) -> AnibridgeDb:
        """Enters the context manager, returning the database instance."""
        session = self._SessionLocal()
        token = self._session_ctx.set(session)
        tokens = self._session_tokens.get()
        self._session_tokens.set((*tokens, token))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the session opened for this context, if any."""
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

    def close(self) -> None:
        """Close the current session and dispose the SQLAlchemy engine."""
        self.__exit__(None, None, None)
        self.engine.dispose()

    @property
    def session(self) -> Session:
        """Return the current SQLAlchemy session, creating it if needed."""
        session = self._session_ctx.get()
        if session is None:
            session = self._SessionLocal()
            token = self._session_ctx.set(session)
            tokens = self._session_tokens.get()
            self._session_tokens.set((*tokens, token))
        return session


@cache
def db() -> AnibridgeDb:
    """Get the singleton instance of the AnibridgeDb.

    Uses LRU caching to ensure only one instance is created and reused.
    """
    return AnibridgeDb(get_config().data_path)
