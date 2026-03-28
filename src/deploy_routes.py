"""
Deploy routes — build images with Cloud Native Buildpacks and
run / stop / inspect user containers inside DinD.
"""

import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel, Field

from auth_config import current_active_user, SECRET
from dind_manager import dind_manager
from user_db import User
from fastapi_users.jwt import decode_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/deploy", tags=["deploy"])

# Simple allowlist for identifiers used in shell commands
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9._/-]+$")


def _validate_name(value: str, label: str = "name"):
    if not _SAFE_NAME.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value!r}")


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------

class BuildRequest(BaseModel):
    project_path: str = Field(..., description="Path inside /workspace, e.g. '/workspace/frontend'")
    image_name: str = Field(..., description="Image tag, e.g. 'my-frontend'")


class RunRequest(BaseModel):
    image_name: str
    container_name: str
    port: int = Field(8080, description="Port the app listens on inside the container")
    env: dict[str, str] = Field(default_factory=dict)


class StopRequest(BaseModel):
    container_name: str


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@router.post("/build")
async def build_project(
    body: BuildRequest,
    user: User = Depends(current_active_user),
):
    """Trigger a buildpack build inside the user's DinD.

    Returns immediately with a build_id.  Stream output via the
    /ws/deploy/build WebSocket endpoint.
    """
    user_id = str(user.id)
    _validate_name(body.image_name, "image_name")
    _validate_name(body.project_path, "project_path")

    session = dind_manager.get_session(user_id)
    if session is None:
        raise HTTPException(status_code=400, detail="No active DinD session. Sync workspace first.")

    # Kick off build in background — return immediately
    # Output will be streamed via the WebSocket below
    return {
        "status": "accepted",
        "image_name": body.image_name,
        "project_path": body.project_path,
    }


async def _authenticate_ws(token: str) -> Optional[str]:
    try:
        payload = decode_jwt(token, SECRET, audience=["fastapi-users:auth"])
        return payload.get("sub")
    except Exception:
        return None


@router.websocket("/ws/deploy/build")
async def build_stream_ws(
    websocket: WebSocket,
    token: str = Query(...),
    project_path: str = Query(...),
    image_name: str = Query(...),
):
    """Stream buildpack build output over WebSocket."""
    user_id = await _authenticate_ws(token)
    if user_id is None:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    if not _SAFE_NAME.match(image_name) or not _SAFE_NAME.match(project_path):
        await websocket.close(code=4002, reason="Invalid parameters")
        return

    await websocket.accept()

    try:
        exec_id, stream = await dind_manager.build_with_pack(
            user_id, project_path, image_name
        )

        for chunk in stream:
            if isinstance(chunk, bytes):
                await websocket.send_text(chunk.decode("utf-8", errors="replace"))
            else:
                await websocket.send_text(str(chunk))

        # Check final exit code
        inspect = dind_manager.client.api.exec_inspect(exec_id)
        exit_code = inspect.get("ExitCode", -1)
        await websocket.send_text(f"\n--- Build finished (exit code {exit_code}) ---\n")

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("Build stream error for user %s: %s", user_id, exc)
        try:
            await websocket.send_text(f"\n--- Build error: {exc} ---\n")
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/run")
async def run_container(
    body: RunRequest,
    user: User = Depends(current_active_user),
):
    """Run a previously built image inside the user's DinD."""
    user_id = str(user.id)
    _validate_name(body.image_name, "image_name")
    _validate_name(body.container_name, "container_name")

    try:
        result = await dind_manager.run_app_container(
            user_id,
            image_name=body.image_name,
            container_name=body.container_name,
            port=body.port,
            env=body.env,
        )
        return {"status": "running", **result}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/stop")
async def stop_container(
    body: StopRequest,
    user: User = Depends(current_active_user),
):
    """Stop and remove an app container inside DinD."""
    user_id = str(user.id)
    _validate_name(body.container_name, "container_name")

    try:
        await dind_manager.stop_app_container(user_id, body.container_name)
        return {"status": "stopped", "container_name": body.container_name}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/status")
async def deploy_status(
    user: User = Depends(current_active_user),
):
    """Return status of all app containers for the current user."""
    user_id = str(user.id)
    containers = await dind_manager.get_app_status(user_id)
    return {"containers": containers}


@router.get("/logs/{container_name}")
async def container_logs(
    container_name: str,
    user: User = Depends(current_active_user),
    tail: int = 200,
):
    """Get recent logs from a user's app container."""
    user_id = str(user.id)
    _validate_name(container_name, "container_name")

    try:
        logs = await dind_manager.get_app_logs(user_id, container_name, tail=tail)
        return {"container_name": container_name, "logs": logs}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
