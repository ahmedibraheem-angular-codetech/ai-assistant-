"""Read one workspace file once through the existing descriptor guard."""

import os
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.files.access import ApprovedRoot, open_file, read_fd
from app.files.exceptions import FileAccessError
from app.files.models import READ_ONLY
from app.files.repository import WorkspaceRepository
from app.files.grants import AgentWorkspaceGrantRepository
from app.permissions.service import _path_allowed, normalize_path_grant
from app.tools.exceptions import ToolExecutionError, ToolValidationError

_DENIED = "That workspace is not allowed for this agent."
_PATH = "That resource is outside the allowed paths."
_UNAVAILABLE = "That directory is not available."
_CHANGED = "The source file changed while it was being read."
_LARGE = "That file is too large to ingest."


def read_authorized_bytes(agent_id: str, workspace_id: str, relative_path: str) -> tuple[bytes, str]:
    """Return the file bytes and the canonical path. The descriptor is closed."""
    try:
        parsed = str(UUID(workspace_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ToolValidationError("Tool argument 'workspace_id' must be a UUID.") from exc
    normalized = normalize_path_grant(relative_path)
    session = get_session_factory()()
    try:
        if not AgentWorkspaceGrantRepository(session).allows(agent_id, parsed):
            raise FileAccessError(_DENIED)
        if not _path_allowed(session, agent_id, parsed, normalized):
            raise FileAccessError(_PATH)
        root = _root(session, parsed)
    finally:
        session.close()
    descriptor, info, canonical = open_file(root, normalized)
    try:
        size = int(info.st_size)
        if size > get_settings().knowledge_max_file_bytes:
            raise ToolExecutionError(_LARGE)
        payload = read_fd(descriptor, size, get_settings().knowledge_max_file_bytes)
    finally:
        os.close(descriptor)
    if len(payload) != size:
        raise ToolExecutionError(_CHANGED)
    return payload, canonical


def _root(session: Session, workspace_id: str) -> ApprovedRoot:
    workspace = WorkspaceRepository(session).get(workspace_id)
    if workspace is None or not workspace.enabled or workspace.access_scope != READ_ONLY:
        raise FileAccessError(_UNAVAILABLE)
    return ApprovedRoot(path=workspace.root_path, device=workspace.root_device, inode=workspace.root_inode)
