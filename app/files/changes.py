"""Controlled text create, update, and quarantine.

The workspace registration stays read-only. A change also needs an explicit
path grant, an approval bound to the content digest, and an EXECUTING row.
Bytes for a proposal live outside every workspace. Approval rows store the
digest, not the text.
"""

import difflib
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.approvals.fingerprint import bind_arguments, content_digest
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.files.access import (
    ApprovedRoot,
    _open_root,
    _raise_file_os,
    _walk_directories,
    metadata,
    open_file,
    read_fd,
    relative_parts,
)
from app.files.authorization import authorize
from app.files.exceptions import FileAccessError
from app.files.models import FileRecoveryRecord
from app.files.operations import _load_root
from app.permissions.service import explicit_file_grant
from app.tools.exceptions import ToolValidationError

MUTATION_TOOLS = {
    "create_text_file": "create",
    "update_text_file": "write",
    "quarantine_file": "delete",
}
TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".csv",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".html",
        ".css",
        ".yml",
        ".yaml",
        ".xml",
        ".sql",
        ".java",
        ".php",
    }
)
_BLOCKED_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "credentials.json",
        "secrets.json",
        "id_rsa",
        "approval.key",
        "control_plane.token",
        "control_plane.lock",
        "assistant.db",
    }
)
_BLOCKED_SUFFIXES = frozenset({".pem", ".key", ".sqlite", ".db", ".p12", ".pfx", ".kdbx"})
_CHANGED = "The file changed after approval."
_EXISTS = "That file already exists."
_PROTECTED = "That file is protected."
_UNSUPPORTED = "That file type cannot be changed."
_TOO_LARGE = "That file is too large to change."
_BACKUP = "Recovery storage is full."


@dataclass(frozen=True, slots=True)
class StagedChange:
    arguments: dict
    payload: bytes | None


def inspect_change(tool_name: str, agent_id: str | None, arguments: dict) -> StagedChange:
    """Validate a change and return digest-only arguments. No file is written."""
    if tool_name in {"ingest_document", "reindex_document", "delete_document"}:
        from app.knowledge.stage import stage_knowledge

        return stage_knowledge(tool_name, agent_id, arguments)
    if tool_name not in MUTATION_TOOLS:
        return StagedChange(bind_arguments(arguments), None)
    action = MUTATION_TOOLS[tool_name]
    root = _load_root(arguments.get("workspace_id"))
    if _protected_workspace(root):
        raise FileAccessError(_PROTECTED)
    relative = arguments.get("relative_path")
    if not isinstance(relative, str):
        raise ToolValidationError("Tool argument 'relative_path' must be a string.")
    parts = relative_parts(relative)
    _reject_sensitive(parts)
    normalized = "/".join(parts)
    if not explicit_file_grant(agent_id, arguments.get("workspace_id"), normalized, action):
        raise FileAccessError("File changes are not permitted.")
    if action == "delete":
        suffix = Path(parts[-1]).suffix.lower()
        if suffix not in TEXT_EXTENSIONS:
            raise FileAccessError(_UNSUPPORTED)
        current = _read_existing(root, normalized)
        if b"\x00" in current:
            raise FileAccessError(_UNSUPPORTED)
        canonical = bind_arguments(
            {
                "workspace_id": arguments["workspace_id"],
                "relative_path": normalized,
                "operation": "delete",
                "baseline_sha256": hashlib.sha256(current).hexdigest(),
                "size_bytes": len(current),
            }
        )
        return StagedChange(canonical, None)
    payload = _text_payload(arguments.get("content"), parts[-1])
    overwrite = bool(arguments.get("overwrite"))
    baseline = None
    try:
        current = _read_existing(root, normalized)
    except FileAccessError as exc:
        if "not found" not in str(exc).casefold():
            raise
        current = None
    if current is not None and action == "create" and not overwrite:
        raise FileAccessError(_EXISTS)
    if current is not None and overwrite:
        if not explicit_file_grant(agent_id, arguments.get("workspace_id"), normalized, "write"):
            raise FileAccessError("File changes are not permitted.")
        baseline = hashlib.sha256(current).hexdigest()
    if action == "write":
        if current is None:
            raise FileAccessError("That file was not found.")
        baseline = hashlib.sha256(current).hexdigest()
    canonical = bind_arguments(
        {
            "workspace_id": arguments["workspace_id"],
            "relative_path": normalized,
            "operation": "create" if action == "create" else "update",
            "overwrite": overwrite,
            "baseline_sha256": baseline or "",
            "content": payload.decode("utf-8"),
            "size_bytes": len(payload),
        }
    )
    return StagedChange(canonical, payload)


