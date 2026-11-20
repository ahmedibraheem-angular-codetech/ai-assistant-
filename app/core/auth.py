"""Local control-plane authentication (WP-05A, simplified design).

Every HTTP request needs ``Authorization: Bearer <token>`` except the health
check. The token is 43 url-safe characters stored in a 0600 file next to the
database. It is created on first start.

Design decisions (see agent-tools/WP05A_SIMPLIFIED_DESIGN.md):
- Protected by default. Only GET/HEAD/OPTIONS on ``{api_prefix}/health`` are
  public, matched on the exact request path. New or unknown routes are
  protected automatically, so no route inventory is needed.
- Host must be a loopback name (DNS-rebinding defense). A browser Origin, when
  present, must also be loopback.
- Rotation needs the server stopped. Each serving process holds a shared
  flock on ``control_plane.lock`` from startup to shutdown; ``rotate`` takes an
  exclusive non-blocking lock and refuses while any server holds it. Each
  process opens the lock file itself inside the lifespan, so worker processes
  never share an inherited lock.
"""

from __future__ import annotations

import fcntl
import hmac
import os
import secrets
import stat
from pathlib import Path

TOKEN_NAME = "control_plane.token"
LOCK_NAME = "control_plane.lock"
TOKEN_LENGTH = 43
_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_PUBLIC_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class AuthStoreError(RuntimeError):
    """The token store is missing, unsafe, or damaged. The server must not start."""


class ServerRunningError(RuntimeError):
    """A serving process holds the lock, so the token cannot be rotated."""


# ---------------------------------------------------------------- storage


def data_dir() -> Path:
    from app.core.config import get_settings

    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        database = Path(url.removeprefix("sqlite:///"))
        if database.name:
            return database.parent
    return Path.home() / ".personal-ai-assistant"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AuthStoreError("The data folder is not a real directory.")
    if info.st_uid != os.getuid():
        raise AuthStoreError("The data folder belongs to another user.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)


def _valid(token: str) -> bool:
    return len(token) == TOKEN_LENGTH and set(token) <= _ALPHABET


def new_token() -> str:
    token = secrets.token_urlsafe(32)
    assert _valid(token)
    return token


def _open_regular(path: Path, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(descriptor)
        raise AuthStoreError(f"{path.name} is not a private regular file.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.close(descriptor)
        raise AuthStoreError(f"{path.name} is readable by other users. Run chmod 600 on it.")
    return descriptor


def read_token(directory: Path | None = None) -> str:
    folder = directory or data_dir()
    path = folder / TOKEN_NAME
    try:
        descriptor = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError as exc:
        raise AuthStoreError("No token yet. Start the server once to create it.") from exc
    try:
        raw = os.read(descriptor, TOKEN_LENGTH + 1)
    finally:
        os.close(descriptor)
    token = raw.decode("ascii", errors="replace")
    if not _valid(token):
        raise AuthStoreError("The token file is damaged. Run: python -m app.control_plane rotate")
    return token


def write_token(token: str, directory: Path | None = None) -> None:
    """Replace the token atomically and durably: temp file, fsync, rename, fsync dir."""
    if not _valid(token):
        raise AuthStoreError("Refusing to store a malformed token.")
    folder = directory or data_dir()
    _ensure_dir(folder)
    temporary = folder / f".{TOKEN_NAME}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.write(descriptor, token.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, folder / TOKEN_NAME)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory_fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _open_lock(folder: Path) -> int:
    _ensure_dir(folder)
    return _open_regular(folder / LOCK_NAME, os.O_RDWR | os.O_CREAT)


class ServeLease:
    """Shared lock held by one serving process for its whole lifetime."""

    def __init__(self, descriptor: int, token: str) -> None:
        self._descriptor: int | None = descriptor
        self.token = token

    @classmethod
    def acquire(cls, directory: Path | None = None) -> "ServeLease":
        folder = directory or data_dir()
        descriptor = _open_lock(folder)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AuthStoreError("A token rotation is in progress. Try again.") from exc
            try:
                token = read_token(folder)
            except AuthStoreError:
                if (folder / TOKEN_NAME).exists():
                    raise
                # First start: create under an exclusive lock so two first
                # starts cannot write different tokens.
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    token = read_token(folder)
                except AuthStoreError:
                    if (folder / TOKEN_NAME).exists():
                        raise
                    write_token(new_token(), folder)
                    token = read_token(folder)
                fcntl.flock(descriptor, fcntl.LOCK_SH)
        except BaseException:
            os.close(descriptor)
            raise
        return cls(descriptor, token)

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)  # closing the fd releases the flock


def rotate(directory: Path | None = None) -> str:
    """Write a new token. Refuses while any server holds its lease."""
    folder = directory or data_dir()
    descriptor = _open_lock(folder)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServerRunningError(
                "The server is running. Stop it, rotate, then start it again."
            ) from exc
        token = new_token()
        write_token(token, folder)
        return token
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------- middleware


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value


def _loopback_origin(origin: str) -> bool:
    if origin == "null":
        return False
    scheme, _, rest = origin.partition("://")
    if scheme not in {"http", "https"} or not rest or "/" in rest:
        return False
    return _host_name(rest) in _LOOPBACK_HOSTS


def bearer_matches(header: str, token: str) -> bool:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not _valid(value):
        return False
    return hmac.compare_digest(value.encode("ascii"), token.encode("ascii"))


class LocalAuthMiddleware:
    """Pure ASGI middleware. Reads the token from ``app.state.auth_token``."""

    def __init__(self, app, *, api_prefix: str) -> None:
        self.app = app
        self.health_path = api_prefix.rstrip("/") + "/health"

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {}
        for name, value in scope.get("headers") or []:
            key = name.decode("latin-1").lower()
            if key in headers:  # duplicated security headers are rejected
                headers[key] = None
            else:
                headers[key] = value.decode("latin-1")
        host = headers.get("host")
        if host is None or _host_name(host) not in _LOOPBACK_HOSTS:
            await _deny(scope, send, 421, "Requests must use a localhost address.")
            return
        origin = headers.get("origin", "")
        if origin is None or (origin and not _loopback_origin(origin)):
            await _deny(scope, send, 403, "Cross-site requests are not allowed.")
            return
        if (
            scope["type"] == "http"
            and scope.get("method", "") in _PUBLIC_METHODS
            and scope.get("path") == self.health_path
        ):
            await self.app(scope, receive, send)
            return
        token = getattr(scope.get("app").state, "auth_token", None) if scope.get("app") else None
        authorization = headers.get("authorization")
        if not isinstance(token, str) or not isinstance(authorization, str) or not bearer_matches(authorization, token):
            await _deny(scope, send, 401, "Authentication required.")
            return
        await self.app(scope, receive, send)


async def _deny(scope, send, status: int, message: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = ('{"detail":"' + message + '"}').encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
