"""Alembic environment for the Phase -1 spike.

No __file__ arithmetic here (SPEC-INTERFACES.md 1.4): script_location and the sync sqlite:// URL
are supplied programmatically by shell_spike.py via alembic.config.Config.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
target_metadata = None  # migrations use explicit op.* calls, not autogenerate


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
