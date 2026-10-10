from alembic import context

from eddn_helper import config as app_config
from eddn_helper import database
from eddn_helper.models import Base

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    return True


def _configure(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        transactional_ddl=True,
        include_object=include_object,
    )


def run_migrations_online():
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = app_config.database_url(config.get_main_option("sqlalchemy.url") or None)
    engine = database.make_engine(url)
    with engine.connect() as connection:
        connection = connection.execution_options(sqlite_begin_immediate=True)
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


run_migrations_online()
