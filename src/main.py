from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auth_config import auth_backend, current_active_user, fastapi_users
from schemas import UserCreate, UserRead, UserUpdate
from user_db import User, create_db_and_tables
from db import init_database
from file_routes import router as file_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_db_and_tables()
    init_database()
    yield


app = FastAPI(
    title="ITEC Auth API",
    description="Authentication API powered by fastapi-users",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://itecify.onlinedi.vision",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Auth routers ---
app.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/auth/jwt",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix="/users",
    tags=["users"],
)
app.include_router(file_router)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "message": "ITEC API is running"}


@app.get("/protected")
async def protected_route(user: User = Depends(current_active_user)):
    return {
        "message": f"Hello {user.username}, this is a protected route!",
        "user": user.email,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
