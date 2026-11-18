"""Read ZIP members in memory under fixed limits.

Nothing is written to disk. Header checks run before decompression, and the
byte, time, and count limits are applied again while members are read.
"""

import time
import zipfile
from dataclasses import dataclass
from io import BytesIO

from app.core.config import get_settings
from app.files.exceptions import FileAccessError

_UNREADABLE = "The file could not be read."
_TOO_LARGE = "That archive is too large to read."
_TOO_MANY = "That archive contains too many files."
_RATIO = "That archive is too compressed to read safely."
_NESTED = "That archive is nested too deeply."
_TRAVERSAL = "That archive contains an unsafe path."
_LINK = "That archive contains a link and was not read."
_SLOW = "The archive took too long to read."
_ENCRYPTED = "That archive is encrypted and was not read."


@dataclass(frozen=True, slots=True)
class ZipLimits:
    max_uncompressed: int
    max_member: int
    max_files: int
    max_ratio: int
    max_depth: int
    max_seconds: float


def limits_from_settings() -> ZipLimits:
    settings = get_settings()
    return ZipLimits(
        max_uncompressed=settings.file_zip_max_uncompressed_bytes,
        max_member=settings.file_zip_max_member_bytes,
        max_files=settings.file_zip_max_files,
        max_ratio=settings.file_zip_max_ratio,
        max_depth=settings.file_zip_max_depth,
        max_seconds=settings.file_zip_max_seconds,
    )


def assert_archive_safe(data: bytes, limits: ZipLimits | None = None) -> None:
    """Decompress members under the limits and discard the bytes."""
    read_archive(data, limits=limits)


def read_archive(
    data: bytes,
    *,
    limits: ZipLimits | None = None,
    clock=time.monotonic,
) -> list[tuple[str, bytes]]:
    active = limits or limits_from_settings()
    started = clock()
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FileAccessError(_UNREADABLE) from exc
    try:
        infos = archive.infolist()
        if len(infos) > active.max_files:
            raise FileAccessError(_TOO_MANY)
        claimed = 0
        for info in infos:
            _check_clock(started, active, clock)
            _check_member(info, active)
            claimed += max(0, int(info.file_size))
            if claimed > active.max_uncompressed:
                raise FileAccessError(_TOO_LARGE)
        members: list[tuple[str, bytes]] = []
        for info in infos:
            _check_clock(started, active, clock)
            if info.is_dir():
                continue
            members.append((info.filename, _read_member(archive, info, active, started, clock)))
        return members
    finally:
        archive.close()


def _check_member(info: zipfile.ZipInfo, limits: ZipLimits) -> None:
    if info.flag_bits & 0x1:
        raise FileAccessError(_ENCRYPTED)
    if _is_link(info):
        raise FileAccessError(_LINK)
    name = info.filename
    if _unsafe_name(name) or _depth(name) > limits.max_depth:
        if _depth(name) > limits.max_depth and not _unsafe_name(name):
            raise FileAccessError(_NESTED)
        raise FileAccessError(_TRAVERSAL)
    if int(info.file_size) > limits.max_member or int(info.file_size) < 0:
        raise FileAccessError(_TOO_LARGE)
    compressed = max(1, int(info.compress_size))
    # Tiny members are not bombs. The ratio applies once a member claims 64KB or more.
    if int(info.file_size) >= 65_536 and int(info.file_size) / compressed > limits.max_ratio:
        raise FileAccessError(_RATIO)


def _read_member(archive, info, limits: ZipLimits, started: float, clock) -> bytes:
    try:
        handle = archive.open(info, pwd=None)
    except (RuntimeError, zipfile.BadZipFile) as exc:
        raise FileAccessError(_UNREADABLE) from exc
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            _check_clock(started, limits, clock)
            piece = handle.read(65_536)
            if not piece:
                break
            total += len(piece)
            if total > limits.max_member or total > limits.max_uncompressed:
                raise FileAccessError(_TOO_LARGE)
            chunks.append(piece)
    finally:
        handle.close()
    return b"".join(chunks)


def _check_clock(started: float, limits: ZipLimits, clock) -> None:
    if clock() - started > limits.max_seconds:
        raise FileAccessError(_SLOW)


def _is_link(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0o170000
    return mode == 0o120000


def _member_name(name: str) -> str:
    if name.endswith("/"):
        return name[:-1]
    return name


def _unsafe_name(name: str) -> bool:
    text = _member_name(name)
    if not text or "\x00" in text or "\\" in text:
        return True
    if text.startswith("/") or text.startswith("~"):
        return True
    if len(text) > 1 and text[1] == ":":
        return True
    return any(part in {"", ".", ".."} for part in text.split("/"))


def _depth(name: str) -> int:
    return len([part for part in _member_name(name).split("/") if part])
