from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auth_config import auth_backend, current_active_user, fastapi_users
from schemas import UserCreate, UserRead, UserUpdate
from user_db import User, create_db_and_tables
from db import init_database
from file_routes import router as file_router
from workspace_routes import router as workspace_router
from terminal_routes import router as terminal_router
from deploy_routes import router as deploy_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_db_and_tables()
    init_database()

    # Start DinD idle-cleanup background task
    from dind_manager import dind_manager
    await dind_manager.start_cleanup_loop()

    yield

    # Shutdown: stop all DinD containers
    await dind_manager.shutdown()


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
        "http://localhost:5174",
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
app.include_router(workspace_router)
app.include_router(terminal_router)
app.include_router(deploy_router)


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
