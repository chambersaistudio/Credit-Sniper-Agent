"""
User resolution until real authentication exists. Requests without a
user_id (e.g. before a profile is saved) resolve to one real local user row,
so foreign-key-constrained inserts never reference a nonexistent user.
When auth lands, replace resolve_user_id with a dependency that reads the
authenticated principal — call sites won't change.
"""
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

DEFAULT_USER_SENTINEL = "default"
DEFAULT_USER_EMAIL = "local-user@credit-sniper.local"
# Fixed id, not an email lookup: the consumer can change their email in the
# profile without the next request creating a second, empty user.
LOCAL_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


async def get_or_create_default_user(db: AsyncSession) -> User:
    user = await db.get(User, LOCAL_USER_ID)
    if user:
        return user
    user = User(id=LOCAL_USER_ID, email=DEFAULT_USER_EMAIL, full_name="")
    db.add(user)
    await db.flush()
    return user


def parse_uuid(value: str, what: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {what}: {value!r}")


async def resolve_user_id(db: AsyncSession, user_id: str | None) -> uuid.UUID:
    if not user_id or user_id == DEFAULT_USER_SENTINEL:
        return (await get_or_create_default_user(db)).id
    resolved = parse_uuid(user_id, "user_id")
    if await db.get(User, resolved) is None:
        raise HTTPException(status_code=404, detail="User not found")
    return resolved
