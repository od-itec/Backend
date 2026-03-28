"""REST + WebSocket endpoints for buildpack build & deploy."""

import asyncio
import logging

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from auth_config import current_active_user
from deploy_service import (
    delete_deployment,
    deploy_image,
    get_build_logs,
    get_build_status,
    get_deploy_status,
    start_build,
)
from terminal_routes import _authenticate_ws
from user_db import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/deploy", tags=["deploy"])


class DeployRequest(BaseModel):
    pod_type: str = "frontend"


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------
@router.post("/build")
def trigger_build(
    body: DeployRequest,
    user: User = Depends(current_active_user),
):
    """Kick off a buildpack build job for the user's workspace."""
    job_name = start_build(user.id, body.pod_type)
    return {"status": "building", "job": job_name}


@router.get("/build/status")
def build_status(
    pod_type: str = "frontend",
    user: User = Depends(current_active_user),
):
    return get_build_status(user.id, pod_type)


@router.get("/build/logs")
def build_logs(
    pod_type: str = "frontend",
    user: User = Depends(current_active_user),
):
    logs = get_build_logs(user.id, pod_type)
    return {"logs": logs}


@router.post("/run")
def run_deployment(
    body: DeployRequest,
    user: User = Depends(current_active_user),
):
    """Deploy the built image as a K8s Deployment + Service."""
    result = deploy_image(user.id, body.pod_type)
    return {"status": "deployed", **result}


@router.get("/status")
def deploy_status_endpoint(
    pod_type: str = "frontend",
    user: User = Depends(current_active_user),
):
    result = get_deploy_status(user.id, pod_type)
    if result is None:
        return {"status": "not_deployed"}
    return {"status": "deployed", **result}


@router.delete("")
def remove_deployment(
    pod_type: str = "frontend",
    user: User = Depends(current_active_user),
):
    deleted = delete_deployment(user.id, pod_type)
    return {"status": "deleted" if deleted else "not_found"}


# ---------------------------------------------------------------------------
# WebSocket: stream build logs in real-time
# ---------------------------------------------------------------------------
@router.websocket("/ws/build-logs")
async def build_logs_ws(
    ws: WebSocket,
    token: str = Query(...),
    pod_type: str = Query("frontend"),
):
    """Stream build job logs to the browser in real-time."""
    try:
        user_id = _authenticate_ws(token)
    except ValueError:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()

    from uuid import UUID

    uid = UUID(user_id)
    sent_len = 0

    try:
        while True:
            status = get_build_status(uid, pod_type)
            logs = get_build_logs(uid, pod_type)

            # Send only new log lines
            if len(logs) > sent_len:
                await ws.send_text(logs[sent_len:])
                sent_len = len(logs)

            if status["status"] in ("succeeded", "failed", "not_found"):
                await ws.send_text(f"\n--- Build {status['status']} ---\n")
                break

            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("Build log stream error")
    finally:
        try:
            await ws.close()
        except Exception:
            pass
