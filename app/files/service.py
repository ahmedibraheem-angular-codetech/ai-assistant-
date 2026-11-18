from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.files.access import approve_directory
from app.files.exceptions import (
    DuplicateWorkspaceNameError,
    DuplicateWorkspaceRootError,
    InvalidWorkspaceIdError,
    WorkspaceApprovalError,
    WorkspaceLimitError,
    WorkspaceNotFoundError,
)
from app.files.models import READ_ONLY, Workspace
from app.files.repository import WorkspaceRepository
from app.tools.runtime import stop_file_workers
from app.files.schemas import WorkspaceCreate, WorkspaceListResponse, WorkspaceRead, WorkspaceUpdate

_APPROVAL = (
    "Read-only access to this directory. "
    "Registration does not grant write, delete, or command execution."
)
_LIMIT = 50


class WorkspaceService:
    def __init__(self, repository: WorkspaceRepository) -> None:
        self.repository = repository

    def list_workspaces(self) -> WorkspaceListResponse:
        rows = [self._read(item) for item in self.repository.list_workspaces()]
        return WorkspaceListResponse(workspaces=rows, count=len(rows))

    def get(self, workspace_id: str) -> WorkspaceRead:
        return self._read(self._require(workspace_id))

    def create(self, payload: WorkspaceCreate) -> WorkspaceRead:
        if payload.confirm_read_only is not True:
            raise WorkspaceApprovalError()
        if self.repository.count() >= _LIMIT:
            raise WorkspaceLimitError()
        approved = approve_directory(payload.path)
        normalized = payload.name.casefold()
        if self.repository.get_by_normalized_name(normalized) is not None:
            raise DuplicateWorkspaceNameError(payload.name)
        if self.repository.get_by_root(approved.path) is not None:
            raise DuplicateWorkspaceRootError()
        created = self.repository.insert(
            workspace_id=str(uuid4()),
            name=payload.name,
            name_normalized=normalized,
            root_path=approved.path,
            root_device=approved.device,
            root_inode=approved.inode,
            created_at=datetime.now(timezone.utc),
        )
        return self._read(created)

    def update(self, workspace_id: str, payload: WorkspaceUpdate) -> WorkspaceRead:
        current = self._require(workspace_id)
        normalized = None
        if payload.name is not None:
            normalized = payload.name.casefold()
            existing = self.repository.get_by_normalized_name(normalized)
            if existing is not None and existing.id != current.id:
                raise DuplicateWorkspaceNameError(payload.name)
        updated = self.repository.update(
            current.id,
            name=payload.name,
            name_normalized=normalized,
            enabled=payload.enabled,
            updated_at=datetime.now(timezone.utc),
        )
        if updated is None:
            raise WorkspaceNotFoundError()
        return self._read(updated)

    def delete(self, workspace_id: str) -> None:
        current = self._require(workspace_id)
        from app.approvals.service import cancel_pending

        cancel_pending(self.repository.session, workspace_id=current.id)
        stop_file_workers(workspace_id=current.id)
        if not self.repository.delete(current.id):
            raise WorkspaceNotFoundError()

    def _require(self, workspace_id: str) -> Workspace:
        try:
            parsed = str(UUID(workspace_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvalidWorkspaceIdError() from exc
        workspace = self.repository.get(parsed)
        if workspace is None:
            raise WorkspaceNotFoundError()
        return workspace

    def _read(self, workspace: Workspace) -> WorkspaceRead:
        return WorkspaceRead(
            id=workspace.id,
            name=workspace.name,
            root_path=workspace.root_path,
            access_scope=READ_ONLY,
            enabled=workspace.enabled,
            approval=_APPROVAL,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
        )
