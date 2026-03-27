from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime

class User(BaseModel):
    username: str
    email: EmailStr
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.now)

class UserInDB(User):
    hashed_password: str
