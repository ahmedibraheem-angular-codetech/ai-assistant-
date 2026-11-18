"""Open files inside one approved directory.

String checks reject traversal before any open. The open itself uses directory
file descriptors and O_NOFOLLOW, so a symlink swapped in after the check is
not followed. Symlinks are never followed, including ones that stay inside
the workspace.
"""

import errno
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from app.files.exceptions import FileAccessError, WorkspacePathError, WorkspacePermissionError

_OUTSIDE = "That path is outside the workspace."
_SYMLINK = "Symlinks are not followed."
_MISSING = "That file was not found."
_NOT_FILE = "That path is not a file."
_NOT_DIR = "That path is not a directory."
_DENIED = "The operating system denied access. macOS privacy protections were not bypassed."
_UNAVAILABLE = "That directory is not available."
_NOT_DIRECTORY = "That path is not a directory."
_SYMLINK_ROOT = "That path contains a symlink. Register the real directory."
_ROOT = "The filesystem root cannot be a workspace."
_HOME = "The home directory cannot be a workspace. Choose a specific folder."
_TOO_BROAD = "That directory is too broad to register. Choose a specific folder."

_BLOCKED_ROOTS = {
    "/",
    "/users",
    "/volumes",
    "/system",
    "/private",
    "/private/etc",
    "/private/tmp",
    "/private/var",
    "/etc",
    "/var",
    "/tmp",
    "/applications",
    "/library",
}


@dataclass(frozen=True, slots=True)
class ApprovedRoot:
    path: str
    device: int
    inode: int


def approve_directory(raw_path: str) -> ApprovedRoot:
    """Validate a directory the user explicitly asked to register.

    The stored path is the submitted path after absolute normalization. If any
    symlink is involved, registration stops instead of silently using the target.
    """
    if "\x00" in raw_path:
        raise WorkspacePathError(_UNAVAILABLE)
    absolute = Path(os.path.abspath(raw_path))
    try:
        real = Path(os.path.realpath(absolute))
    except OSError as exc:
        _raise_os(exc, registration=True)
        raise WorkspacePathError(_UNAVAILABLE) from exc
    if not _same_directory(absolute, real):
        raise WorkspacePathError(_SYMLINK_ROOT)
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise WorkspacePathError("That directory does not exist.") from exc
    except PermissionError as exc:
        raise WorkspacePermissionError() from exc
    except OSError as exc:
        _raise_os(exc, registration=True)
        raise WorkspacePathError(_UNAVAILABLE) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspacePathError(_NOT_DIRECTORY)
    if _is_blocked_root(real):
        if real == Path("/"):
            raise WorkspacePathError(_ROOT)
        home = _home()
        if home is not None and real == home:
            raise WorkspacePathError(_HOME)
        raise WorkspacePathError(_TOO_BROAD)
    if len(str(real)) > 1024:
        raise WorkspacePathError(_UNAVAILABLE)
    descriptor = _open_directory(real)
    try:
        current = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISDIR(current.st_mode):
        raise WorkspacePathError(_NOT_DIRECTORY)
    return ApprovedRoot(path=str(real), device=int(current.st_dev), inode=int(current.st_ino))


def relative_parts(relative: str | None) -> tuple[str, ...]:
    if relative is None:
        return ()
    text = unicodedata.normalize("NFC", relative)
    if text.strip() in {"", "."}:
        return ()
    decoded = text
    for _ in range(3):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    decoded = unicodedata.normalize("NFC", decoded)
    if text.startswith("/") or decoded.startswith("/") or "\\" in text or "\x00" in text or "\x00" in decoded:
        raise FileAccessError(_OUTSIDE)
    parts = tuple(text.split("/"))
    decoded_parts = tuple(decoded.split("/"))
    if any(part in {"", ".", ".."} for part in parts + decoded_parts):
        raise FileAccessError(_OUTSIDE)
    return parts


def list_directory(
    root: ApprovedRoot,
    relative: str | None,
    *,
    recursive: bool,
    limit: int,
    max_depth: int,
    extensions: set[str] | None,
) -> list[dict]:
    parts = relative_parts(relative)
    root_fd = _open_root(root)
    try:
        directory = _walk_directories(root_fd, parts)
        try:
            entries: list[dict] = []
            _collect(
                directory,
                parts,
                entries,
                recursive=recursive,
                limit=limit,
                depth=0,
                max_depth=max_depth,
                extensions=extensions,
            )
            return entries
        finally:
            if directory != root_fd:
                os.close(directory)
    finally:
        os.close(root_fd)


