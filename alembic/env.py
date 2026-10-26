from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Side-effect import: registers every model on Base.metadata so autogenerate
# (and this env's target_metadata) sees the full schema, not just whichever
# model happened to be imported first.
import app.models  # noqa: F401
from app.core.config import settings
from app.db.base import Base

config = context.config

# Only fall back to settings.DATABASE_URL when no URL was supplied by the
# caller. The test harness builds a Config and sets sqlalchemy.url to the
# disposable test database before calling command.upgrade(); overwriting it
# here unconditionally would silently run every test migration against the
# dev database instead.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    # disable_existing_loggers=False is NOT cosmetic. fileConfig defaults
    # to True, which DISABLES every logger that already exists -- and
    # when Alembic is driven in-process (the test harness does exactly
    # this, and so does any "migrate on startup" entrypoint), every
    # app.* logger has already been created by the imports above. The
    # result is an application that silently stops logging the moment it
    # runs a migration, which is the worst possible time to go quiet.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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
