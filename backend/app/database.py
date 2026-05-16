import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from app.config import settings

# Vercel serverless functions don't persist between requests, so connection
# pooling causes stale-connection errors. NullPool opens/closes per request
# and works correctly with Neon/Vercel Postgres's external pgbouncer pooler.
_pool_kwargs = {"poolclass": NullPool} if os.getenv("VERCEL") else {}

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


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
