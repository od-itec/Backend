"""
Workspace routes — sync files to DinD and detect buildable projects.
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth_config import current_active_user
from dind_manager import dind_manager
from file_repository import FileRepository
from user_db import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspace", tags=["workspace"])


def get_file_repo() -> FileRepository:
    return FileRepository()


# ------------------------------------------------------------------
# Helpers — resolve flat file table into path-based list
# ------------------------------------------------------------------

def _build_path_map(rows: list[dict]) -> list[dict]:
    """Convert flat DB rows (with parent_id) to a list of {path, content, type}."""
    by_id = {r["file_id"]: r for r in rows}

    def resolve_path(row: dict) -> str:
        parts = [row["name"]]
        current = row
        while current.get("parent_id") and current["parent_id"] in by_id:
            current = by_id[current["parent_id"]]
            parts.append(current["name"])
        parts.reverse()
        return "/".join(parts)

    result = []
    for row in rows:
        result.append({
            "path": resolve_path(row),
            "content": row.get("content", ""),
            "type": row.get("type", "file"),
        })
    return result


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@router.post("/sync")
async def sync_workspace(
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(get_file_repo),
):
    """Sync the user's files from DB into their DinD container at /workspace."""
    user_id = str(user.id)

    # Ensure DinD is up
    session = await dind_manager.ensure_dind(user_id)

    # Load files from ScyllaDB
    rows = repo.get_all_files(user.id)
    if not rows:
        return {"status": "ok", "synced": 0}

    path_items = _build_path_map(rows)

    # Sort so folders come before their children
    path_items.sort(key=lambda x: x["path"])

    await dind_manager.sync_files(user_id, path_items)

    return {"status": "ok", "synced": len(path_items)}


class DetectedProject(BaseModel):
    path: str
    type: str  # "node", "python", "static", "unknown"
    name: str


@router.get("/detect-projects", response_model=list[DetectedProject])
async def detect_projects(
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(get_file_repo),
):
    """Scan the user's workspace for buildable projects (package.json, requirements.txt, etc.)."""
    user_id = str(user.id)

    session = dind_manager.get_session(user_id)
    if session is None:
        raise HTTPException(status_code=400, detail="No active DinD session. Sync workspace first.")

    # Ask the DinD container to list marker files
    exit_code, output = await dind_manager.run_in_dind(
        user_id,
        "find /workspace -maxdepth 3 -name package.json -o -name requirements.txt -o -name Procfile -o -name go.mod",
    )

    if exit_code != 0:
        return []

    projects = []
    seen_dirs = set()
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Determine project dir
        parts = line.rsplit("/", 1)
        project_dir = parts[0] if len(parts) > 1 else "/workspace"
        if project_dir in seen_dirs:
            continue
        seen_dirs.add(project_dir)

        filename = parts[-1] if len(parts) > 1 else line
        project_type = "unknown"
        if filename == "package.json":
            project_type = "node"
        elif filename == "requirements.txt":
            project_type = "python"
        elif filename == "go.mod":
            project_type = "go"
        elif filename == "Procfile":
            project_type = "procfile"

        project_name = project_dir.split("/")[-1] or "workspace"
        projects.append(DetectedProject(
            path=project_dir,
            type=project_type,
            name=project_name,
        ))

    return projects
