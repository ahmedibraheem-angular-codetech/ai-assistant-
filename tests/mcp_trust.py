"""Host-local trusted executables for MCP *test fixtures* (macOS and Linux).

Why this exists
---------------
Production config/process validation rejects symlink roots and symlink
ancestors via ``lstat`` (see ``app.mcp.loader._directory_is_trusted``,
``app.mcp.process.parent_entry_trusted``). Ubuntu's merged-/usr layout makes
``/bin`` a symlink to ``/usr/bin``, so hard-coded ``/bin`` fixtures fail
publication on Linux CI while still passing on macOS.

What this helper checks (filesystem / native-header trust subset)
-----------------------------------------------------------------
For candidate paths it verifies, with ``lstat`` / ``O_NOFOLLOW`` opens only:

* Root and every ancestor: real directory (not a symlink), not group/world
  writable, owned by root or the current user.
* File: regular file (not a symlink), not setuid/setgid, not group/world
  writable, owner-executable, owned by root or the current user.
* Native header: first four bytes are an accepted Mach-O magic (darwin) or
  ELF magic (linux); shebang/scripts and other formats are rejected via
  ``app.mcp.process.header_accepted``.

What this helper does *not* claim
---------------------------------
It does **not** fully reproduce production executable validation. In
particular it does not run the full loader/process pipeline (config schema,
``executable_roots`` membership of the path, blocked locations, descriptor
re-hash / spawn assessment, etc.). Fixtures still go through production
``reload_configuration`` / spawn paths for end-to-end acceptance.

Import-time failure (intentional)
---------------------------------
``TRUSTED_ROOT``, ``TRUSTED_EXECUTABLE``, and ``TRUSTED_ALT_EXECUTABLE`` are
bound when this module is imported because MCP test modules import those
names at module scope. Deferring discovery would still raise at the same
collection/import boundary for those tests. Raising ``RuntimeError`` here
fails closed and preserves CI coverage (no silent skip).

Supported hosts for MCP fixture suites: macOS or Linux with at least one
non-symlink root among ``/usr/bin`` then ``/bin`` that contains two distinct
native regular executables from the fixed candidate name pairs below.
"""

from __future__ import annotations

import os
import stat
import sys

from app.mcp.process import header_accepted

# Fixed deterministic search order. Do not reorder casually: CI and local
# hosts should resolve the same triple for a given filesystem layout.
_CANDIDATE_ROOTS = ("/usr/bin", "/bin")
_CANDIDATE_NAME_PAIRS = (
    ("ls", "echo"),
    ("true", "false"),
    ("cat", "true"),
    ("echo", "true"),
    ("false", "true"),
)


def _directory_trusted(path: str) -> bool:
    """Filesystem trust for one directory component (lstat, no follow)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_mode & 0o022:
        return False
    return info.st_uid in {0, os.getuid()}


def _native_header_trusted(path: str) -> bool:
    """Reject shebang/scripts and non-native formats; never follow symlinks."""
    if sys.platform == "darwin":
        platform = "darwin"
    elif sys.platform.startswith("linux"):
        platform = "linux"
    else:
        return False
    # Fail closed: never open without O_NOFOLLOW (no symlink-following fallback).
    if not hasattr(os, "O_NOFOLLOW"):
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        header = os.read(descriptor, 4)
    finally:
        os.close(descriptor)
    return header_accepted(header, platform)


def _ancestors_trusted(path: str) -> bool:
    """Walk every ancestor with the same directory trust rules as above."""
    current = os.path.dirname(os.path.abspath(path))
    for _ in range(64):
        if not _directory_trusted(current):
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent
    return False


def _executable_trusted(path: str) -> bool:
    """Filesystem + native-header trust for one candidate executable path."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_mode & (stat.S_ISUID | stat.S_ISGID | 0o022)
        or not info.st_mode & stat.S_IXUSR
        or info.st_uid not in {0, os.getuid()}
    ):
        return False
    if not _ancestors_trusted(path):
        return False
    return _native_header_trusted(path)


def _discover() -> tuple[str, str, str]:
    """Return (root, primary_executable, alternate_executable)."""
    for root in _CANDIDATE_ROOTS:
        if not _directory_trusted(root):
            continue
        for primary_name, alternate_name in _CANDIDATE_NAME_PAIRS:
            primary = f"{root}/{primary_name}"
            alternate = f"{root}/{alternate_name}"
            if primary == alternate:
                continue
            if _executable_trusted(primary) and _executable_trusted(alternate):
                return root, primary, alternate
    raise RuntimeError(
        "No trusted native executable pair is available for MCP tests on this "
        "host. Need a non-symlink root in {/usr/bin, /bin} with two distinct "
        "regular Mach-O (macOS) or ELF (Linux) executables from the fixed "
        "candidate list (ls/echo, true/false, …), owned by root or the "
        "current user, without setuid/setgid or group/world write."
    )


TRUSTED_ROOT, TRUSTED_EXECUTABLE, TRUSTED_ALT_EXECUTABLE = _discover()