def visit_files(
    root: ApprovedRoot,
    relative: str | None,
    *,
    limit: int,
    max_depth: int,
    extensions: set[str] | None,
    visitor,
) -> int:
    """Call visitor(relative_path, stat, directory_fd, name) for regular files.

    The visitor must not close directory_fd. Scanning stops after limit files.
    """
    parts = relative_parts(relative)
    root_fd = _open_root(root)
    seen = 0
    try:
        directory = _walk_directories(root_fd, parts)
        try:
            seen = _visit(directory, parts, seen, limit=limit, depth=0, max_depth=max_depth, extensions=extensions, visitor=visitor)
        finally:
            if directory != root_fd:
                os.close(directory)
        return seen
    finally:
        os.close(root_fd)


def open_file(root: ApprovedRoot, relative: str | None) -> tuple[int, os.stat_result, str]:
    parts = relative_parts(relative)
    if not parts:
        raise FileAccessError(_NOT_FILE)
    root_fd = _open_root(root)
    directory = root_fd
    try:
        if len(parts) > 1:
            directory = _walk_directories(root_fd, parts[:-1])
        try:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except OSError as exc:
            _raise_file_os(exc)
            raise FileAccessError(_MISSING) from exc
        try:
            info = os.fstat(descriptor)
        except OSError as exc:
            os.close(descriptor)
            _raise_file_os(exc)
            raise FileAccessError(_MISSING) from exc
        if stat.S_ISLNK(info.st_mode):
            os.close(descriptor)
            raise FileAccessError(_SYMLINK)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise FileAccessError(_NOT_FILE)
        return descriptor, info, "/".join(parts)
    finally:
        if directory != root_fd:
            os.close(directory)
        os.close(root_fd)


def metadata(root: ApprovedRoot, relative: str | None) -> dict:
    parts = relative_parts(relative)
    root_fd = _open_root(root)
    try:
        if not parts:
            info = os.fstat(root_fd)
            name = Path(root.path).name or root.path
            return _metadata(name, "", "directory", info)
        parent = root_fd
        if len(parts) > 1:
            parent = _walk_directories(root_fd, parts[:-1])
        try:
            info = os.lstat(parts[-1], dir_fd=parent)
        except OSError as exc:
            _raise_file_os(exc)
            raise FileAccessError(_MISSING) from exc
        finally:
            if parent != root_fd:
                os.close(parent)
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            kind = "other"
        return _metadata(parts[-1], "/".join(parts), kind, info)
    finally:
        os.close(root_fd)


def read_fd(descriptor: int, size: int, maximum: int) -> bytes:
    if size > maximum:
        raise FileAccessError("That file is too large to read.")
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        piece = os.read(descriptor, min(remaining, 65_536))
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def _collect(
    directory: int,
    prefix: tuple[str, ...],
    entries: list[dict],
    *,
    recursive: bool,
    limit: int,
    depth: int,
    max_depth: int,
    extensions: set[str] | None,
) -> None:
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        _raise_file_os(exc)
        return
    for name in names:
        if len(entries) >= limit:
            return
        if name in {".", ".."} or "/" in name or "\x00" in name:
            continue
        try:
            info = os.lstat(name, dir_fd=directory)
        except OSError:
            continue
        relative = "/".join((*prefix, name))
        if stat.S_ISLNK(info.st_mode):
            if extensions is None:
                entries.append(_entry(name, relative, "symlink", info))
            continue
        if stat.S_ISDIR(info.st_mode):
            if extensions is None:
                entries.append(_entry(name, relative, "directory", info))
            if recursive and depth + 1 < max_depth and len(entries) < limit:
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                except OSError:
                    continue
                try:
                    _collect(
                        child,
                        (*prefix, name),
                        entries,
                        recursive=True,
                        limit=limit,
                        depth=depth + 1,
                        max_depth=max_depth,
                        extensions=extensions,
                    )
                finally:
                    os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        extension = Path(name).suffix.lower()
        if extensions is not None and extension not in extensions:
            continue
        entries.append(_entry(name, relative, "file", info))


