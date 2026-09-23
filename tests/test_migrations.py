from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import app.models  # noqa: F401
from app.database import Base, engine
from tests.conftest import requires_db

pytestmark = requires_db


async def test_migrations_match_models(db_ready):
    """Fails when a model changes without a migration — `create_all` used to
    hide this, and new columns never reached deployed databases."""
    def diff(connection):
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []
