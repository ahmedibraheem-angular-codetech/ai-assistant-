"""Pure permission decisions.

Callers load the current rows and pass them in. This module does not read the
database, the model payload, or a previous decision.
"""

from dataclasses import dataclass

ALLOW = "ALLOW"
DENY = "DENY"
REQUIRES_APPROVAL = "REQUIRES_APPROVAL"

MODES = ("always_ask", "ask_sensitive", "restricted_auto")
DEFAULT_MODE = "ask_sensitive"

_MUTATING = frozenset({"write", "modify", "delete"})
_FUTURE_ACTIONS = frozenset({"install", "launch", "execute_command"})
_FUTURE_RESOURCES = frozenset({"terminal", "application", "installer"})
_FILE_RESOURCES = frozenset({"workspace", "file", "folder"})

_FILE_CHANGE = "File changes are not permitted."
_UNAVAILABLE = "That action is not available."
_UNKNOWN_AGENT = "That agent was not found."
_DISABLED_AGENT = "That agent is turned off."
_UNKNOWN_TOOL = "That tool is not available."
_DISABLED_TOOL = "That tool is turned off."
_UNGRANTED = "That tool is not allowed for this agent."
_WORKSPACE = "That workspace is not allowed for this agent."
_PATH = "That resource is outside the allowed paths."
_APPROVAL = "This action needs approval."
_RESTRICTED = "That action is not allowed in restricted mode."
_INTERNET = "Internet access is not allowed in restricted mode."
_SAFE = "Allowed by an explicit safe permission."
_READ = "Allowed by an explicit read permission."


@dataclass(frozen=True, slots=True)
class PermissionFacts:
    mode: str
    agent_id: str | None
    agent_found: bool
    agent_enabled: bool
    tool_name: str | None
    tool_known: bool
    tool_enabled: bool
    tool_granted: bool
    risk: str
    action: str
    allowed_actions: tuple[str, ...]
    resource_type: str
    workspace_required: bool
    workspace_granted: bool
    path_allowed: bool
    internet: bool
    change_granted: bool = True


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    decision: str
    reason: str
    code: str
    mode: str


def decide(facts: PermissionFacts) -> PermissionDecision:
    """Deny unless the loaded facts contain an explicit permission for this mode."""
    if facts.tool_name is None:
        return _bare(facts)
    if facts.action == "modify" or (
        facts.action in {"create", "write", "delete"} and not facts.change_granted
    ):
        return _deny(facts, _FILE_CHANGE, "unavailable")
    if facts.action in _FUTURE_ACTIONS or facts.resource_type in _FUTURE_RESOURCES:
        return _deny(facts, _UNAVAILABLE, "unavailable")
    if not facts.agent_found:
        return _deny(facts, _UNKNOWN_AGENT, "unknown_agent")
    if facts.agent_id is not None and not facts.agent_enabled:
        return _deny(facts, _DISABLED_AGENT, "agent_disabled")
    if not facts.tool_known:
        return _deny(facts, _UNKNOWN_TOOL, "unknown_tool")
    if facts.action not in facts.allowed_actions:
        return _deny(facts, _UNAVAILABLE, "unknown_action")
    if not facts.tool_enabled:
        return _deny(facts, _DISABLED_TOOL, "disabled")
    if not facts.tool_granted:
        return _deny(facts, _UNGRANTED, "ungranted")
    if facts.workspace_required and not facts.workspace_granted:
        return _deny(facts, _WORKSPACE, "workspace")
    if not facts.path_allowed:
        return _deny(facts, _PATH, "path")
    if facts.risk not in {"low", "read", "sensitive"} or facts.mode not in MODES:
        return _deny(facts, _UNAVAILABLE, "unknown_action")
    if facts.action in {"create", "write", "delete"}:
        if facts.mode == "restricted_auto":
            return _deny(facts, _RESTRICTED, "restricted")
        return _approval(facts)
    if facts.risk == "sensitive":
        if facts.mode == "restricted_auto":
            return _deny(facts, _RESTRICTED, "restricted")
        return _approval(facts)
    if facts.risk == "read":
        if facts.mode == "always_ask":
            return _approval(facts)
        if facts.mode == "restricted_auto" and facts.internet:
            return _deny(facts, _INTERNET, "restricted")
        return _allow(facts, _READ)
    return _allow(facts, _SAFE)


def _bare(facts: PermissionFacts) -> PermissionDecision:
    if facts.action in _MUTATING:
        return _deny(facts, _FILE_CHANGE, "unavailable")
    if facts.action in _FUTURE_ACTIONS or facts.resource_type in _FUTURE_RESOURCES:
        return _deny(facts, _UNAVAILABLE, "unavailable")
    if facts.resource_type == "internet":
        if facts.mode == "restricted_auto":
            return _deny(facts, _INTERNET, "restricted")
        if facts.mode == "always_ask":
            return _approval(facts)
        return _deny(facts, _UNAVAILABLE, "unknown_action")
    return _deny(facts, _UNAVAILABLE, "unknown_action")


def _allow(facts: PermissionFacts, reason: str) -> PermissionDecision:
    return PermissionDecision(ALLOW, reason, "allow", facts.mode)


def _deny(facts: PermissionFacts, reason: str, code: str) -> PermissionDecision:
    return PermissionDecision(DENY, reason, code, facts.mode)


def _approval(facts: PermissionFacts) -> PermissionDecision:
    return PermissionDecision(REQUIRES_APPROVAL, _APPROVAL, "approval", facts.mode)
