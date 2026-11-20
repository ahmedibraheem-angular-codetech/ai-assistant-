"""Schema upgrades.

Version 2 adds conversation storage. Version 3 adds explicit personal memories.
Version 4 adds confirmation-gated memory actions. Version 5 adds agent tool
grants. Version 6 adds read-only workspace registrations. Version 7 adds per-agent
workspace grants. Version 8 adds permission policy, tool switches, path
grants, capability preferences, and the permission audit. Version 9 adds
approval requests. Version 10 keeps one pending request per operation.
Version 11 adds recovery records for file changes. Version 12 adds the
file-operation journal used to recover an interrupted change. Version 13 adds
a nullable approval id on that journal. Existing journal rows are left in
place and stay unlinked. Version 14 adds knowledge documents, generations,
chunks, and access rows. Version 15 adds the singleton MCP configuration
revision. Version 16 adds encrypted MCP execution payloads. Existing rows
in other tables are left in place.
Existing grant rows
are not rewritten. Each upgrade creates
new tables and does not rewrite or delete existing rows. create_all is not
used to alter existing tables.
"""

from sqlalchemy import Engine, text

from app.approvals.models import ApprovalRequestRecord
from app.conversations.models import ConversationRecord, MessageRecord
from app.files.models import (
    AgentWorkspaceGrantRecord,
    FileOperationJournalRecord,
    FileRecoveryRecord,
    WorkspaceRecord,
)
from app.memories.models import MemoryRecord, PendingMemoryActionRecord
from app.permissions.models import (
    AgentPathGrantRecord,
    CapabilityPreferenceRecord,
    PermissionAuditRecord,
    PermissionPolicyRecord,
    ToolSettingRecord,
)
from app.tools.models import AgentToolGrantRecord


def apply_migration(engine: Engine, version: int) -> None:
    if version == 2:
        upgrade_to_2(engine)
        return
    if version == 3:
        upgrade_to_3(engine)
        return
    if version == 4:
        upgrade_to_4(engine)
        return
    if version == 5:
        upgrade_to_5(engine)
        return
    if version == 6:
        upgrade_to_6(engine)
        return
    if version == 7:
        upgrade_to_7(engine)
        return
    if version == 8:
        upgrade_to_8(engine)
        return
    if version == 9:
        upgrade_to_9(engine)
        return
    if version == 10:
        upgrade_to_10(engine)
        return
    if version == 11:
        upgrade_to_11(engine)
        return
    if version == 12:
        upgrade_to_12(engine)
        return
    if version == 13:
        upgrade_to_13(engine)
        return
    if version == 14:
        upgrade_to_14(engine)
        return
    if version == 15:
        upgrade_to_15(engine)
        return
    if version == 16:
        upgrade_to_16(engine)
        return
    if version == 17:
        upgrade_to_17(engine)
        return
    if version == 18:
        upgrade_to_18(engine)
        return
    if version == 19:
        upgrade_to_19(engine)
        return
    if version == 20:
        upgrade_to_20(engine)
        return
    if version == 21:
        upgrade_to_21(engine)
        return
    if version == 22:
        upgrade_to_22(engine)
        return
    raise RuntimeError(
        f"No migration is registered for schema version {version}. Existing data was left unchanged."
    )


def upgrade_to_2(engine: Engine) -> None:
    ConversationRecord.__table__.create(bind=engine, checkfirst=True)
    MessageRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_3(engine: Engine) -> None:
    MemoryRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_4(engine: Engine) -> None:
    PendingMemoryActionRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_5(engine: Engine) -> None:
    AgentToolGrantRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_6(engine: Engine) -> None:
    WorkspaceRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_7(engine: Engine) -> None:
    AgentWorkspaceGrantRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_8(engine: Engine) -> None:
    PermissionPolicyRecord.__table__.create(bind=engine, checkfirst=True)
    ToolSettingRecord.__table__.create(bind=engine, checkfirst=True)
    AgentPathGrantRecord.__table__.create(bind=engine, checkfirst=True)
    CapabilityPreferenceRecord.__table__.create(bind=engine, checkfirst=True)
    PermissionAuditRecord.__table__.create(bind=engine, checkfirst=True)
    from app.permissions.repository import ensure_permission_policy

    ensure_permission_policy(engine)


def upgrade_to_9(engine: Engine) -> None:
    ApprovalRequestRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_11(engine: Engine) -> None:
    FileRecoveryRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_12(engine: Engine) -> None:
    FileOperationJournalRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_13(engine: Engine) -> None:
    """Link a journal to the approval that started it. Older rows stay null."""
    with engine.begin() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(file_operation_journal)")).fetchall()
        }
        if "approval_id" not in columns:
            connection.execute(
                text("ALTER TABLE file_operation_journal ADD COLUMN approval_id VARCHAR(36)")
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_file_journal_approval "
                "ON file_operation_journal (approval_id) WHERE approval_id IS NOT NULL"
            )
        )


def upgrade_to_14(engine: Engine) -> None:
    """Add local knowledge tables. No existing table is rewritten."""
    from app.knowledge.schema import ensure_knowledge_schema

    ensure_knowledge_schema(engine)


def upgrade_to_15(engine: Engine) -> None:
    """Add the global MCP revision row. No existing table is rewritten."""
    from app.mcp.schema import ensure_mcp_schema

    ensure_mcp_schema(engine)


def upgrade_to_16(engine: Engine) -> None:
    """Add encrypted MCP payloads. Existing approval rows are not rewritten."""
    from app.approvals.payloads import McpExecutionPayloadRecord

    McpExecutionPayloadRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_17(engine: Engine) -> None:
    """Add standing approvals for read-only tools. No existing table is rewritten."""
    from app.permissions.models import StandingApprovalRecord

    StandingApprovalRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_18(engine: Engine) -> None:
    """Add assistant controls (pause and budgets). No existing table is rewritten."""
    from app.core.controls import AssistantControlRecord

    AssistantControlRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_19(engine: Engine) -> None:
    """Add agent teams for delegation. No existing table is rewritten."""
    from app.agents.team import AgentTeamMemberRecord

    AgentTeamMemberRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_20(engine: Engine) -> None:
    """Add project memory. No existing table is rewritten."""
    from app.projects.core import ProjectEntryRecord, ProjectRecord

    ProjectRecord.__table__.create(bind=engine, checkfirst=True)
    ProjectEntryRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_21(engine: Engine) -> None:
    """Add the knowledge graph. No existing table is rewritten."""
    from app.graph.core import GraphEntityRecord, GraphRelationRecord

    GraphEntityRecord.__table__.create(bind=engine, checkfirst=True)
    GraphRelationRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_22(engine: Engine) -> None:
    """Add agent lessons. No existing table is rewritten."""
    from app.learning.lessons import AgentLessonRecord

    AgentLessonRecord.__table__.create(bind=engine, checkfirst=True)


def upgrade_to_10(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_pending_fingerprint "
                "ON approval_requests (fingerprint) WHERE status = 'PENDING'"
            )
        )
