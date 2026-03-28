from typing import Dict, Any, List, Optional
from uuid import UUID
from datetime import datetime, timezone
import logging

from db import db

logger = logging.getLogger(__name__)


class FileRepository:
    """File data access layer for ScyllaDB"""

    def __init__(self):
        self.session = db.get_session()

    def get_all_files(self, user_id: UUID) -> List[Dict[str, Any]]:
        query = "SELECT * FROM files WHERE user_id = %s"
        rows = self.session.execute(query, (user_id,))
        return list(rows)

    def upsert_file(
        self,
        user_id: UUID,
        file_id: UUID,
        parent_id: Optional[UUID],
        type: str,
        name: str,
        content: str,
        language: str,
        is_expanded: bool,
        position: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO files (
                user_id, file_id, parent_id, type, name,
                content, language, is_expanded, position,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        self.session.execute(
            query,
            (
                user_id, file_id, parent_id, type, name,
                content, language, is_expanded, position,
                now, now,
            ),
        )

    def bulk_upsert(self, user_id: UUID, items: List[Dict[str, Any]]) -> None:
        self.delete_all_files(user_id)
        for item in items:
            self.upsert_file(
                user_id=user_id,
                file_id=item["file_id"],
                parent_id=item.get("parent_id"),
                type=item["type"],
                name=item["name"],
                content=item.get("content", ""),
                language=item.get("language", ""),
                is_expanded=item.get("is_expanded", False),
                position=item.get("position", 0),
            )

    def update_file(self, user_id: UUID, file_id: UUID, **kwargs) -> None:
        if not kwargs:
            return
        allowed = {"name", "content", "language", "is_expanded", "position", "parent_id"}
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in allowed:
                updates.append(f"{key} = %s")
                values.append(value)
        if not updates:
            return
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc))
        values.extend([user_id, file_id])
        query = f"UPDATE files SET {', '.join(updates)} WHERE user_id = %s AND file_id = %s"
        self.session.execute(query, values)

    def delete_file(self, user_id: UUID, file_id: UUID) -> None:
        query = "DELETE FROM files WHERE user_id = %s AND file_id = %s"
        self.session.execute(query, (user_id, file_id))

    def delete_all_files(self, user_id: UUID) -> None:
        query = "DELETE FROM files WHERE user_id = %s"
        self.session.execute(query, (user_id,))
