"""Load the operator MCP file and publish one global revision.

The loader reads mcp/servers.json. It does not rewrite that file, spawn a
process, or expose environment values. A snapshot is active only when the
file bytes, the committed row, and the published snapshot all name the same
revision.
"""

import hashlib
import json
import logging
import os
import signal
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

_MAX_BYTES = 65_536
_MAX_SERVERS = 4
_MAX_ARGS = 16
_MAX_ENV = 16
_GLOBAL = "global"
_MESSAGE = "The MCP configuration is not valid."
_ROOT_KEYS = frozenset({"executable_roots", "servers"})
_SERVER_KEYS = frozenset(
    {
        "id",
        "display_name",
        "enabled",
        "executable",
        "args",
        "env",
        "agent_ids",
        "tool_names",
    }
)
_OPTIONAL_SERVER_KEYS = frozenset({"resource_uris"})
_MACHO = frozenset(
    {
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
)
logger = logging.getLogger(__name__)
_lock = threading.Lock()
_snapshot: "ConfigSnapshot | None" = None
_fault_stage: str | None = None
_fault_callback = None
_permit_hook = None


class ConfigError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_MESSAGE)


@dataclass(frozen=True, slots=True)
class ServerConfig:
    id: str
    display_name: str
    enabled: bool
    executable: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    agent_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    resource_uris: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"ServerConfig(id={self.id!r}, enabled={self.enabled!r})"


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    revision: str
    epoch: int
    roots: tuple[str, ...]
    servers: tuple[ServerConfig, ...]

    def __repr__(self) -> str:
        return f"ConfigSnapshot(revision={self.revision!r}, epoch={self.epoch!r}, servers={len(self.servers)})"


@dataclass(frozen=True, slots=True)
class ReloadResult:
    published: bool
    reason: str


@dataclass(frozen=True, slots=True)
class McpStatus:
    available: bool
    reason: str
    revision: str | None
    epoch: int | None


@dataclass(frozen=True, slots=True)
class _Row:
    revision: str
    epoch: int


def config_path() -> Path:
    from app.approvals.fingerprint import _key_path

    return _key_path().parent / "mcp" / "servers.json"


def set_reload_fault(stage: str | None, callback=None) -> None:
    """Test hook. Production calls leave the stage empty."""
    global _fault_stage, _fault_callback
    _fault_stage = stage
    _fault_callback = callback


def clear_reload_fault() -> None:
    set_reload_fault(None, None)


_UNINSTALLED = object()


def install_sighup():
    """Install SIGHUP on the main thread. Other threads keep the explicit reload."""
    if not hasattr(signal, "SIGHUP") or threading.current_thread() is not threading.main_thread():
        return _UNINSTALLED
    previous = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, handle_sighup)
    return previous


def restore_sighup(previous) -> None:
    if previous is _UNINSTALLED or threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGHUP, previous)


def handle_sighup(_signum, _frame) -> None:
    try:
        reload_configuration()
    except Exception:
        logger.warning("The MCP configuration reload failed.")


_epoch_listener = None


def set_epoch_listener(callback) -> None:
    """Register the session closer. It runs only after a new epoch is published."""
    global _epoch_listener
    _epoch_listener = callback


def blocked_location(path: str) -> bool:
    """True when a path is outside the approved executable location rules."""
    return _forbidden_location(path)


def recover_configuration() -> McpStatus:
    """Publish a committed revision when the file still matches it.

    A missing row, a different file, or a file that no longer validates leaves
    the snapshot unpublished. The row is not rewritten.
    """
    global _snapshot
    with _lock:
        row = _read_row()
        if row is None:
            _snapshot = None
            return McpStatus(False, "not_loaded", None, None)
        try:
            raw = _read_bytes()
            digest = _digest(raw)
            if digest != row.revision:
                _keep_committed_snapshot(row)
                return McpStatus(False, "mismatch", None, None)
            parsed = _validate(raw)
        except ConfigError:
            _keep_committed_snapshot(row)
            _warn_if_present()
            return McpStatus(False, "unavailable", None, None)
        _snapshot = ConfigSnapshot(row.revision, row.epoch, parsed.roots, parsed.servers)
        return McpStatus(True, "ok", row.revision, row.epoch)


def reload_configuration() -> ReloadResult:
    """Validate one candidate, commit that revision, then publish it."""
    result, new_epoch = _reload_under_lock()
    if new_epoch is not None:
        listener = _epoch_listener
        if listener is not None:
            try:
                listener(new_epoch)
            except Exception:
                logger.warning("The MCP session shutdown failed.")
    return result


