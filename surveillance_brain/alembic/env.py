"""
alembic/env.py
==============
Alembic environment script.

Wires up:
    - The sync database URL from config.py (Alembic needs a sync engine)
    - SQLAlchemy metadata from db.models so autogenerate works

Note: embeddings live in Qdrant, NOT Postgres, so there is no longer a
pgvector extension to install here.
"""

from __future__ import annotations

import logging
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from db.models import Base  # noqa: E402

config_alembic = context.config

if config_alembic.config_file_name is not None:
    fileConfig(config_alembic.config_file_name)

target_metadata = Base.metadata

config_alembic.set_main_option("sqlalchemy.url", config.DATABASE_DSN_SYNC)

logger = logging.getLogger("alembic.env")


def run_migrations_offline() -> None:
    url = config_alembic.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config_alembic.get_section(config_alembic.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
