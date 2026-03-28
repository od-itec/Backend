"""REST endpoints for workspace session lifecycle (start/stop/sync pods)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from auth_config import current_active_user
from file_repository import FileRepository
from k8s_service import (
    delete_all_user_pods,
    ensure_namespace,
    ensure_pod_ready,
    get_user_pod,
    sync_files_from_pod,
    sync_files_to_pod,
)
from user_db import User

router = APIRouter(prefix="/workspace", tags=["workspace"])


def _repo() -> FileRepository:
    return FileRepository()


@router.post("/start")
def start_workspace(
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(_repo),
):
    """Create pods for the user and sync files from DB into them."""
    ensure_namespace()

    results = {}
    files = repo.get_all_files(user.id)

    for pod_type in ("frontend", "backend"):
        pod = ensure_pod_ready(user.id, pod_type)
        sync_msg = sync_files_to_pod(user.id, pod_type, files)
        results[pod_type] = {
            "pod": pod.metadata.name,
            "phase": pod.status.phase,
            "sync": sync_msg,
        }

    return {"status": "ok", "pods": results}


@router.post("/stop")
def stop_workspace(user: User = Depends(current_active_user)):
    """Tear down all pods for the user."""
    delete_all_user_pods(user.id)
    return {"status": "ok"}


@router.get("/status")
def workspace_status(user: User = Depends(current_active_user)):
    """Return current pod status for the user."""
    pods = {}
    for pod_type in ("frontend", "backend"):
        pod = get_user_pod(user.id, pod_type)
        if pod:
            pods[pod_type] = {
                "pod": pod.metadata.name,
                "phase": pod.status.phase,
            }
        else:
            pods[pod_type] = None
    return {"pods": pods}


@router.post("/sync")
def sync_workspace(
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(_repo),
):
    """Bidirectional sync: pull files from pods back to DB, then push DB to pods.

    The "pull-then-push" order means the pod's filesystem is the source of truth
    when this endpoint is called (e.g. after running ``npm install`` in the terminal).
    """
    results = {}
    for pod_type in ("frontend", "backend"):
        pod = get_user_pod(user.id, pod_type)
        if not pod or pod.status.phase != "Running":
            results[pod_type] = "pod not running"
            continue

        # Pull from pod → DB
        pod_files = sync_files_from_pod(user.id, pod_type)
        if pod_files:
            repo.bulk_upsert(user.id, pod_files)

        results[pod_type] = f"synced {len(pod_files)} items"

    return {"status": "ok", "sync": results}