def _reload_under_lock() -> tuple[ReloadResult, int | None]:
    """Commit and publish while the loader lock is held. The epoch is returned only for a new commit."""
    global _snapshot
    with _lock:
        _hit("lock_acquired")
        try:
            raw = _read_bytes()
        except ConfigError:
            _warn_if_present()
            return ReloadResult(False, "unreadable"), None
        digest = _digest(raw)
        try:
            parsed = _validate(raw)
        except ConfigError:
            _warn_if_present()
            return ReloadResult(False, "invalid"), None
        row = _read_row()
        if row is not None and row.revision == digest:
            _hit("before_reread")
            try:
                if _digest(_read_bytes()) != digest:
                    return ReloadResult(False, "mismatch"), None
            except ConfigError:
                return ReloadResult(False, "unreadable"), None
            _snapshot = ConfigSnapshot(digest, row.epoch, parsed.roots, parsed.servers)
            return ReloadResult(True, "unchanged"), None
        expected = row
        new_epoch = 1 if expected is None else expected.epoch + 1
        committed = False
        try:
            engine = _engine()
            with Session(engine) as session:
                with session.begin():
                    _hit("before_cas")
                    if expected is None:
                        inserted = session.execute(
                            text(
                                "INSERT INTO mcp_config_revisions (id, revision, epoch, loaded_at) "
                                "VALUES (:id, :revision, :epoch, :loaded_at)"
                            ),
                            {
                                "id": _GLOBAL,
                                "revision": digest,
                                "epoch": new_epoch,
                                "loaded_at": _now(),
                            },
                        )
                        if inserted.rowcount != 1:
                            raise ConfigError("conflict")
                    else:
                        updated = session.execute(
                            text(
                                "UPDATE mcp_config_revisions "
                                "SET revision = :revision, epoch = :epoch, loaded_at = :loaded_at "
                                "WHERE id = :id AND revision = :expected_revision AND epoch = :expected_epoch"
                            ),
                            {
                                "id": _GLOBAL,
                                "revision": digest,
                                "epoch": new_epoch,
                                "loaded_at": _now(),
                                "expected_revision": expected.revision,
                                "expected_epoch": expected.epoch,
                            },
                        )
                        if updated.rowcount != 1:
                            raise ConfigError("conflict")
                    _hit("before_reread")
                    try:
                        confirmed = _read_bytes()
                    except ConfigError as exc:
                        raise ConfigError("mismatch") from exc
                    if confirmed != raw:
                        raise ConfigError("mismatch")
                    _hit("before_commit")
            committed = True
            _hit("after_commit")
        except ConfigError as exc:
            if committed:
                return ReloadResult(False, "unpublished"), None
            return ReloadResult(False, exc.code if exc.code in {"conflict", "mismatch"} else "failed"), None
        except Exception:
            logger.warning("The MCP configuration reload failed.")
            return ReloadResult(False, "unpublished" if committed else "failed"), None
        _snapshot = ConfigSnapshot(digest, new_epoch, parsed.roots, parsed.servers)
        return ReloadResult(True, "ok"), new_epoch


def mcp_status() -> McpStatus:
    """Re-read the file. This does not publish a new revision."""
    with _lock:
        return _status_locked()


def _status_locked() -> McpStatus:
    """The publication check. The caller holds the loader lock."""
    published = _snapshot
    row = _read_row()
    if published is None or row is None:
        return McpStatus(False, "not_loaded", None, None)
    if published.revision != row.revision or published.epoch != row.epoch:
        return McpStatus(False, "mismatch", None, None)
    try:
        digest = _digest(_read_bytes())
    except ConfigError:
        return McpStatus(False, "unavailable", None, None)
    if digest != published.revision or digest != row.revision:
        return McpStatus(False, "mismatch", None, None)
    return McpStatus(True, "ok", published.revision, published.epoch)


class WritePermit:
    """Holds the loader lock from a matching publication check through release.

    current_snapshot is not dispatch authorization. The permit repeats the
    file, database, and published-snapshot checks while the lock is held and
    keeps that lock until the caller finishes the protected write.
    """

    def __init__(self) -> None:
        self._held = True

    @classmethod
    def acquire(cls, revision: str, epoch: int) -> "WritePermit":
        _lock.acquire()
        try:
            if not _publication_matches(revision, epoch):
                raise ConfigError("mismatch")
            hook = _permit_hook
            if hook is not None:
                hook()
            return cls()
        except BaseException:
            _lock.release()
            raise

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        _lock.release()


def set_permit_hook(callback) -> None:
    """Test hook. It runs while the permit holds the loader lock."""
    global _permit_hook
    _permit_hook = callback


def _publication_matches(revision: str, epoch: int) -> bool:
    status = _status_locked()
    published = _snapshot
    return bool(
        status.available
        and status.revision == revision
        and status.epoch == epoch
        and published is not None
        and published.revision == revision
        and published.epoch == epoch
    )


