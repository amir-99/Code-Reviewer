import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from reviewer.config.schema import Settings
from reviewer.store.models import Base


def configure(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(Settings().database_url.get_secret_value())
    async with engine.connect() as connection:
        await connection.run_sync(configure)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url.get_secret_value(),
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
