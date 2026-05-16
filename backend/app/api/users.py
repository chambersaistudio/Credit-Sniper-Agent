"""
User profile management — personal info used to personalize dispute letters.
"""
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.user import User

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreate(BaseModel):
    email: str
    full_name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    ssn_last_four: str | None = None
    date_of_birth: str | None = None
    phone: str | None = None


class UserUpdate(BaseModel):
    full_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    ssn_last_four: str | None = None
    date_of_birth: str | None = None
    phone: str | None = None
    autonomy_level: str | None = None


@router.post("/", response_model=dict[str, Any])
async def create_user(request: UserCreate, db: AsyncSession = Depends(get_db)):
    """Create a user profile for dispute letter personalization."""
    existing = await db.execute(select(User).where(User.email == request.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="User with this email already exists")

    user = User(**request.model_dump())
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _format_user(user)


@router.get("/{user_id}", response_model=dict[str, Any])
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _format_user(user)


@router.patch("/{user_id}", response_model=dict[str, Any])
async def update_user(user_id: str, request: UserUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    for field, value in request.model_dump(exclude_none=True).items():
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    return _format_user(user)


def _format_user(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "address": user.address,
        "city": user.city,
        "state": user.state,
        "zip_code": user.zip_code,
        "ssn_last_four": user.ssn_last_four,
        "date_of_birth": user.date_of_birth,
        "phone": user.phone,
        "autonomy_level": user.autonomy_level,
        "created_at": str(user.created_at),
    }
