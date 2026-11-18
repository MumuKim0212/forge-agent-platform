# Not protected -- see alembic.ini's header.
#
# Reads its OWN injected DATABASE_URL via `db.resolve_database_url()` --
# never the platform's own database setting (that belongs to a different
# store entirely: ADR 0023 Amendment 7 tabulates all three stores and
# says plainly they must never be merged). Nothing in this file imports
# from the platform's own `app` package -- `db`
# here is this template's own module, made importable by `prepend_sys_path
# = .` in alembic.ini (mirroring the platform's own migrations/env.py,
# which does the same for its own config module).
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from db import resolve_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic's Config is a configparser: a lone % in the value (a percent-encoded
# password) raises "invalid interpolation syntax in '<the whole URL>'" and prints
# the URL. Doubling it hands the original back (the platform's own
# migrations env.py escapes its URL the same way).
config.set_main_option("sqlalchemy.url", resolve_database_url().replace("%", "%%"))

# No ORM models are wired into Alembic's autogenerate for this seed -- the
# one table the shipped initial migration creates is written by hand
# (shape, not product). An agent extending this service with its
# own models is free to point target_metadata at them; until then this
# stays None so `alembic revision --autogenerate` never silently invents a
# migration nobody asked for.
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

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