def _visit(
    directory: int,
    prefix: tuple[str, ...],
    seen: int,
    *,
    limit: int,
    depth: int,
    max_depth: int,
    extensions: set[str] | None,
    visitor,
) -> int:
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        _raise_file_os(exc)
        return seen
    for name in names:
        if seen >= limit:
            return seen
        if name in {".", ".."} or "/" in name or "\x00" in name:
            continue
        try:
            info = os.lstat(name, dir_fd=directory)
        except OSError:
            continue
        relative = "/".join((*prefix, name))
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            if depth + 1 < max_depth and seen < limit:
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                except OSError:
                    continue
                try:
                    seen = _visit(
                        child,
                        (*prefix, name),
                        seen,
                        limit=limit,
                        depth=depth + 1,
                        max_depth=max_depth,
                        extensions=extensions,
                        visitor=visitor,
                    )
                finally:
                    os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        extension = Path(name).suffix.lower()
        if extensions is not None and extension not in extensions:
            continue
        visitor(relative, info, directory, name)
        seen += 1
    return seen


def _walk_directories(root_fd: int, parts: tuple[str, ...]) -> int:
    current = root_fd
    opened: list[int] = []
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            opened.append(nxt)
            current = nxt
    except OSError as exc:
        for descriptor in opened:
            os.close(descriptor)
        _raise_file_os(exc)
        raise FileAccessError(_NOT_DIR) from exc
    if len(opened) > 1:
        for descriptor in opened[:-1]:
            os.close(descriptor)
    return current


def _open_root(root: ApprovedRoot) -> int:
    try:
        descriptor = os.open(root.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
            raise FileAccessError(_DENIED) from exc
        raise FileAccessError("That workspace is not available.") from exc
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise FileAccessError("That workspace is not available.") from exc
    if int(info.st_dev) != root.device or int(info.st_ino) != root.inode or not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise FileAccessError("That workspace is not available.")
    return descriptor


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        _raise_os(exc, registration=True)
        raise WorkspacePathError(_UNAVAILABLE) from exc


def _metadata(name: str, relative: str, kind: str, info: os.stat_result) -> dict:
    from datetime import datetime, timezone

    extension = Path(name).suffix.lower()
    modified = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds")
    return {
        "filename": name,
        "relative_path": relative,
        "extension": extension,
        "size_bytes": int(info.st_size) if kind == "file" else None,
        "modified_at": modified,
        "category": _category(extension) if kind == "file" else kind,
        "entry_type": kind,
    }


def _entry(name: str, relative: str, kind: str, info: os.stat_result) -> dict:
    extension = Path(name).suffix.lower()
    return {
        "name": name,
        "relative_path": relative,
        "entry_type": kind,
        "extension": extension if kind == "file" else "",
        "size_bytes": int(info.st_size) if kind == "file" else None,
        "category": _category(extension) if kind == "file" else kind,
    }


def _category(extension: str) -> str:
    from app.files.parsers import category_for

    return category_for(extension)


def _same_directory(absolute: Path, real: Path) -> bool:
    """Allow only macOS system prefixes such as /var -> /private/var.

    Any other symlink is a different directory and must be submitted as the
    real path so registration cannot silently change the approved folder.
    """
    if absolute == real:
        return True
    absolute_text = str(absolute)
    real_text = str(real)
    for prefix in ("/var/", "/tmp/", "/etc/"):
        if absolute_text.startswith(prefix) and real_text == "/private" + absolute_text:
            return True
    return False


def _home() -> Path | None:
    try:
        return Path(os.path.realpath(Path.home()))
    except OSError:
        return None


def _is_blocked_root(path: Path) -> bool:
    text = str(path)
    if text == "/":
        return True
    home = _home()
    if home is not None and path == home:
        return True
    return text.casefold() in _BLOCKED_ROOTS


def _raise_os(exc: OSError, *, registration: bool) -> None:
    if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
        raise WorkspacePermissionError() from exc
    if exc.errno in {errno.ELOOP, errno.EMLINK}:
        raise WorkspacePathError(_SYMLINK_ROOT) from exc
    if exc.errno == errno.ENOENT:
        raise WorkspacePathError("That directory does not exist.") from exc
    if registration:
        return


def _raise_file_os(exc: OSError) -> None:
    if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
        raise FileAccessError(_DENIED) from exc
    if exc.errno in {errno.ELOOP, errno.EMLINK}:
        raise FileAccessError(_SYMLINK) from exc
    if exc.errno == errno.ENOENT:
        raise FileAccessError(_MISSING) from exc
    if exc.errno == errno.ENOTDIR:
        raise FileAccessError(_NOT_DIR) from exc
    raise FileAccessError(_OUTSIDE) from exc
