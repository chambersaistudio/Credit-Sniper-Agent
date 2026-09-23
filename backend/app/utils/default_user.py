"""
The single local user for AUTH_MODE=disabled (local development and tests).

In hosted `jwt` mode this user is never used: identity comes only from the
verified token (see app/auth.py), and no request may name a user id. This
module exists so that with auth off, foreign-key-constrained inserts still
reference a real user row.
"""
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

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
