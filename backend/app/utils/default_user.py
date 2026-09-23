"""
Fallback "default" user for requests that don't specify a real user_id
(e.g. before the user has filled out their profile). Always resolves to
a real row so foreign-key-constrained inserts (reports, disputes) never
fail, instead of minting an orphan UUID that references nothing.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.user import User

DEFAULT_USER_EMAIL = "local-user@credit-sniper.local"


async def get_or_create_default_user(db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.email == DEFAULT_USER_EMAIL))
    user = result.scalar_one_or_none()
    if user:
        return user

    user = User(email=DEFAULT_USER_EMAIL, full_name="[YOUR FULL NAME]")
    db.add(user)
    await db.flush()
    return user
