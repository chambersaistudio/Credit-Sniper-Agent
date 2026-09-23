"""
Consumer profile — the identity details a bureau or furnisher needs to
locate the consumer's file. `/me` is the single local user until real
authentication exists; the same handlers then read the signed-in user.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.database import get_db
from app.models.user import User
from app.utils.default_user import DEFAULT_USER_EMAIL

router = APIRouter(prefix="/api/users", tags=["users"])

AUTONOMY_LEVELS = {"approval_required", "semi_auto", "full_auto"}
REQUIRED_FOR_CORRESPONDENCE = ("full_name", "address", "city", "state", "zip_code")


class ProfileUpdate(BaseModel):
    email: str | None = None
    full_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    ssn_last_four: str | None = None
    date_of_birth: str | None = None
    phone: str | None = None
    autonomy_level: str | None = None

    @field_validator("ssn_last_four")
    @classmethod
    def _ssn_last_four(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not (len(value) == 4 and value.isdigit()):
            raise ValueError("Must be exactly the last 4 digits — never the full SSN")
        return value

    @field_validator("state")
    @classmethod
    def _state(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not (len(value) == 2 and value.isalpha()):
            raise ValueError("Use the 2-letter state code")
        return value.upper()

    @field_validator("autonomy_level")
    @classmethod
    def _autonomy(cls, value: str | None) -> str | None:
        if value is not None and value not in AUTONOMY_LEVELS:
            raise ValueError(f"Must be one of {sorted(AUTONOMY_LEVELS)}")
        return value


def missing_profile_fields(user: User) -> list[str]:
    return [name for name in REQUIRED_FOR_CORRESPONDENCE if not getattr(user, name)]


def _placeholder_email(email: str | None) -> bool:
    """The dev user's fixed email and the per-subject placeholder minted at
    first sign-in (when the token carried no email) shouldn't surface as the
    consumer's real address — the profile shows them as blank to fill in."""
    return not email or email == DEFAULT_USER_EMAIL or email.endswith("@auth.local")


def _format_user(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": "" if _placeholder_email(user.email) else user.email,
        "full_name": user.full_name,
        "address": user.address,
        "city": user.city,
        "state": user.state,
        "zip_code": user.zip_code,
        "ssn_last_four": user.ssn_last_four,
        "date_of_birth": user.date_of_birth,
        "phone": user.phone,
        "autonomy_level": user.autonomy_level,
        "missing_for_correspondence": missing_profile_fields(user),
    }


@router.get("/me", response_model=dict[str, Any])
async def get_me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await db.commit()  # persist the user row if it was just provisioned on first sign-in
    return _format_user(user)


@router.patch("/me", response_model=dict[str, Any])
async def update_me(
    request: ProfileUpdate, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    updates = request.model_dump(exclude_unset=True)
    if "email" in updates and not updates["email"]:
        raise HTTPException(status_code=422, detail="Email can't be blank")
    for name, value in updates.items():
        setattr(user, name, value)
    await db.commit()
    await db.refresh(user)
    return _format_user(user)