def current_snapshot() -> ConfigSnapshot | None:
    status = mcp_status()
    if not status.available:
        return None
    return _snapshot


def _keep_committed_snapshot(row: _Row) -> None:
    """Drop memory that does not match the committed row. Keep a matching one."""
    global _snapshot
    published = _snapshot
    if published is None or published.revision != row.revision or published.epoch != row.epoch:
        _snapshot = None


def _hit(stage: str) -> None:
    if _fault_stage != stage:
        return
    if _fault_callback is not None:
        _fault_callback()


def _engine():
    from app.core.database import get_engine

    return get_engine()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_row() -> _Row | None:
    with Session(_engine()) as session:
        found = session.execute(
            text("SELECT revision, epoch FROM mcp_config_revisions WHERE id = :id"),
            {"id": _GLOBAL},
        ).first()
    if found is None:
        return None
    return _Row(str(found[0]), int(found[1]))


def _read_bytes() -> bytes:
    path = config_path()
    directory = path.parent
    try:
        directory_stat = os.lstat(directory)
    except OSError as exc:
        raise ConfigError("unreadable") from exc
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise ConfigError("unreadable")
    if directory_stat.st_uid != os.getuid() or (directory_stat.st_mode & 0o777) != 0o700:
        raise ConfigError("unreadable")
    try:
        file_stat = os.lstat(path)
    except OSError as exc:
        raise ConfigError("unreadable") from exc
    if stat.S_ISLNK(file_stat.st_mode):
        raise ConfigError("unreadable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigError("unreadable") from exc
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or (current.st_mode & 0o777) != 0o600
            or current.st_size > _MAX_BYTES
        ):
            raise ConfigError("unreadable")
        raw = os.read(descriptor, current.st_size)
        if len(raw) != current.st_size or os.read(descriptor, 1):
            raise ConfigError("unreadable")
        return raw
    finally:
        os.close(descriptor)


def _validate(raw: bytes) -> ConfigSnapshot:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ConfigError("invalid_json") from None
    if not isinstance(parsed, dict) or set(parsed) != _ROOT_KEYS:
        raise ConfigError("unsupported" if isinstance(parsed, dict) and not set(parsed) <= _ROOT_KEYS else "invalid_schema")
    roots = _roots(parsed["executable_roots"])
    servers = _servers(parsed["servers"], roots)
    return ConfigSnapshot("", 0, roots, servers)


