import uuid
from fastapi_users import schemas
from pydantic import Field, field_validator
import re


class UserRead(schemas.BaseUser[uuid.UUID]):
    username: str


class UserCreate(schemas.BaseUserCreate):
    username: str = Field(..., min_length=3, max_length=50)

    @field_validator("username")
    def username_alphanumeric(cls, v):
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError(
                "Username must contain only letters, numbers, and underscores"
            )
        return v


class UserUpdate(schemas.BaseUserUpdate):
    username: str | None = None
