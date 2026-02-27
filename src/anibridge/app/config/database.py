"""Database Configuration for AniBridge."""

from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

import sqlalchemy.event
from anibridge.utils.cache import cache
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker

from anibridge.app import __file__ as src_file
from anibridge.app import config, log
from anibridge.app.exceptions import DataPathError
from anibridge.app.utils.paths import find_project_root

__all__ = ["AniBridgeDB", "db"]


if TYPE_CHECKING:
    from sqlalchemy.connectors.aioodbc import AsyncAdapt_aioodbc_connection
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session


class AniBridgeDB:
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

        Args:
            data_path (Path): Directory where the database should be stored

        Raises:
            PermissionError: If the process lacks write permissions for data_path
            ValueError: If data_path exists but is a file instead of a directory
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
        self._session: Session | None = None
        self._do_migrations()

    def _setup_db(self) -> Engine:
        """Creates and initializes the SQLite database.

        Returns:
            Engine: Configured SQLAlchemy engine instance

        Raises:
            PermissionError: If unable to create the data directory
            ValueError: If data_path exists but is a file instead of a directory
        """
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
        def _set_sqlite_pragma(dbapi_connection: AsyncAdapt_aioodbc_connection, _):
            """Set SQLite PRAGMA settings on new connections."""
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA synchronous=NORMAL;")
                cur.execute("PRAGMA temp_store=MEMORY;")
                cur.execute("PRAGMA cache_size=-20000;")
                cur.execute("PRAGMA foreign_keys=ON;")
            finally:
                cur.close()

        return engine

    def _do_migrations(self) -> None:
        """Executes database migrations using Alembic.

        Configures Alembic to use the SQLite database and runs all pending
        migrations to bring the schema up to the latest version.

        Raises:
            AlembicError: If migration execution fails
            FileNotFoundError: If Alembic migration scripts are not found
        """
        from alembic.config import Config

        from alembic import command

        log.debug("Running database migrations")

        if src_file is None:
            log.error("Cannot determine source file path for Alembic configuration")
            raise FileNotFoundError("Source file path is undefined")

        project_root = find_project_root(Path(src_file).resolve())
        if project_root is None:
            log.error("Cannot determine project root for Alembic configuration")
            raise FileNotFoundError("Project root could not be determined")

        cfg = Config()
        cfg.set_main_option("script_location", str(project_root / "alembic"))
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

    def __enter__(self) -> AniBridgeDB:
        """Enters the context manager, returning the database instance."""
        self._session = self._SessionLocal()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the session opened for this context, if any."""
        if self._session is not None:
            self._session.close()
            self._session = None

    @property
    def session(self) -> Session:
        """Return the current SQLAlchemy session, creating it if needed."""
        if self._session is None:
            self._session = self._SessionLocal()
        return self._session


@cache
def db() -> AniBridgeDB:
    """Get the singleton instance of the AniBridgeDB.

    Uses LRU caching to ensure only one instance is created and reused.

    Returns:
        AniBridgeDB: The singleton database manager instance
    """
    return AniBridgeDB(config.data_path)
