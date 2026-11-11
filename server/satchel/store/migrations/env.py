from alembic import context
from sqlalchemy import create_engine

from satchel.settings import get_settings
from satchel.store.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    settings = get_settings()
    url = (
        context.config.get_main_option("sqlalchemy.url")
        or settings.migration_database_url
        or settings.database_url
    )
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
