import uuid

from fastapi_users import FastAPIUsers
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
import os
from dotenv import load_dotenv

from user_manager import get_user_manager
from user_db import User

load_dotenv()

SECRET = os.getenv("SECRET_KEY", "your-secret-key-here-change-this")
TOKEN_LIFETIME = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30")) * 60  # seconds

bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=SECRET, lifetime_seconds=TOKEN_LIFETIME)


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

current_active_user = fastapi_users.current_user(active=True)
