import uuid
from typing import Optional

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, UUIDIDMixin
import os
from dotenv import load_dotenv

from user_db import User, get_user_db

load_dotenv()

SECRET = os.getenv("SECRET_KEY", "your-secret-key-here-change-this")


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        print(f"User {user.id} ({user.email}) has registered.")


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)
