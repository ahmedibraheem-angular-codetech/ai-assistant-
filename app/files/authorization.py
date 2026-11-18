"""Fresh permission checks for one file operation.

Results are not cached. Every check opens a session, reads the current rows,
and closes it. A stored modify grant still does not authorize a change.
Create, write, and delete run only when an explicit path grant exists and the
parent has already moved that exact approval to EXECUTING.
"""

import contextvars

from app.files.exceptions import FileAccessError

_AGENT = contextvars.ContextVar("file_agent_id", default=None)
_TOOL = contextvars.ContextVar("file_tool_name", default=None)
_APPROVAL = contextvars.ContextVar("file_approval_id", default=None)

_DENIED = "That workspace is not allowed for this agent."
_MUTATION = "File changes are not permitted."
_MUTATING_ACTIONS = frozenset({"create", "write", "modify", "delete"})


def bind_file_actor(agent_id: str | None, tool_name: str | None, approval_id: str | None = None):
    agent_token = _AGENT.set(agent_id if isinstance(agent_id, str) else None)
    tool_token = _TOOL.set(tool_name if isinstance(tool_name, str) else None)
    approval_token = _APPROVAL.set(approval_id if isinstance(approval_id, str) else None)
    return agent_token, tool_token, approval_token


def reset_file_actor(tokens) -> None:
    agent_token, tool_token, approval_token = tokens
    _AGENT.reset(agent_token)
    _TOOL.reset(tool_token)
    _APPROVAL.reset(approval_token)


def authorize(
    action: str,
    workspace_id: object,
    relative_path: str | None = None,
    arguments: dict | None = None,
) -> None:
    """Raise unless the current agent may perform this action in this workspace.

    The check reads the database again. A stored modify grant does not allow the
    action. Create, write, and delete also need a matching EXECUTING approval.
    The worker does not write an audit row while the parent request still holds
    its database transaction.
    """
    from app.permissions.engine import ALLOW
    from app.permissions.service import evaluate_request
    from app.tools.registry import get_tool_registry

    if action == "modify":
        raise FileAccessError(_MUTATION)
    decision = evaluate_request(
        agent_id=_AGENT.get() if isinstance(_AGENT.get(), str) else None,
        tool_name=_TOOL.get() if isinstance(_TOOL.get(), str) else None,
        action=action,
        resource_type="file" if isinstance(relative_path, str) and relative_path else "workspace",
        resource_id=workspace_id if isinstance(workspace_id, str) else None,
        relative_path=relative_path if isinstance(relative_path, str) else None,
        registry=get_tool_registry(),
    )
    if decision.decision == ALLOW:
        return
    if decision.decision == "REQUIRES_APPROVAL" and _approved_execution(action, arguments or {}):
        return
    if action in _MUTATING_ACTIONS:
        raise FileAccessError(_MUTATION)
    if decision.code == "workspace" or decision.code in {"ungranted", "unknown_agent", "agent_disabled"}:
        raise FileAccessError(_DENIED)
    raise FileAccessError(decision.reason)


def _approved_execution(action: str, arguments: dict) -> bool:
    from app.approvals.service import execution_matches

    agent_id = _AGENT.get() if isinstance(_AGENT.get(), str) else None
    tool_name = _TOOL.get() if isinstance(_TOOL.get(), str) else None
    approval_id = _APPROVAL.get() if isinstance(_APPROVAL.get(), str) else None
    return execution_matches(
        approval_id,
        agent_id=agent_id,
        tool_name=tool_name,
        action=action,
        arguments=arguments,
    )
