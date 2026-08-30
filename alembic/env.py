from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from alphaforge.db.base import Base
from alphaforge.models import schema  # noqa: F401
from alphaforge.database_defaults import resolve_alembic_database_url, sqlite_path_from_url
import os

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
declared_url = config.get_main_option("sqlalchemy.url")
database_url = resolve_alembic_database_url(declared_url, os.environ)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
database_path = sqlite_path_from_url(database_url)
if database_path is not None:
    database_path.parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)

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
