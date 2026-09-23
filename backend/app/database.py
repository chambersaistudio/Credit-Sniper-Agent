import asyncio
import os
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from app.config import settings

# Tests use NullPool, since each test runs on its own event loop. The API
# itself runs as a long-lived server and keeps a normal connection pool.
_pool_kwargs = {"poolclass": NullPool} if os.getenv("DB_NULL_POOL") else {"pool_pre_ping": True}

engine = create_async_engine(settings.database_url, echo=settings.debug, **_pool_kwargs)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _upgrade_to_head() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")


async def run_migrations() -> None:
    """Apply pending schema migrations. Alembic's env drives its own event
    loop, so it runs in a worker thread rather than on the app's loop."""
    await asyncio.to_thread(_upgrade_to_head)
