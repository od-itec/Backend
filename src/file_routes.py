from uuid import UUID

from fastapi import APIRouter, Depends

from auth_config import current_active_user
from file_repository import FileRepository
from file_schemas import FileItem, FileUpdate, SaveTreeRequest
from user_db import User

router = APIRouter(prefix="/files", tags=["files"])


def get_file_repo() -> FileRepository:
    return FileRepository()


@router.get("", response_model=list[FileItem])
def load_files(
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(get_file_repo),
):
    rows = repo.get_all_files(user.id)
    return [
        FileItem(
            file_id=row["file_id"],
            parent_id=row.get("parent_id"),
            type=row["type"],
            name=row["name"],
            content=row.get("content", ""),
            language=row.get("language", ""),
            is_expanded=row.get("is_expanded", False),
            position=row.get("position", 0),
        )
        for row in rows
    ]


@router.put("")
def save_tree(
    body: SaveTreeRequest,
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(get_file_repo),
):
    repo.bulk_upsert(user.id, [item.model_dump() for item in body.items])
    return {"status": "ok", "count": len(body.items)}


@router.post("", response_model=FileItem, status_code=201)
def create_file(
    item: FileItem,
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(get_file_repo),
):
    repo.upsert_file(
        user_id=user.id,
        file_id=item.file_id,
        parent_id=item.parent_id,
        type=item.type,
        name=item.name,
        content=item.content,
        language=item.language,
        is_expanded=item.is_expanded,
        position=item.position,
    )
    return item


@router.patch("/{file_id}")
def update_file(
    file_id: UUID,
    body: FileUpdate,
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(get_file_repo),
):
    updates = body.model_dump(exclude_none=True)
    repo.update_file(user.id, file_id, **updates)
    return {"status": "ok"}


@router.delete("/{file_id}", status_code=204)
def delete_file(
    file_id: UUID,
    user: User = Depends(current_active_user),
    repo: FileRepository = Depends(get_file_repo),
):
    repo.delete_file(user.id, file_id)
