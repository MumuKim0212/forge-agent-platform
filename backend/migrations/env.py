from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.db_url import normalise_database_url
from app.models.base import Base
from app.schema_check import alembic_option_value
from app.models import (  # noqa: F401  (registers all models on Base.metadata)
    approval,
    audit,
    build,
    build_trace,
    change_request,
    deployment,
    engine_selection,
    eval_run,
    policy_verdict,
    project,
    template,
    user,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# an explicitly supplied URL (a test's temp file, or an operator
# driving `alembic.command` with their own Config) wins; alembic.ini keeps its
# empty value, so the default is still the platform's own DATABASE_URL.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", alembic_option_value(settings.database_url))

# the same driver normalisation `app/db.py` applies, to whichever URL won
# above (an explicit one included: a bare `postgresql://` fails here for the same reason).
config.set_main_option(
    "sqlalchemy.url", alembic_option_value(normalise_database_url(config.get_main_option("sqlalchemy.url")))
)

target_metadata = Base.metadata


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
