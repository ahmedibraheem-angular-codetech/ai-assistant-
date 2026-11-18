"""Durable journal for one file change.

A database commit and a filesystem rename are not one transaction. Each state
is committed before the next destructive step. Startup reconciles anything
left unfinished. It does not overwrite a file it cannot identify.
"""

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import get_session_factory
from app.files.exceptions import FileAccessError
from app.files.models import FileOperationJournalRecord, FileRecoveryRecord

PREPARED = "PREPARED"
BACKUP_READY = "BACKUP_READY"
APPLYING = "APPLYING"
APPLIED = "APPLIED"
COMMITTED = "COMMITTED"
ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
RECOVERED = "RECOVERED"
FAILED = "FAILED"

_TERMINAL = frozenset({COMMITTED, ROLLBACK_REQUIRED, RECOVERED, FAILED})
_OPEN = frozenset({PREPARED, BACKUP_READY, APPLYING, APPLIED})


class JournalInterrupted(Exception):
    """A test asked the change to stop after a durable step."""


def crash_point(stage: str) -> None:
    """Stop after a named step when a test sets ASSISTANT_FILE_JOURNAL_CRASH.

    The worker process exits immediately, so later cleanup cannot hide the crash.
    """
    if os.environ.get("ASSISTANT_FILE_JOURNAL_CRASH") == stage:
        os._exit(86)