def _roots(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_SERVERS:
        raise ConfigError("invalid_schema")
    found: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.startswith("/") or "\x00" in item:
            raise ConfigError("invalid_schema")
        path = os.path.normpath(item)
        if path in found:
            raise ConfigError("invalid_schema")
        _directory_is_trusted(path)
        if _forbidden_location(path):
            raise ConfigError("unsupported")
        found.append(path)
    return tuple(found)


def _servers(value: object, roots: tuple[str, ...]) -> tuple[ServerConfig, ...]:
    if not isinstance(value, list) or len(value) > _MAX_SERVERS:
        raise ConfigError("invalid_schema")
    if value and not roots:
        raise ConfigError("invalid_schema")
    found: list[ServerConfig] = []
    seen: set[str] = set()
    omitted_resources = False
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("invalid_schema")
        keys = set(item)
        if not _SERVER_KEYS <= keys:
            raise ConfigError("invalid_schema")
        if keys - _SERVER_KEYS - _OPTIONAL_SERVER_KEYS:
            raise ConfigError("unsupported")
        if "resource_uris" not in keys:
            omitted_resources = True
        server = _server(item, roots)
        if server.id in seen:
            raise ConfigError("invalid_schema")
        seen.add(server.id)
        found.append(server)
    if omitted_resources:
        logger.warning("MCP resource reads are not authorized until resource_uris is set.")
    return tuple(found)


def _server(item: dict, roots: tuple[str, ...]) -> ServerConfig:
    identifier = item["id"]
    display = item["display_name"]
    enabled = item["enabled"]
    executable = item["executable"]
    if (
        not isinstance(identifier, str)
        or not _identifier(identifier)
        or not isinstance(display, str)
        or not display.strip()
        or len(display) > 80
        or any(character.isascii() and ord(character) < 32 for character in display)
        or not isinstance(enabled, bool)
        or not isinstance(executable, str)
    ):
        raise ConfigError("invalid_schema")
    path = os.path.normpath(executable)
    if not path.startswith("/") or not any(path == root or path.startswith(root + "/") for root in roots):
        raise ConfigError("unsupported")
    if _forbidden_location(path):
        raise ConfigError("unsupported")
    _regular_executable(path)
    return ServerConfig(
        identifier,
        display.strip(),
        enabled,
        path,
        _arguments(item["args"]),
        _environment(item["env"]),
        _agents(item["agent_ids"]),
        _tools(item["tool_names"]),
        _resources(item.get("resource_uris", [])),
    )


def _arguments(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ARGS:
        raise ConfigError("invalid_schema")
    found: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 256 or "\x00" in item:
            raise ConfigError("invalid_schema")
        found.append(item)
    return tuple(found)


def _environment(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or len(value) > _MAX_ENV:
        raise ConfigError("invalid_schema")
    found: list[tuple[str, str]] = []
    for key in sorted(value):
        item = value[key]
        if (
            not isinstance(key, str)
            or not _env_name(key)
            or not isinstance(item, str)
            or len(item) > 1024
            or "\x00" in item
        ):
            raise ConfigError("invalid_schema")
        found.append((key, item))
    return tuple(found)


def _agents(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise ConfigError("invalid_schema")
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError("invalid_schema")
        try:
            parsed = str(UUID(item))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConfigError("invalid_schema") from exc
        if parsed in found:
            raise ConfigError("invalid_schema")
        found.append(parsed)
    return tuple(found)


def _resources(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ConfigError("invalid_schema")
    found: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 400 or "\x00" in item or item in found:
            raise ConfigError("invalid_schema")
        found.append(item)
    return tuple(found)


def _tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ConfigError("invalid_schema")
    found: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _identifier(item) or item in found:
            raise ConfigError("invalid_schema")
        found.append(item)
    return tuple(found)


def _identifier(value: str) -> bool:
    if not value or len(value) > 64 or not value[0].islower() or not value[0].isascii():
        return False
    return all(character.islower() or character.isdigit() or character in {"_", "-"} for character in value)


def _env_name(value: str) -> bool:
    if not value or len(value) > 64 or not (value[0].isalpha() or value[0] == "_"):
        return False
    return all(character.isalnum() or character == "_" for character in value)


def _directory_is_trusted(path: str) -> None:
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise ConfigError("invalid_schema") from exc
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise ConfigError("unsupported")
    if current.st_mode & 0o022 or current.st_uid not in {os.getuid(), 0}:
        raise ConfigError("unsupported")


def _regular_executable(path: str) -> None:
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise ConfigError("invalid_schema") from exc
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or current.st_mode & (stat.S_ISUID | stat.S_ISGID | 0o022)
        or not current.st_mode & stat.S_IXUSR
        or current.st_uid not in {os.getuid(), 0}
    ):
        raise ConfigError("unsupported")
    _trusted_parents(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigError("unsupported") from exc
    try:
        header = os.read(descriptor, 4)
    finally:
        os.close(descriptor)
    if header.startswith(b"#!") or not _native_header(header):
        raise ConfigError("unsupported")


def _trusted_parents(path: str) -> None:
    """Reject ancestors that an untrusted user could use to replace the executable."""
    from app.mcp.process import parent_entry_trusted

    current = Path(os.path.abspath(path)).parent
    owner = os.getuid()
    for _ in range(64):
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise ConfigError("unsupported") from exc
        if not parent_entry_trusted(info, owner):
            raise ConfigError("unsupported")
        if current == current.parent:
            return
        current = current.parent
    raise ConfigError("unsupported")


def _native_header(header: bytes) -> bool:
    if sys.platform == "darwin":
        return header in _MACHO
    if sys.platform.startswith("linux"):
        return header == b"\x7fELF"
    return False


def _forbidden_location(path: str) -> bool:
    from app.approvals.fingerprint import _key_path

    candidates = [os.path.normpath(path)]
    blocked = {
        os.path.normpath(tempfile.gettempdir()),
        "/tmp",
        "/var/tmp",
        "/private/tmp",
        os.path.normpath(str(_key_path().parent)),
    }
    for root in list(blocked):
        try:
            blocked.add(os.path.realpath(root))
        except OSError:
            continue
    for workspace in _workspace_roots():
        blocked.add(os.path.normpath(workspace))
    for candidate in candidates:
        for root in blocked:
            if candidate == root or candidate.startswith(root.rstrip("/") + "/"):
                return True
    return False


def _warn_if_present() -> None:
    path = config_path()
    directory = path.parent
    if path.is_symlink() or path.exists() or directory.is_symlink() or directory.exists():
        logger.warning("The MCP configuration was rejected.")


def _workspace_roots() -> list[str]:
    try:
        with Session(_engine()) as session:
            rows = session.execute(text("SELECT path FROM workspaces")).scalars().all()
    except Exception:
        return []
    return [row for row in rows if isinstance(row, str)]
