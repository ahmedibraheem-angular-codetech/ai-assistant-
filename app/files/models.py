from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.agents.models import as_utc
from app.core.database import Base

READ_ONLY = "read_only"


@dataclass(frozen=True, slots=True)
class Workspace:
    id: str
    name: str
    root_path: str
    root_device: int
    root_inode: int
    access_scope: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class WorkspaceRecord(Base):
    """A directory the user explicitly approved for read-only access.

    Deleting a row removes the registration only. The directory is not touched.
    access_scope stays read_only. This table does not grant write or command access.
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    root_device: Mapped[int] = mapped_column(BigInteger, nullable=False)
    root_inode: Mapped[int] = mapped_column(BigInteger, nullable=False)
    access_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentWorkspaceGrantRecord(Base):
    """Workspaces one agent may read.

    A file-tool grant does not include any workspace. No rows means the agent
    cannot read registered files. Deleting the agent or the workspace removes
    these rows. This table does not grant write, delete, or command access.
    """

    __tablename__ = "agent_workspace_grants"

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
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class FileRecoveryRecord(Base):
    """A quarantined or overwritten file. The bytes live outside every workspace."""

    __tablename__ = "file_recoveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(400), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    replaced_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FileOperationJournalRecord(Base):
    """Durable intent for one file change. The row is committed before the next destructive step."""

    __tablename__ = "file_operation_journal"
    __table_args__ = (
        Index(
            "uq_file_journal_approval",
            "approval_id",
            unique=True,
            sqlite_where=text("approval_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(400), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    original_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    recovery_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def to_workspace(record: WorkspaceRecord) -> Workspace:
    return Workspace(
        id=record.id,
        name=record.name,
        root_path=record.root_path,
        root_device=int(record.root_device),
        root_inode=int(record.root_inode),
        access_scope=record.access_scope,
        enabled=bool(record.enabled),
        created_at=as_utc(record.created_at),
        updated_at=as_utc(record.updated_at),
    )
