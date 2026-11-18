"""Seal a knowledge approval to the source hash and model digest."""

from uuid import UUID

from app.agents.repository import AgentRepository
from app.approvals.fingerprint import bind_arguments
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.files.changes import StagedChange
from app.files.exceptions import FileAccessError
from app.knowledge.identity import get_embedding_client
from app.knowledge.source import read_authorized_bytes
from app.knowledge.store import content_hash, get_document
from app.tools.exceptions import ToolExecutionError, ToolValidationError

_SCOPE = "That knowledge scope is not allowed."
_PARENT = "User-private knowledge is available to the parent agent."
_RECIPIENTS = "Those knowledge recipients are not allowed."
_DOCUMENT = "That document was not found."

_MUTATIONS = frozenset({"ingest_document", "reindex_document", "delete_document"})
_SCOPES = frozenset({"user_private", "agent_private", "project", "shared"})


def stage_knowledge(tool_name: str, agent_id: str | None, arguments: dict) -> StagedChange:
    if tool_name not in _MUTATIONS:
        return StagedChange(bind_arguments(arguments), None)
    if not isinstance(agent_id, str):
        raise FileAccessError(_SCOPE)
    if tool_name == "delete_document":
        return _stage_delete(agent_id, arguments)
    if tool_name == "reindex_document":
        return _stage_reindex(agent_id, arguments)
    return _stage_ingest(agent_id, arguments)


def _stage_ingest(agent_id: str, arguments: dict) -> StagedChange:
    scope = arguments.get("scope")
    if scope not in _SCOPES:
        raise ToolValidationError("Tool argument 'scope' is not valid.")
    _require_scope_caller(agent_id, scope)
    workspace_id = arguments.get("workspace_id")
    relative = arguments.get("relative_path")
    if not isinstance(workspace_id, str) or not isinstance(relative, str):
        raise ToolValidationError("The source workspace and path are required.")
    payload, canonical = read_authorized_bytes(agent_id, workspace_id, relative)
    recipients = _recipients(agent_id, scope, arguments.get("recipient_agent_ids"))
    model = get_settings().knowledge_embedding_model
    digest = get_embedding_client().resolve(model)
    sealed = {
        "operation": "ingest",
        "scope": scope,
        "workspace_id": workspace_id,
        "relative_path": canonical,
        "source_sha256": content_hash(payload),
        "embedding_model": model,
        "embedding_digest": digest,
        "recipient_agent_ids": recipients,
    }
    return StagedChange(bind_arguments(sealed), None)


def _stage_reindex(agent_id: str, arguments: dict) -> StagedChange:
    document = _owned(agent_id, arguments.get("document_id"), mutating=True)
    payload, canonical = read_authorized_bytes(agent_id, document["workspace_id"], document["relative_path"])
    if canonical != document["relative_path"]:
        raise FileAccessError(_DOCUMENT)
    recipients = _recipients(agent_id, document["scope"], arguments.get("recipient_agent_ids"))
    model = get_settings().knowledge_embedding_model
    digest = get_embedding_client().resolve(model)
    sealed = {
        "operation": "reindex",
        "document_id": document["id"],
        "scope": document["scope"],
        "workspace_id": document["workspace_id"],
        "relative_path": canonical,
        "source_sha256": content_hash(payload),
        "embedding_model": model,
        "embedding_digest": digest,
        "recipient_agent_ids": recipients,
    }
    return StagedChange(bind_arguments(sealed), None)


def _stage_delete(agent_id: str, arguments: dict) -> StagedChange:
    document = _owned(agent_id, arguments.get("document_id"), mutating=True)
    if document.get("active_generation_id") is None:
        raise FileAccessError(_DOCUMENT)
    model = get_settings().knowledge_embedding_model
    digest = get_embedding_client().resolve(model)
    sealed = {
        "operation": "delete",
        "document_id": document["id"],
        "scope": document["scope"],
        "generation_id": document["active_generation_id"],
        "source_sha256": document.get("content_sha256") or "",
        "embedding_model": model,
        "embedding_digest": digest,
    }
    return StagedChange(bind_arguments(sealed), None)


def _require_scope_caller(agent_id: str, scope: str) -> None:
    if scope == "user_private" and agent_id != get_settings().parent_agent_id:
        raise FileAccessError(_PARENT)


def _recipients(agent_id: str, scope: str, value: object) -> list[str]:
    if scope in {"agent_private", "project"}:
        if value not in (None, []):
            raise ToolValidationError(_RECIPIENTS)
        return []
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ToolValidationError(_RECIPIENTS)
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ToolValidationError(_RECIPIENTS)
        try:
            parsed = str(UUID(item))
        except ValueError as exc:
            raise ToolValidationError(_RECIPIENTS) from exc
        if parsed not in found:
            found.append(parsed)
    if len(found) > 8:
        raise ToolValidationError(_RECIPIENTS)
    if scope == "shared" and not found:
        raise ToolValidationError(_RECIPIENTS)
    session = get_session_factory()()
    try:
        repository = AgentRepository(session)
        for recipient in found:
            agent = repository.get(recipient)
            if agent is None or not agent.enabled:
                raise FileAccessError(_RECIPIENTS)
    finally:
        session.close()
    if scope == "user_private" and agent_id in found and agent_id != get_settings().parent_agent_id:
        raise FileAccessError(_PARENT)
    return sorted(found)


def _owned(agent_id: str, document_id: object, *, mutating: bool) -> dict:
    if not isinstance(document_id, str):
        raise ToolValidationError("Tool argument 'document_id' must be a string.")
    document = get_document(document_id)
    if document is None:
        raise FileAccessError(_DOCUMENT)
    if not can_mutate(agent_id, document):
        raise FileAccessError(_DOCUMENT)
    return document


def can_mutate(agent_id: str, document: dict) -> bool:
    scope = document.get("scope")
    if scope == "user_private":
        return agent_id == get_settings().parent_agent_id
    if scope == "agent_private":
        return document.get("owner_agent_id") == agent_id
    if scope == "shared":
        return document.get("created_by_agent_id") == agent_id
    if scope == "project":
        return _project_visible(agent_id, document)
    return False


def _project_visible(agent_id: str, document: dict) -> bool:
    from app.files.grants import AgentWorkspaceGrantRepository
    from app.permissions.service import _path_allowed

    workspace_id = document.get("workspace_id")
    relative_path = document.get("relative_path")
    if not isinstance(workspace_id, str) or not isinstance(relative_path, str):
        return False
    session = get_session_factory()()
    try:
        granted = AgentWorkspaceGrantRepository(session).allows(agent_id, workspace_id)
        return granted and _path_allowed(session, agent_id, workspace_id, relative_path)
    finally:
        session.close()
