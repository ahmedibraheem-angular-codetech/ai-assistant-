"""AES-256-GCM payloads and the atomic mcp-payload.key publication.

The approval HMAC key is not an input. A published key is never replaced.
Temporary names are never treated as keys.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select

_KEY_NAME = "mcp-payload.key"
_TEMP_PREFIX = "mcp-payload.key."
_TEMP_NAME = re.compile(r"^mcp-payload\.key\.\d+\.[0-9a-f]+$")
_stop_before_link = False


class PayloadError(Exception):
    def __init__(self) -> None:
        super().__init__("The MCP payload is not available.")


def set_stop_before_link(value: bool) -> None:
    """Test hook. A true value leaves the temporary file unpublished."""
    global _stop_before_link
    _stop_before_link = value


def load_payload_key() -> bytes:
    """Return the 32-byte payload key. Create it only when no payload row exists."""
    directory = _directory()
    cleanup_abandoned_payload_keys()
    descriptor = _open_directory(directory)
    try:
        if _payload_rows_exist():
            return _read_final(descriptor, _KEY_NAME)
        return _reuse_or_create(descriptor, _KEY_NAME)
    finally:
        os.close(descriptor)


def cleanup_abandoned_payload_keys() -> None:
    """Remove unpublished temporary files. The final key is never unlinked."""
    directory = _directory()
    try:
        descriptor = _open_directory(directory)
    except PayloadError:
        return
    try:
        for name in os.listdir(descriptor):
            if name == _KEY_NAME or _TEMP_NAME.fullmatch(name) is None:
                continue
            try:
                os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                continue
    finally:
        os.close(descriptor)


def seal_payload(plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    key = load_payload_key()
    nonce = os.urandom(12)
    try:
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    except Exception as exc:
        raise PayloadError() from exc
    return nonce, ciphertext


def open_payload(nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != 12:
        raise PayloadError()
    if not isinstance(ciphertext, (bytes, bytearray)) or len(ciphertext) < 16:
        raise PayloadError()
    key = load_payload_key()
    try:
        return AESGCM(key).decrypt(bytes(nonce), bytes(ciphertext), aad)
    except (InvalidTag, ValueError, TypeError) as exc:
        raise PayloadError() from exc


def payload_aad(*, approval_id: str, fingerprint: str, tool_name: str, agent_id: str | None) -> bytes:
    body = json.dumps(
        {
            "agent_id": agent_id,
            "approval_id": approval_id,
            "fingerprint": fingerprint,
            "tool_name": tool_name,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return body.encode("ascii")


def _directory() -> Path:
    from app.approvals.fingerprint import _key_path

    return _key_path().parent


def _open_directory(directory: Path) -> int:
    try:
        inspected = os.lstat(directory)
    except OSError as exc:
        raise PayloadError() from exc
    if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISDIR(inspected.st_mode):
        raise PayloadError()
    if stat.S_IMODE(inspected.st_mode) != 0o700 or inspected.st_uid != os.getuid():
        raise PayloadError()
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise PayloadError() from exc
    try:
        opened = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise PayloadError() from exc
    if (
        opened.st_dev != inspected.st_dev
        or opened.st_ino != inspected.st_ino
        or not stat.S_ISDIR(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_uid != os.getuid()
    ):
        os.close(descriptor)
        raise PayloadError()
    return descriptor


def _reuse_or_create(directory: int, name: str) -> bytes:
    try:
        current = os.lstat(name, dir_fd=directory)
    except FileNotFoundError:
        return _publish(directory, name)
    except OSError as exc:
        raise PayloadError() from exc
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise PayloadError()
    return _read_final(directory, name)


def _publish(directory: int, name: str) -> bytes:
    token = os.urandom(8).hex()
    temporary = f"{_TEMP_PREFIX}{os.getpid()}.{token}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
    try:
        material = os.urandom(32)
        _write_all(descriptor, material)
        info = os.fstat(descriptor)
        if not _accepted(info, os.getuid()) or info.st_size != 32:
            raise PayloadError()
        os.fsync(descriptor)
        if _stop_before_link:
            raise PayloadError()
        try:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            os.close(descriptor)
            descriptor = -1
            os.unlink(temporary, dir_fd=directory)
            return _read_final(directory, name)
        os.fsync(directory)
    except PayloadError:
        raise
    except OSError as exc:
        raise PayloadError() from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not _stop_before_link:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    return _read_final(directory, name)


def _read_final(directory: int, name: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except OSError as exc:
        raise PayloadError() from exc
    try:
        info = os.fstat(descriptor)
        if not _accepted(info, os.getuid()):
            raise PayloadError()
        material = os.read(descriptor, 64)
    except OSError as exc:
        raise PayloadError() from exc
    finally:
        os.close(descriptor)
    if len(material) != 32:
        raise PayloadError()
    return material


def _accepted(info: os.stat_result, owner: int) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == owner
        and info.st_size == 32
    )


def _write_all(descriptor: int, material: bytes) -> None:
    view = memoryview(material)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise PayloadError()
        view = view[written:]


def _payload_rows_exist() -> bool:
    from app.approvals.payloads import McpExecutionPayloadRecord
    from app.core.database import get_session_factory

    session = get_session_factory()()
    try:
        count = session.scalar(select(func.count()).select_from(McpExecutionPayloadRecord))
        return bool(count)
    except Exception as exc:
        raise PayloadError() from exc
    finally:
        session.close()
