"""Reads and writes permission rows. Grant tables are not duplicated here."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.permissions.engine import DEFAULT_MODE, MODES
from app.permissions.models import (
    AgentPathGrantRecord,
    CapabilityPreferenceRecord,
    PermissionPolicyRecord,
    ToolSettingRecord,
)

POLICY_ID = 1
CAPABILITIES = (
    "terminal",
    "external_app",
    "internet",
    "tool_install",
    "file_modify",
    "file_delete",
)


class PermissionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def mode(self) -> str:
        self.session.expire_all()
        row = self.session.get(PermissionPolicyRecord, POLICY_ID)
        if row is None or row.mode not in MODES:
            return DEFAULT_MODE
        return row.mode

    def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise ValueError(mode)
        now = datetime.now(timezone.utc)
        row = self.session.get(PermissionPolicyRecord, POLICY_ID)
        if row is None:
            self.session.add(PermissionPolicyRecord(id=POLICY_ID, mode=mode, updated_at=now))
        else:
            row.mode = mode
            row.updated_at = now
        self.session.commit()
        return mode

    def tool_enabled(self, tool_name: str) -> bool | None:
        self.session.expire_all()
        row = self.session.get(ToolSettingRecord, tool_name)
        if row is None:
            return None
        return bool(row.enabled)

    def set_tool_enabled(self, tool_name: str, enabled: bool) -> None:
        now = datetime.now(timezone.utc)
        row = self.session.get(ToolSettingRecord, tool_name)
        if row is None:
            self.session.add(ToolSettingRecord(tool_name=tool_name, enabled=enabled, updated_at=now))
        else:
            row.enabled = enabled
            row.updated_at = now
        self.session.commit()

    def capability_enabled(self) -> dict[str, bool]:
        self.session.expire_all()
        rows = list(self.session.scalars(select(CapabilityPreferenceRecord)).all())
        stored = {row.name: bool(row.enabled) for row in rows}
        return {name: stored.get(name, False) for name in CAPABILITIES}

    def set_capability(self, name: str, enabled: bool) -> None:
        if name not in CAPABILITIES:
            raise ValueError(name)
        now = datetime.now(timezone.utc)
        row = self.session.get(CapabilityPreferenceRecord, name)
        if row is None:
            self.session.add(CapabilityPreferenceRecord(name=name, enabled=enabled, updated_at=now))
        else:
            row.enabled = enabled
            row.updated_at = now
        self.session.commit()

    def path_grants(self, agent_id: str) -> list[AgentPathGrantRecord]:
        self.session.expire_all()
        statement = (
            select(AgentPathGrantRecord)
            .where(AgentPathGrantRecord.agent_id == agent_id)
            .order_by(AgentPathGrantRecord.relative_path.asc())
        )
        return list(self.session.scalars(statement).all())

    def replace_paths(self, agent_id: str, rows: list[dict]) -> None:
        self.session.execute(
            delete(AgentPathGrantRecord).where(AgentPathGrantRecord.agent_id == agent_id)
        )
        seen: set[tuple] = set()
        for row in rows:
            key = (row["workspace_id"], row["relative_path"], row["kind"], row["action"])
            if key in seen:
                continue
            seen.add(key)
            self.session.add(
                AgentPathGrantRecord(
                    agent_id=agent_id,
                    workspace_id=row["workspace_id"],
                    relative_path=row["relative_path"],
                    kind=row["kind"],
                    action=row["action"],
                )
            )
        self.session.commit()


def user_enabled_tool(tool_name: str) -> bool:
    """False only when the user has stored an off switch. A missing row stays on."""
    from sqlalchemy.orm import Session as OrmSession

    with OrmSession(get_engine()) as session:
        session.expire_all()
        return PermissionRepository(session).tool_enabled(tool_name) is not False


def ensure_permission_policy(engine=None) -> None:
    """Insert the default mode when the table is empty. Existing modes stay."""
    from sqlalchemy.orm import Session as OrmSession

    bind = engine or get_engine()
    with OrmSession(bind) as session:
        if session.get(PermissionPolicyRecord, POLICY_ID) is not None:
            return
        session.add(
            PermissionPolicyRecord(
                id=POLICY_ID,
                mode=DEFAULT_MODE,
                updated_at=datetime.now(timezone.utc),
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()


def parse_agent_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None