def write_proposal(approval_id: str, payload: bytes) -> None:
    path = _proposal_path(approval_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def discard_proposal(approval_id: str) -> None:
    path = _proposal_path(approval_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def apply_change(arguments: dict) -> dict:
    """Run one approved change inside the worker. The original stays on failure."""
    from app.files.operations import _pause_for_tests

    operation = arguments.get("operation")
    workspace_id = arguments.get("workspace_id")
    relative = arguments.get("relative_path")
    action = {"create": "create", "update": "write", "delete": "delete"}.get(operation)
    if action is None or not isinstance(relative, str):
        raise FileAccessError("File changes are not permitted.")
    authorize(action, workspace_id, relative, arguments)
    _pause_for_tests()
    authorize(action, workspace_id, relative, arguments)
    root = _load_root(workspace_id)
    if _protected_workspace(root):
        raise FileAccessError(_PROTECTED)
    parts = relative_parts(relative)
    _reject_sensitive(parts)
    if operation == "delete":
        result = _quarantine(root, arguments)
    else:
        result = _replace(root, arguments)
    _audit_file("file_change_completed", arguments, result.get("content_sha256", ""))
    return result


def describe_change(arguments: dict, approval_id: str) -> dict | None:
    operation = arguments.get("operation")
    if operation not in {"create", "update", "delete"}:
        return None
    relative = arguments.get("relative_path") if isinstance(arguments.get("relative_path"), str) else ""
    size = arguments.get("size_bytes") if isinstance(arguments.get("size_bytes"), int) else 0
    diff = ""
    if operation == "delete":
        diff = f"Move {relative} to recovery. Size {size} bytes."
    else:
        proposed = _proposal_text(approval_id)
        original = ""
        if operation == "update":
            original = _optional_text(arguments.get("workspace_id"), relative)
        diff = _diff(relative, original, proposed)
    return {
        "operation": operation,
        "workspace_id": arguments.get("workspace_id"),
        "relative_path": relative,
        "size_bytes": size,
        "baseline_sha256": arguments.get("baseline_sha256") or "",
        "content_sha256": _nested_digest(arguments.get("content")),
        "diff": diff,
    }


def list_recoveries() -> list[dict]:
    from sqlalchemy import select

    session = get_session_factory()()
    try:
        from app.files.models import FileOperationJournalRecord

        rows = list(
            session.scalars(select(FileRecoveryRecord).order_by(FileRecoveryRecord.created_at.desc())).all()
        )
        journals = {
            item.id: item.state
            for item in session.scalars(select(FileOperationJournalRecord)).all()
        }
        visible = []
        for row in rows:
            if row.status != "ready":
                continue
            state = journals.get(row.id)
            if state is not None and state != "COMMITTED":
                continue
            visible.append(_public_recovery(row))
        return visible
    finally:
        session.close()


def restore_recovery(recovery_id: str) -> dict:
    session = get_session_factory()()
    try:
        row = session.get(FileRecoveryRecord, recovery_id)
        if row is None or row.status != "ready":
            raise FileAccessError("That recovery record was not found.")
        payload = _recovery_path(row.id).read_bytes()
        if hashlib.sha256(payload).hexdigest() != row.content_sha256:
            raise FileAccessError("The recovery record does not match its file.")
        root = _load_root(row.workspace_id)
        if row.operation == "delete":
            _restore_missing(root, row.relative_path, payload)
        else:
            _restore_matching(root, row.relative_path, payload, row.replaced_sha256 or "")
        row.status = "restored"
        row.restored_at = datetime.now(timezone.utc)
        session.commit()
        _audit_file(
            "file_change_restored",
            {"operation": row.operation, "relative_path": row.relative_path, "workspace_id": row.workspace_id},
            row.content_sha256,
        )
        return _public_recovery(row)
    finally:
        session.close()


def _replace(root: ApprovedRoot, arguments: dict) -> dict:
    from app.files.journal import (
        APPLYING,
        JournalInterrupted,
        backup_matches,
        begin_journal,
        crash_point,
        finalize_journal,
        mark_journal,
        write_backup,
    )

    relative = arguments["relative_path"]
    expected = _nested_digest(arguments.get("content"))
    baseline = arguments.get("baseline_sha256") or ""
    payload = _proposal_bytes(expected)
    crash_point("before_journal")
    root_fd, directory = _parent(root, relative)
    name = relative_parts(relative)[-1]
    temporary = f".{uuid4().hex}.assistant-tmp"
    try:
        info = _lstat_file(directory, name)
        existing: bytes | None = None
        if info is None:
            if arguments.get("operation") != "create" or baseline:
                raise FileAccessError(_CHANGED if baseline else "That file was not found.")
        else:
            existing = _read_dir_file(directory, name)
            if hashlib.sha256(existing).hexdigest() != baseline:
                raise FileAccessError(_CHANGED)
        journal_id = begin_journal(
            workspace_id=str(arguments["workspace_id"]),
            relative_path=relative,
            operation=str(arguments["operation"]),
            original_sha256=baseline,
            proposed_sha256=expected,
        )
        crash_point("after_journal")
        if existing is not None:
            _reserve_backup(len(existing))
            write_backup(journal_id, existing)
            crash_point("after_backup")
            if not backup_matches(journal_id, baseline):
                mark_journal(journal_id, "FAILED")
                raise FileAccessError("The recovery backup does not match its hash.")
            mark_journal(journal_id, "BACKUP_READY")
            crash_point("after_backup_verified")
        mark_journal(journal_id, APPLYING)
        crash_point("after_intent")
        _write_dir_file(directory, temporary, payload)
        crash_point("during_temp")
        if existing is not None:
            again = _read_dir_file(directory, name)
            if hashlib.sha256(again).hexdigest() != baseline:
                raise FileAccessError(_CHANGED)
        crash_point("before_rename")
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        crash_point("after_rename")
        written = _read_dir_file(directory, name)
        if hashlib.sha256(written).hexdigest() != expected:
            raise FileAccessError("The written file did not match the approval.")
        mark_journal(journal_id, "APPLIED")
        crash_point("before_finalize")
        finalize_journal(
            journal_id,
            workspace_id=str(arguments["workspace_id"]),
            relative_path=relative,
            operation=str(arguments["operation"]),
            content_sha256=baseline,
            size_bytes=len(existing or b""),
            replaced_sha256=expected if existing is not None else None,
            keep_recovery=existing is not None,
        )
        crash_point("after_finalize")
        return {
            "operation": arguments["operation"],
            "relative_path": relative,
            "workspace_id": arguments["workspace_id"],
            "content_sha256": expected,
            "size_bytes": len(written),
            "changed": True,
        }
    except JournalInterrupted:
        _unlink_quiet(directory, temporary)
        raise
    except Exception:
        _unlink_quiet(directory, temporary)
        raise
    finally:
        if directory != root_fd:
            os.close(directory)
        os.close(root_fd)


def _rollback_file(directory: int, name: str, temporary: str, existing: bytes | None) -> None:
    if existing is None:
        _unlink_quiet(directory, name)
        return
    try:
        _write_dir_file(directory, temporary, existing)
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except OSError:
        _unlink_quiet(directory, temporary)


def _quarantine(root: ApprovedRoot, arguments: dict) -> dict:
    from app.files.journal import (
        JournalInterrupted,
        backup_matches,
        begin_journal,
        crash_point,
        finalize_journal,
        mark_journal,
        write_backup,
    )

    relative = arguments["relative_path"]
    baseline = arguments.get("baseline_sha256") or ""
    root_fd, directory = _parent(root, relative)
    name = relative_parts(relative)[-1]
    try:
        crash_point("before_journal")
        existing = _read_dir_file(directory, name)
        if hashlib.sha256(existing).hexdigest() != baseline:
            raise FileAccessError(_CHANGED)
        journal_id = begin_journal(
            workspace_id=str(arguments["workspace_id"]),
            relative_path=relative,
            operation="delete",
            original_sha256=baseline,
            proposed_sha256="",
        )
        crash_point("after_journal")
        _reserve_backup(len(existing))
        write_backup(journal_id, existing)
        crash_point("after_backup")
        if not backup_matches(journal_id, baseline):
            mark_journal(journal_id, "FAILED")
            raise FileAccessError("The recovery backup does not match its hash.")
        mark_journal(journal_id, "BACKUP_READY")
        crash_point("after_backup_verified")
        again = _read_dir_file(directory, name)
        if hashlib.sha256(again).hexdigest() != baseline:
            raise FileAccessError(_CHANGED)
        mark_journal(journal_id, "APPLYING")
        crash_point("after_intent")
        crash_point("during_temp")
        crash_point("before_rename")
        try:
            os.unlink(name, dir_fd=directory)
        except OSError as exc:
            _raise_file_os(exc)
            raise FileAccessError("That file could not be moved to recovery.") from exc
        os.fsync(directory)
        crash_point("after_rename")
        if _lstat_file(directory, name) is not None:
            raise FileAccessError("That file could not be moved to recovery.")
        mark_journal(journal_id, "APPLIED")
        crash_point("before_finalize")
        finalize_journal(
            journal_id,
            workspace_id=str(arguments["workspace_id"]),
            relative_path=relative,
            operation="delete",
            content_sha256=baseline,
            size_bytes=len(existing),
            replaced_sha256=None,
            keep_recovery=True,
        )
        crash_point("after_finalize")
        return {
            "operation": "delete",
            "relative_path": relative,
            "workspace_id": arguments["workspace_id"],
            "content_sha256": baseline,
            "size_bytes": len(existing),
            "changed": True,
            "recoverable": True,
        }
    except JournalInterrupted:
        raise
    finally:
        if directory != root_fd:
            os.close(directory)
        os.close(root_fd)


def _commit_recovery(arguments: dict, partial: Path, content_sha256: str, replaced: str | None) -> None:
    if not partial.exists():
        return
    recovery_id = str(uuid4())
    destination = _recovery_path(recovery_id)
    os.replace(partial, destination)
    session = get_session_factory()()
    try:
        session.add(
            FileRecoveryRecord(
                id=recovery_id,
                workspace_id=str(arguments["workspace_id"]),
                relative_path=str(arguments["relative_path"]),
                operation=str(arguments["operation"]),
                content_sha256=content_sha256,
                size_bytes=destination.stat().st_size,
                replaced_sha256=replaced,
                status="ready",
                created_at=datetime.now(timezone.utc),
                restored_at=None,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        destination.unlink(missing_ok=True)
        raise
    finally:
        session.close()


def _restore_missing(root: ApprovedRoot, relative: str, payload: bytes) -> None:
    parent = _parent(root, relative)
    root_fd, directory = parent
    name = relative_parts(relative)[-1]
    try:
        if _lstat_file(directory, name) is not None:
            raise FileAccessError("That file already exists.")
        temporary = f".{uuid4().hex}.assistant-tmp"
        _write_dir_file(directory, temporary, payload)
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if directory != root_fd:
            os.close(directory)
        os.close(root_fd)


def _restore_matching(root: ApprovedRoot, relative: str, payload: bytes, replaced_sha256: str) -> None:
    current = _read_existing(root, relative)
    if hashlib.sha256(current).hexdigest() != replaced_sha256:
        raise FileAccessError(_CHANGED)
    parent = _parent(root, relative)
    root_fd, directory = parent
    name = relative_parts(relative)[-1]
    temporary = f".{uuid4().hex}.assistant-tmp"
    try:
        _write_dir_file(directory, temporary, payload)
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except Exception:
        _unlink_quiet(directory, temporary)
        raise
    finally:
        if directory != root_fd:
            os.close(directory)
        os.close(root_fd)


def _parent(root: ApprovedRoot, relative: str) -> tuple[int, int]:
    parts = relative_parts(relative)
    root_fd = _open_root(root)
    if len(parts) == 1:
        return root_fd, root_fd
    try:
        directory = _walk_directories(root_fd, parts[:-1])
    except Exception:
        os.close(root_fd)
        raise
    return root_fd, directory


def _read_existing(root: ApprovedRoot, relative: str) -> bytes:
    descriptor, info, _normalized = open_file(root, relative)
    try:
        limit = get_settings().file_write_max_bytes
        return read_fd(descriptor, int(info.st_size), limit)
    finally:
        os.close(descriptor)


def _text_payload(value: object, filename: str) -> bytes:
    if not isinstance(value, str):
        raise ToolValidationError("Tool argument 'content' must be a string.")
    suffix = Path(filename).suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        raise FileAccessError(_UNSUPPORTED)
    if "\x00" in value:
        raise FileAccessError(_UNSUPPORTED)
    try:
        payload = value.encode("utf-8")
    except UnicodeError as exc:
        raise FileAccessError(_UNSUPPORTED) from exc
    if len(payload) > get_settings().file_write_max_bytes:
        raise FileAccessError(_TOO_LARGE)
    return payload


def _reject_sensitive(parts: tuple[str, ...]) -> None:
    for part in parts:
        lowered = part.casefold()
        if lowered in _BLOCKED_NAMES or lowered.startswith(".env"):
            raise FileAccessError(_PROTECTED)
        suffix = Path(part).suffix.lower()
        if suffix in _BLOCKED_SUFFIXES:
            raise FileAccessError(_PROTECTED)


def _protected_workspace(root: ApprovedRoot) -> bool:
    real = Path(root.path).resolve()
    app = Path(__file__).resolve().parents[2]
    data = app / "data"
    return real == app or app in real.parents or real == data or data in real.parents


def _proposal_path(approval_id: str) -> Path:
    return _storage_dir("file-proposals") / approval_id


def _recovery_path(recovery_id: str) -> Path:
    return _recovery_dir() / recovery_id


def _recovery_dir() -> Path:
    return _storage_dir("file-recovery")


def _storage_dir(name: str) -> Path:
    from app.approvals.fingerprint import _key_path

    directory = _key_path().parent / name
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        return directory
    return directory


def _proposal_bytes(expected: str) -> bytes:
    from app.files.authorization import _APPROVAL

    approval_id = _APPROVAL.get()
    if not isinstance(approval_id, str) or not approval_id:
        raise FileAccessError("File changes are not permitted.")
    path = _proposal_path(approval_id)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise FileAccessError("The approved content is no longer available.") from exc
    if hashlib.sha256(payload).hexdigest() != expected:
        raise FileAccessError(_CHANGED)
    return payload


def _proposal_text(approval_id: str) -> str:
    path = _proposal_path(approval_id)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _optional_text(workspace_id: object, relative: str) -> str:
    try:
        root = _load_root(workspace_id)
        return _read_existing(root, relative).decode("utf-8")
    except (FileAccessError, UnicodeError, ToolValidationError):
        return ""


def _diff(relative: str, original: str, proposed: str) -> str:
    lines = list(
        difflib.unified_diff(
            original.splitlines(),
            proposed.splitlines(),
            fromfile=relative,
            tofile=relative,
            lineterm="",
        )
    )
    text = "\n".join(lines[:80])
    return text[:4000]


def _nested_digest(value: object) -> str:
    if isinstance(value, dict) and isinstance(value.get("digest"), str):
        return value["digest"]
    if isinstance(value, str):
        return content_digest(value)
    return ""


def _lstat_file(directory: int, name: str):
    try:
        info = os.lstat(name, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _raise_file_os(exc)
        raise FileAccessError("That file was not found.") from exc
    if stat.S_ISLNK(info.st_mode):
        raise FileAccessError("Symlinks are not followed.")
    if not stat.S_ISREG(info.st_mode):
        raise FileAccessError("That path is not a file.")
    return info


def _read_dir_file(directory: int, name: str) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError as exc:
        _raise_file_os(exc)
        raise FileAccessError("That file was not found.") from exc
    try:
        info = os.fstat(descriptor)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise FileAccessError("That path is not a file.")
        return read_fd(descriptor, int(info.st_size), get_settings().file_write_max_bytes)
    finally:
        os.close(descriptor)


def _write_dir_file(directory: int, name: str, payload: bytes) -> None:
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
    except OSError as exc:
        _raise_file_os(exc)
        raise FileAccessError("That file could not be written.") from exc
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_outside(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_quiet(directory: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except OSError:
        return


def _reserve_backup(size: int) -> None:
    settings = get_settings()
    directory = _recovery_dir()
    files = [item for item in directory.iterdir() if item.is_file()]
    total = 0
    for item in files:
        try:
            total += item.stat().st_size
        except OSError:
            continue
    if len(files) >= settings.file_backup_max_count or total + size > settings.file_backup_max_bytes:
        raise FileAccessError(_BACKUP)


def _public_recovery(row: FileRecoveryRecord) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "relative_path": row.relative_path,
        "operation": row.operation,
        "size_bytes": row.size_bytes,
        "content_sha256": row.content_sha256,
        "status": row.status,
        "created_at": row.created_at,
        "restored_at": row.restored_at,
    }


def _audit_file(event: str, arguments: dict, digest: str) -> None:
    from app.permissions.audit import add_audit

    session = get_session_factory()()
    try:
        add_audit(
            session,
            event=event,
            detail=f"{arguments.get('operation', '')} {arguments.get('relative_path', '')} {digest[:16]}".strip(),
            tool_name=arguments.get("operation"),
            resource_type="file",
            resource_id=str(arguments.get("workspace_id") or "")[:200],
            action=str(arguments.get("operation") or ""),
        )
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def metadata_for(arguments: dict) -> dict:
    root = _load_root(arguments.get("workspace_id"))
    info = metadata(root, arguments.get("relative_path"))
    return {"metadata": info}
