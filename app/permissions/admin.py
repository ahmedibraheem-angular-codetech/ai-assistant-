"""User-facing permission changes. Tools cannot call this module."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.service import AgentService
from app.files.exceptions import FileAccessError, InvalidWorkspaceIdError, WorkspaceNotFoundError
from app.files.repository import WorkspaceRepository
from app.permissions.audit import add_audit
from app.permissions.engine import ALLOW, DENY, REQUIRES_APPROVAL
from app.permissions.manifests import manifest_for_tool
from app.permissions.repository import CAPABILITIES, PermissionRepository
from app.permissions.service import evaluate_request, normalize_path_grant, workspace_uuid
from app.tools.models import AgentToolGrantRecord
from app.tools.registry import ToolRegistry
from app.tools.runtime import stop_file_workers

_FILE_TOOLS = frozenset(
    {
        "list_files",
        "search_files",
        "read_file",
        "get_file_metadata",
        "inspect_project",
        "create_text_file",
        "update_text_file",
        "quarantine_file",
        "show_directory",
        "measure_file",
        "preview_text",
        "bounded_wait",
    }
)


class PermissionAdmin:
    def __init__(
        self,
        session: Session,
        registry: ToolRegistry,
        agents: AgentService,
        workspaces: WorkspaceRepository,
    ) -> None:
        self.session = session
        self.registry = registry
        self.agents = agents
        self.workspaces = workspaces
        self.policy = PermissionRepository(session)

    def configuration(self) -> dict:
        mode = self.policy.mode()
        return {
            "mode": mode,
            "modes": [
                {
                    "id": "always_ask",
                    "summary": "Granted low-risk tools run. Reads and sensitive actions need approval.",
                },
                {
                    "id": "ask_sensitive",
                    "summary": "Granted read-only tools run. Sensitive actions need approval.",
                },
                {
                    "id": "restricted_auto",
                    "summary": "Granted local reads run. Internet and sensitive actions stay denied.",
                },
            ],
            "capabilities": self._capabilities(),
        }

    def set_mode(self, mode: str) -> dict:
        """Change the mode and cancel pending MCP execution approvals together.

        Every mode change invalidates pending call_mcp_tool and read_mcp_resource
        approvals. The decision that created them belonged to the previous mode,
        and restricted_auto must not keep a pending MCP execution. Discovery does
        not create approval rows. Non-MCP approvals keep their existing paths.
        """
        from app.approvals.review import MCP_TOOLS
        from app.approvals.service import cancel_pending

        self.policy.session.expire_all()
        previous = self.policy.mode()
        add_audit(self.session, event="mode_changed", detail=f"{previous} -> {mode}")
        cancel_pending(self.session, tool_names=set(MCP_TOOLS))
        self.policy.set_mode(mode)
        return self.configuration()

    def manifests(self) -> list[dict]:
        self.session.expire_all()
        grants = list(self.session.scalars(select(AgentToolGrantRecord)).all())
        by_tool: dict[str, list[str]] = {}
        for grant in grants:
            by_tool.setdefault(grant.tool_name, [])
            if grant.agent_id not in by_tool[grant.tool_name]:
                by_tool[grant.tool_name].append(grant.agent_id)
        rows = []
        for tool in self.registry.list_tools():
            manifest = manifest_for_tool(tool)
            stored = self.policy.tool_enabled(tool.name)
            effective = bool(tool.is_enabled() and stored is not False)
            rows.append(
                {
                    "tool_id": manifest.tool_id,
                    "name": manifest.name,
                    "description": manifest.description,
                    "resource_types": list(manifest.resource_types),
                    "actions": list(manifest.actions),
                    "risk": manifest.risk,
                    "approval_required": manifest.approval_required,
                    "read_only": manifest.read_only,
                    "execution_policy": manifest.execution_policy,
                    "stored_enabled": True if stored is None else stored,
                    "enabled": effective,
                    "agent_ids": by_tool.get(tool.name, []),
                }
            )
        return rows

    def set_tool_enabled(self, name: str, enabled: bool) -> dict:
        if self.registry.get(name) is None:
            from app.tools.exceptions import ToolNotFoundError

            raise ToolNotFoundError(name)
        add_audit(
            self.session,
            event="tool_enabled" if enabled else "tool_disabled",
            detail="enabled" if enabled else "disabled",
            tool_name=name,
        )
        if not enabled:
            from app.approvals.service import cancel_pending

            cancel_pending(self.session, tool_names={name})
        self.policy.set_tool_enabled(name, enabled)
        if not enabled and name in _FILE_TOOLS:
            stop_file_workers(tools=frozenset({name}))
        return next(item for item in self.manifests() if item["name"] == name)

    def set_capability(self, name: str, enabled: bool) -> dict:
        if name not in CAPABILITIES:
            raise ValueError(name)
        add_audit(
            self.session,
            event="capability_changed",
            detail="enabled" if enabled else "disabled",
            resource_type="capability",
            resource_id=name,
        )
        self.policy.set_capability(name, enabled)
        return next(item for item in self._capabilities() if item["name"] == name)

    def replace_paths(self, agent_id: str, grants: list[dict]) -> dict:
        agent = self.agents.get(agent_id)
        rows = []
        for grant in grants:
            workspace_id = workspace_uuid(grant["workspace_id"])
            if workspace_id is None or self.workspaces.get(workspace_id) is None:
                if workspace_id is None:
                    raise InvalidWorkspaceIdError()
                raise WorkspaceNotFoundError()
            try:
                relative = normalize_path_grant(grant["relative_path"])
            except FileAccessError as exc:
                raise InvalidWorkspaceIdError() from exc
            rows.append(
                {
                    "workspace_id": workspace_id,
                    "relative_path": relative,
                    "kind": grant["kind"],
                    "action": grant["action"],
                }
            )
        from app.approvals.service import cancel_pending

        cancel_pending(self.session, agent_id=agent.id)
        add_audit(
            self.session,
            event="path_grants_replaced",
            detail=f"{len(rows)} path grants",
            agent_id=agent.id,
            resource_type="path",
        )
        self.policy.replace_paths(agent.id, rows)
        stop_file_workers(agent_id=agent.id)
        return self.effective(agent.id)

    def effective(self, agent_id: str) -> dict:
        agent = self.agents.get(agent_id)
        self.session.expire_all()
        from app.tools.grants import AgentToolGrantRepository
        from app.files.grants import AgentWorkspaceGrantRepository

        names = AgentToolGrantRepository(self.session).names_for(agent.id)
        tools = []
        for name in names:
            tool = self.registry.get(name)
            if tool is None:
                continue
            manifest = manifest_for_tool(tool)
            stored = self.policy.tool_enabled(name)
            tools.append(
                {
                    "name": name,
                    "granted": True,
                    "enabled": bool(tool.is_enabled() and stored is not False),
                    "risk": manifest.risk,
                    "read_only": manifest.read_only,
                    "actions": list(manifest.actions),
                    "resource_types": list(manifest.resource_types),
                }
            )
        paths = [
            {
                "workspace_id": row.workspace_id,
                "relative_path": row.relative_path,
                "kind": row.kind,
                "action": row.action,
                "executable": row.action in {"read", "create", "write", "delete"},
            }
            for row in self.policy.path_grants(agent.id)
        ]
        return {
            "agent_id": agent.id,
            "mode": self.policy.mode(),
            "tools": tools,
            "workspace_ids": AgentWorkspaceGrantRepository(self.session).ids_for(agent.id),
            "path_grants": paths,
            "capabilities": self._capabilities(),
        }

    def explain(
        self,
        *,
        agent_id: str | None,
        tool_name: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        relative_path: str | None,
    ) -> dict:
        decision = evaluate_request(
            agent_id=agent_id,
            tool_name=tool_name,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            relative_path=relative_path,
            registry=self.registry,
        )
        if decision.decision != ALLOW:
            event = "approval_required" if decision.decision == REQUIRES_APPROVAL else "denied"
            add_audit(
                self.session,
                event=event,
                decision=decision.decision,
                detail=decision.reason,
                agent_id=agent_id,
                tool_name=tool_name,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
            )
            self.session.commit()
        return {
            "decision": decision.decision,
            "reason": decision.reason,
            "code": decision.code,
            "mode": decision.mode,
        }

    def _capabilities(self) -> list[dict]:
        stored = self.policy.capability_enabled()
        return [
            {
                "name": name,
                "stored_enabled": stored[name],
                "executable": False,
                "reason": "Not available in this version.",
            }
            for name in CAPABILITIES
        ]