def begin_journal(
    *,
    workspace_id: str,
    relative_path: str,
    operation: str,
    original_sha256: str,
    proposed_sha256: str,
    approval_id: str | None = None,
) -> str:
    if not isinstance(approval_id, str) or not approval_id:
        from app.files.authorization import _APPROVAL

        bound = _APPROVAL.get()
        approval_id = bound if isinstance(bound, str) and bound else None
    now = datetime.now(timezone.utc)
    journal_id = str(uuid4())
    session = get_session_factory()()
    try:
        session.add(
            FileOperationJournalRecord(
                id=journal_id,
                approval_id=approval_id,
                workspace_id=workspace_id,
                relative_path=relative_path,
                operation=operation,
                original_sha256=original_sha256,
                proposed_sha256=proposed_sha256,
                state=PREPARED,
                recovery_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return journal_id


def mark_journal(journal_id: str, state: str, recovery_id: str | None = None) -> None:
    session = get_session_factory()()
    try:
        row = session.get(FileOperationJournalRecord, journal_id)
        if row is None:
            raise FileAccessError("The file change journal was not found.")
        row.state = state
        row.updated_at = datetime.now(timezone.utc)
        if recovery_id is not None:
            row.recovery_id = recovery_id
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def backup_file(journal_id: str) -> Path:
    from app.files.changes import _recovery_dir

    return _recovery_dir() / journal_id


def write_backup(journal_id: str, payload: bytes) -> None:
    path = backup_file(journal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def backup_matches(journal_id: str, expected: str) -> bool:
    path = backup_file(journal_id)
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    return bool(expected) and hashlib.sha256(payload).hexdigest() == expected


def finalize_journal(
    journal_id: str,
    *,
    workspace_id: str,
    relative_path: str,
    operation: str,
    content_sha256: str,
    size_bytes: int,
    replaced_sha256: str | None,
    keep_recovery: bool,
) -> None:
    """Publish a recovery row and mark the journal committed in one commit."""
    session = get_session_factory()()
    try:
        row = session.get(FileOperationJournalRecord, journal_id)
        if row is None:
            raise FileAccessError("The file change journal was not found.")
        if row.state == COMMITTED:
            return
        if keep_recovery:
            if not backup_matches(journal_id, content_sha256):
                raise FileAccessError("The recovery backup does not match its hash.")
            session.add(
                FileRecoveryRecord(
                    id=journal_id,
                    workspace_id=workspace_id,
                    relative_path=relative_path,
                    operation=operation,
                    content_sha256=content_sha256,
                    size_bytes=size_bytes,
                    replaced_sha256=replaced_sha256,
                    status="ready",
                    created_at=datetime.now(timezone.utc),
                    restored_at=None,
                )
            )
            row.recovery_id = journal_id
        row.state = COMMITTED
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    except IntegrityError:
        session.rollback()
        current = session.get(FileOperationJournalRecord, journal_id)
        if current is not None and current.state == COMMITTED:
            return
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reconcile_file_journal() -> list[str]:
    """Finish or close every open journal entry. Safe to call more than once."""
    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(FileOperationJournalRecord).where(FileOperationJournalRecord.state.in_(_OPEN))
            ).all()
        )
        identifiers = [row.id for row in rows]
    finally:
        session.close()
    for journal_id in identifiers:
        _reconcile_one(journal_id)
    return identifiers


def _reconcile_one(journal_id: str) -> None:
    session = get_session_factory()()
    try:
        row = session.get(FileOperationJournalRecord, journal_id)
        if row is None or row.state in _TERMINAL:
            return
        state = row.state
        workspace_id = row.workspace_id
        relative_path = row.relative_path
        operation = row.operation
        original = row.original_sha256
        proposed = row.proposed_sha256
    finally:
        session.close()
    target = _target_hash(workspace_id, relative_path)
    backup_ok = backup_matches(journal_id, original) if original else False
    if target == "unreadable":
        mark_journal(journal_id, ROLLBACK_REQUIRED)
        return
    if operation == "delete":
        _reconcile_delete(journal_id, state, target, original, backup_ok, workspace_id, relative_path)
        return
    if original:
        _reconcile_update(
            journal_id,
            state,
            target,
            original,
            proposed,
            backup_ok,
            workspace_id,
            relative_path,
        )
        return
    _reconcile_create(journal_id, target, proposed, workspace_id, relative_path)


def _reconcile_create(
    journal_id: str,
    target: str | None,
    proposed: str,
    workspace_id: str,
    relative_path: str,
) -> None:
    if target is None:
        _clear_temps(workspace_id, relative_path)
        mark_journal(journal_id, FAILED)
        return
    if target == proposed:
        mark_journal(journal_id, COMMITTED)
        return
    mark_journal(journal_id, ROLLBACK_REQUIRED)


def _reconcile_delete(
    journal_id: str,
    state: str,
    target: str | None,
    original: str,
    backup_ok: bool,
    workspace_id: str,
    relative_path: str,
) -> None:
    if target == original and state in {PREPARED, BACKUP_READY, APPLYING, APPLIED}:
        _clear_temps(workspace_id, relative_path)
        mark_journal(journal_id, FAILED)
        return
    if target is None and backup_ok and state in {APPLYING, APPLIED, BACKUP_READY}:
        size = backup_file(journal_id).stat().st_size
        finalize_journal(
            journal_id,
            workspace_id=workspace_id,
            relative_path=relative_path,
            operation="delete",
            content_sha256=original,
            size_bytes=size,
            replaced_sha256=None,
            keep_recovery=True,
        )
        return
    if target is None and state == PREPARED:
        mark_journal(journal_id, ROLLBACK_REQUIRED)
        return
    mark_journal(journal_id, ROLLBACK_REQUIRED)


def _reconcile_update(
    journal_id: str,
    state: str,
    target: str | None,
    original: str,
    proposed: str,
    backup_ok: bool,
    workspace_id: str,
    relative_path: str,
) -> None:
    if target == original:
        _clear_temps(workspace_id, relative_path)
        mark_journal(journal_id, FAILED)
        return
    if target == proposed and backup_ok:
        size = backup_file(journal_id).stat().st_size
        finalize_journal(
            journal_id,
            workspace_id=workspace_id,
            relative_path=relative_path,
            operation="update",
            content_sha256=original,
            size_bytes=size,
            replaced_sha256=proposed,
            keep_recovery=True,
        )
        return
    if target is None and backup_ok:
        _restore_original(workspace_id, relative_path, journal_id)
        mark_journal(journal_id, RECOVERED)
        return
    mark_journal(journal_id, ROLLBACK_REQUIRED)


def _clear_temps(workspace_id: str, relative_path: str) -> None:
    """Remove an unreplaced temporary write. The original file is left untouched."""
    from app.files.changes import _parent
    from app.files.operations import _load_root

    try:
        root = _load_root(workspace_id)
        root_fd, directory = _parent(root, relative_path)
    except (OSError, FileAccessError):
        return
    try:
        for name in os.listdir(directory):
            if name.startswith(".") and name.endswith(".assistant-tmp"):
                try:
                    os.unlink(name, dir_fd=directory)
                except OSError:
                    continue
    finally:
        if directory != root_fd:
            os.close(directory)
        os.close(root_fd)


def _restore_original(workspace_id: str, relative_path: str, journal_id: str) -> None:
    from app.files.changes import _parent, _write_dir_file
    from app.files.access import relative_parts
    from app.files.operations import _load_root

    payload = backup_file(journal_id).read_bytes()
    root = _load_root(workspace_id)
    root_fd, directory = _parent(root, relative_path)
    name = relative_parts(relative_path)[-1]
    temporary = f".{journal_id}.assistant-tmp"
    try:
        _write_dir_file(directory, temporary, payload)
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if directory != root_fd:
            os.close(directory)
        os.close(root_fd)


def _target_hash(workspace_id: str, relative_path: str) -> str | None:
    """Return the file hash, None when the file is absent, or 'unreadable'."""
    from app.files.changes import _read_existing
    from app.files.operations import _load_root

    try:
        root = _load_root(workspace_id)
        payload = _read_existing(root, relative_path)
    except FileAccessError as exc:
        if "not found" in str(exc).casefold():
            return None
        return "unreadable"
    except OSError:
        return "unreadable"
    return hashlib.sha256(payload).hexdigest()
