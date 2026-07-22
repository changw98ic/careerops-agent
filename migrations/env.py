from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from careerops.config import Settings
from careerops.infrastructure.database.schema import DATABASE_SCHEMA, metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url = config.attributes.get("database_url")
if not isinstance(database_url, str):
    database_url = Settings().database_url.get_secret_value()
config.set_main_option(
    "sqlalchemy.url",
    database_url.replace("%", "%%"),
)
target_metadata = metadata


def include_object(object_, _name, type_, reflected, _compare_to):
    if type_ == "table" and reflected:
        return object_.schema == DATABASE_SCHEMA
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"options": "-csearch_path=public"},
    )

    with connectable.connect() as connection:
        # A role named ``careerops`` implicitly puts the same-named schema on its
        # search path. The connect option above applies before SQLAlchemy detects
        # the default schema, keeping reflection independent of the login role.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
