from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.files.models import READ_ONLY, Workspace, WorkspaceRecord, to_workspace


class WorkspaceRepository:
    """The only workspace code that touches SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_workspaces(self) -> list[Workspace]:
        statement = select(WorkspaceRecord).order_by(
            WorkspaceRecord.created_at.asc(),
            WorkspaceRecord.id.asc(),
        )
        return [to_workspace(record) for record in self.session.scalars(statement).all()]

    def get(self, workspace_id: str) -> Workspace | None:
        record = self.session.get(WorkspaceRecord, workspace_id)
        if record is None:
            return None
        return to_workspace(record)

    def get_by_normalized_name(self, name_normalized: str) -> Workspace | None:
        statement = select(WorkspaceRecord).where(WorkspaceRecord.name_normalized == name_normalized)
        record = self.session.scalar(statement)
        if record is None:
            return None
        return to_workspace(record)

    def get_by_root(self, root_path: str) -> Workspace | None:
        statement = select(WorkspaceRecord).where(WorkspaceRecord.root_path == root_path)
        record = self.session.scalar(statement)
        if record is None:
            return None
        return to_workspace(record)

    def count(self) -> int:
        statement = select(func.count()).select_from(WorkspaceRecord)
        return int(self.session.scalar(statement) or 0)

    def insert(
        self,
        *,
        workspace_id: str,
        name: str,
        name_normalized: str,
        root_path: str,
        root_device: int,
        root_inode: int,
        created_at: datetime,
    ) -> Workspace:
        record = WorkspaceRecord(
            id=workspace_id,
            name=name,
            name_normalized=name_normalized,
            root_path=root_path,
            root_device=root_device,
            root_inode=root_inode,
            access_scope=READ_ONLY,
            enabled=True,
            created_at=created_at,
            updated_at=created_at,
        )
        self.session.add(record)
        self.session.commit()
        return to_workspace(record)

    def update(
        self,
        workspace_id: str,
        *,
        name: str | None,
        name_normalized: str | None,
        enabled: bool | None,
        updated_at: datetime,
    ) -> Workspace | None:
        record = self.session.get(WorkspaceRecord, workspace_id)
        if record is None:
            return None
        if name is not None and name_normalized is not None:
            record.name = name
            record.name_normalized = name_normalized
        if enabled is not None:
            record.enabled = enabled
        record.updated_at = updated_at
        self.session.commit()
        return to_workspace(record)

    def delete(self, workspace_id: str) -> bool:
        record = self.session.get(WorkspaceRecord, workspace_id)
        if record is None:
            return False
        self.session.delete(record)
        self.session.commit()
        return True
