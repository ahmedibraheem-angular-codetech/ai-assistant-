"""SQLite engine, sessions, and non-destructive schema initialization.

The default URL points at a local SQLite file. The repository talks to SQLAlchemy
only, so a later move to PostgreSQL is a DATABASE_URL change plus a driver,
not a rewrite of agent storage. Schema updates are versioned. Startup creates
missing tables and never drops existing rows.
"""

from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, Engine, Integer, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

CURRENT_SCHEMA_VERSION = 20

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    """Applied schema versions. Rows are inserted and never deleted."""

    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            kwargs["poolclass"] = NullPool
        _engine = create_engine(url, **kwargs)
        _ensure_sqlite_directory(_engine)
        _register_sqlite_pragmas(_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database() -> None:
    """Create missing tables and stamp the schema version.

    Safe to call on every process start. Existing agent rows are left in place.
    """
    from app.agents import models as agent_models
    from app.conversations import models as conversation_models
    from app.files import models as file_models
    from app.memories import models as memory_models
    from app.approvals import models as approval_models
    from app.permissions import models as permission_models
    from app.core import controls
    from app.agents import team
    from app.projects import core as project_core
    from app.tools import models as tool_models

    _ = (
        agent_models.AgentRecord,
        conversation_models.ConversationRecord,
        conversation_models.MessageRecord,
        memory_models.MemoryRecord,
        memory_models.PendingMemoryActionRecord,
        tool_models.AgentToolGrantRecord,
        file_models.WorkspaceRecord,
        file_models.AgentWorkspaceGrantRecord,
        permission_models.PermissionPolicyRecord,
        permission_models.ToolSettingRecord,
        permission_models.AgentPathGrantRecord,
        permission_models.CapabilityPreferenceRecord,
        permission_models.PermissionAuditRecord,
        permission_models.StandingApprovalRecord,
        controls.AssistantControlRecord,
        team.AgentTeamMemberRecord,
        project_core.ProjectRecord,
        project_core.ProjectEntryRecord,
        approval_models.ApprovalRequestRecord,
    )
    from app.approvals.payloads import McpExecutionPayloadRecord

    _ = (
        McpExecutionPayloadRecord,
        approval_models.ApprovalRequestRecord,
        file_models.FileRecoveryRecord,
        file_models.FileOperationJournalRecord,
    )
    engine = get_engine()
    from app.knowledge.schema import ensure_knowledge_schema
    from app.mcp.schema import ensure_mcp_schema

    ensure_knowledge_schema(engine)
    ensure_mcp_schema(engine)
    Base.metadata.create_all(bind=engine)
    _ensure_schema_version(engine)
    from app.permissions.repository import ensure_permission_policy

    ensure_permission_policy(engine)


def reset_database() -> None:
    """Drop the cached engine so the next access opens a new connection."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def _ensure_sqlite_directory(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    database = engine.url.database
    if not database or database == ":memory:":
        return
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _register_sqlite_pragmas(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _ensure_schema_version(engine: Engine) -> None:
    with Session(engine) as session:
        versions = list(session.scalars(select(SchemaVersion.version)))
        if not versions:
            session.add(
                SchemaVersion(
                    version=CURRENT_SCHEMA_VERSION,
                    applied_at=datetime.now(timezone.utc),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
            return
        latest = max(versions)

    if latest > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {latest} is newer than this application "
            f"({CURRENT_SCHEMA_VERSION}). Startup stopped so existing data would not be modified."
        )
    if latest < CURRENT_SCHEMA_VERSION:
        _apply_pending_migrations(engine, latest)


def _apply_pending_migrations(engine: Engine, latest: int) -> None:
    """Apply registered upgrades. Agent rows are never deleted here."""
    from app.core.migrations import apply_migration

    version = latest
    while version < CURRENT_SCHEMA_VERSION:
        next_version = version + 1
        apply_migration(engine, next_version)
        with Session(engine) as session:
            session.add(
                SchemaVersion(
                    version=next_version,
                    applied_at=datetime.now(timezone.utc),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
        version = next_version
