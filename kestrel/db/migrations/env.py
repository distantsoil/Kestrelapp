"""Alembic environment. No __file__ arithmetic (SPEC-INTERFACES.md 1.4): script_location and the
sync sqlite:// URL are supplied programmatically by kestrel.db.migrate. target_metadata comes from
importing the models so every table is registered on Base.metadata."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, event, pool

import kestrel.db.models  # noqa: F401  (registers all tables on Base.metadata)
from kestrel.db.base import Base
from kestrel.db.engine import _apply_pragmas

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite needs batch mode for ALTER
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # Set WAL/busy_timeout at CONNECT time (pre-transaction). Running these as statements on an
    # open connection begins an implicit transaction that breaks Alembic's version stamping.
    event.listen(connectable, "connect", _apply_pragmas)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
