"""Alembic environment script for managing database migrations."""

import pathlib
import sys
from logging.config import fileConfig

from sqlalchemy.engine import create_engine

from alembic import context

import anibridge.app.models.db
from anibridge.app.config.settings import get_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = anibridge.app.models.db.Base.metadata

db_url = f"sqlite:///{get_config().data_path / 'anibridge.db'}"
config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = create_engine(
        db_url,
        echo=False,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
