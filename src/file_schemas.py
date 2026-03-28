from pydantic import BaseModel
from typing import Optional
from uuid import UUID


class FileItem(BaseModel):
    file_id: UUID
    parent_id: Optional[UUID] = None
    type: str  # "file" or "folder"
    name: str
    content: str = ""
    language: str = ""
    is_expanded: bool = False
    position: int = 0


class SaveTreeRequest(BaseModel):
    items: list[FileItem]


class FileUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    language: Optional[str] = None
    is_expanded: Optional[bool] = None
    position: Optional[int] = None
    parent_id: Optional[UUID] = None
