"""WP-02 executable identity and process descriptor security."""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.mcp.process import (
    ProcessError,
    _close_inherited,
    _list_fds,
    assess_executable,
    open_executable,
    parent_entry_trusted,
    revalidate,
    spawn_identity,
    start_process,
)


class _Server:
    def __init__(self, path: str, args: tuple[str, ...] = ()) -> None:
        self.executable = path
        self.args = args
        self.env: tuple[tuple[str, str], ...] = ()


def _compile(directory: Path, name: str, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source_path = directory / f"{name}.c"
    binary = directory / name
    source_path.write_text(source)
    completed = subprocess.run(
        ["clang", "-O0", "-o", str(binary), str(source_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"clang is required for WP-02 process tests: {completed.stderr}")
    os.chmod(binary, 0o700)
    return binary


@pytest.fixture
def binaries():
    root = Path(__file__).resolve().parent / "_wp02_bin"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    os.chmod(root, 0o700)
    first = _compile(
        root,
        "first",
        '#include <stdio.h>\nint main(void){puts("first");return 0;}\n',
    )
    second = _compile(
        root,
        "second",
        '#include <stdio.h>\nint main(void){puts("second");return 0;}\n',
    )
    echo = _compile(
        root,
        "echoargs",
        (
            "#include <stdio.h>\n"
            "int main(int argc, char **argv){\n"
            "  for(int i=1;i<argc;i++){fputs(argv[i],stdout); if(i+1<argc) fputc(' ',stdout);}\n"
            "  fputc('\\n',stdout); return 0;\n"
            "}\n"
        ),
    )
    yield {"root": root, "first": first, "second": second, "echo": echo}
    shutil.rmtree(root, ignore_errors=True)


def test_parent_entry_trusted_rejects_untrusted_owners_and_modes():
    owner = os.getuid()

    class Info:
        def __init__(self, mode: int, uid: int) -> None:
            self.st_mode = mode
            self.st_uid = uid

    assert parent_entry_trusted(Info(stat.S_IFDIR | 0o700, owner), owner) is True
    assert parent_entry_trusted(Info(stat.S_IFDIR | 0o755, 0), owner) is True
    assert parent_entry_trusted(Info(stat.S_IFDIR | 0o755, owner + 1), owner) is False
    assert parent_entry_trusted(Info(stat.S_IFDIR | 0o775, owner), owner) is False
    assert parent_entry_trusted(Info(stat.S_IFDIR | 0o757, owner), owner) is False
    assert parent_entry_trusted(Info(stat.S_IFLNK | 0o700, owner), owner) is False
    assert parent_entry_trusted(Info(stat.S_IFREG | 0o700, owner), owner) is False


def test_real_filesystem_rejects_group_writable_and_symlink_parents(binaries):
    root = binaries["root"]
    child = root / "child"
    child.mkdir()
    os.chmod(child, 0o700)
    binary = child / "tool"
    shutil.copy2(binaries["first"], binary)
    os.chmod(binary, 0o700)
    assess_executable(str(binary))

    os.chmod(child, 0o770)
    with pytest.raises(ProcessError) as group:
        assess_executable(str(binary))
    assert group.value.code == "executable"
    os.chmod(child, 0o700)

    linked = root / "linked"
    linked.symlink_to(child)
    with pytest.raises(ProcessError) as linked_parent:
        assess_executable(str(linked / "tool"))
    assert linked_parent.value.code == "executable"


def test_replacement_before_spawn_is_rejected(binaries):
    path = binaries["first"]
    replacement = binaries["second"].read_bytes()
    facts, descriptor = open_executable(str(path))
    try:
        os.remove(path)
        path.write_bytes(replacement)
        os.chmod(path, 0o700)
        with pytest.raises(ProcessError) as caught:
            start_process(_Server(str(path)), facts, generation=1, descriptor=descriptor)
        assert caught.value.code == "changed"
        with pytest.raises(ProcessError):
            revalidate(descriptor, facts)
    finally:
        os.close(descriptor)


def test_real_macos_spawn_runs_the_verified_binary(binaries):
    if sys.platform != "darwin":
        pytest.skip("Real posix_spawn coverage runs on macOS.")
    path = str(binaries["echo"].resolve())
    facts, descriptor = open_executable(path)
    try:
        child = start_process(_Server(path, ("alpha", "beta")), facts, generation=9, descriptor=descriptor)
    finally:
        os.close(descriptor)
    try:
        output = os.read(child.stdout_fd, 4096)
        assert output == b"alpha beta\n"
        assert child.facts.sha256 == hashlib.sha256(Path(path).read_bytes()).hexdigest()
        assert child.pgid == child.pid
        child.close_stdin()
        if child.alive():
            child.signal_group(signal.SIGTERM)
        child.reap()
        assert child.reaped is True
    finally:
        if not child.reaped:
            try:
                child.signal_group(signal.SIGKILL)
            except Exception:
                pass
            child.reap()


def test_spawn_identity_documents_the_platform_policy():
    text = spawn_identity()
    if sys.platform == "darwin":
        assert "untrusted different user cannot replace" in text
        assert "same service user can still replace" in text
    elif sys.platform.startswith("linux"):
        assert "execveat" in text
        assert "executable descriptor stays open" in text


def test_close_inherited_preserves_kept_descriptors_including_high_numbers():
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            low = os.open(os.devnull, os.O_RDONLY)
            high = None
            for _ in range(400):
                candidate = os.open(os.devnull, os.O_RDONLY)
                if candidate > 256:
                    high = candidate
                    break
            if high is None:
                os.write(write_end, b"skip")
                os._exit(0)
            keep_low = os.open(os.devnull, os.O_RDONLY)
            keep = {0, 1, 2, keep_low, high, write_end}
            _close_inherited(keep)
            after = set(_list_fds())
            if keep_low not in after or high not in after:
                os.write(write_end, b"missing-keep")
                os._exit(1)
            if low in after:
                os.write(write_end, b"low-survived")
                os._exit(1)
            leaked = [fd for fd in after if fd not in keep]
            if leaked:
                os.write(write_end, b"leaked:" + ",".join(str(fd) for fd in leaked).encode())
                os._exit(1)
            os.write(write_end, b"ok")
            os._exit(0)
        except Exception as exc:
            try:
                os.write(write_end, b"exc:" + repr(exc).encode()[:200])
            except OSError:
                pass
            os._exit(2)
    os.close(write_end)
    status = os.read(read_end, 256)
    os.close(read_end)
    _, exit_status = os.waitpid(pid, 0)
    if status == b"skip":
        pytest.skip("Could not open a descriptor above 256.")
    assert status == b"ok", status
    assert os.WIFEXITED(exit_status) and os.WEXITSTATUS(exit_status) == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Real execveat coverage runs on Linux.")
def test_linux_execveat_preserves_the_executable_descriptor(binaries):
    path = binaries["echo"]
    facts, descriptor = open_executable(str(path), platform="linux")
    try:
        child = start_process(
            _Server(str(path), ("linux",)),
            facts,
            generation=3,
            descriptor=descriptor,
            platform="linux",
        )
        output = os.read(child.stdout_fd, 4096)
        assert output == b"linux\n"
        child.close_stdin()
        child.reap()
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
