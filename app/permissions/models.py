"""Permission policy storage.

These tables record user policy. They do not grant file writes, shell access,
or any capability that has no implementation. Existing tool and workspace
grant tables stay the source of those grants.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PermissionPolicyRecord(Base):
    """The single permission mode for this local assistant."""

    __tablename__ = "permission_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolSettingRecord(Base):
    """User enablement for one registered tool. A missing row follows the tool."""

    __tablename__ = "tool_settings"

    tool_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentPathGrantRecord(Base):
    """Optional file or folder allow-list inside an already granted workspace.

    Read rows narrow read access. Modify and delete rows are stored only.
    They do not authorize a write or a delete.
    """

    __tablename__ = "agent_path_grants"

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    relative_path: Mapped[str] = mapped_column(String(400), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    action: Mapped[str] = mapped_column(String(16), primary_key=True)


class CapabilityPreferenceRecord(Base):
    """A stored preference for a capability this version does not execute."""

    __tablename__ = "capability_preferences"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PermissionAuditRecord(Base):
    """A permission change or a non-allow decision. File contents are not stored."""

    __tablename__ = "permission_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str] = mapped_column(String(180), nullable=False)


class StandingApprovalRecord(Base):
    """A user's standing yes for one agent, one read-only tool, and one scope.

    It replaces the per-call approval for reads only, and only in
    ask_sensitive mode. It never covers create/write/delete, commands,
    sensitive tools, or MCP. Only the local user can create it (admin API);
    tools and model payloads cannot.
    """

    __tablename__ = "standing_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Normalized relative path. None means the whole workspace.
    path_prefix: Mapped[str | None] = mapped_column(String(400), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
