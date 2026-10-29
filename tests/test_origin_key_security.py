"""Private origin signers reject exposed keys and nonregular filesystem inputs."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from unimem import memory_authority


def test_missing_bound_capability_cannot_change_forgetting_authority(tmp_path, monkeypatch):
    from unimem import capabilities, capability_io

    token = capabilities.CURRENT.set({"principal": "missing"})
    monkeypatch.setattr(capability_io, "current", lambda root: None)
    try:
        with pytest.raises(PermissionError, match="forgetting authority"):
            memory_authority.record_forgetting(str(tmp_path), "record", forgotten=True)
    finally:
        capabilities.CURRENT.reset(token)
    assert not (tmp_path / "memory" / "forgetting.jsonl").exists()


@pytest.mark.parametrize(
    "algorithm,filename",
    [
        ("hmac-sha256", ".origin-key"),
        ("ed25519", ".origin-ed25519-key"),
    ],
)
def test_origin_keys_remain_private_regular_files(tmp_path, algorithm, filename):
    key = memory_authority._key(str(tmp_path), create=True, algorithm=algorithm)
    assert key is not None and len(key) == 32
    assert memory_authority._key(str(tmp_path), algorithm=algorithm) == key
    path = tmp_path / "memory" / filename
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        path.chmod(0o644)
        with pytest.raises(ValueError, match="private"):
            memory_authority._key(str(tmp_path), algorithm=algorithm)


def test_hardlinked_origin_signer_is_rejected(tmp_path):
    memory_authority._key(str(tmp_path), create=True)
    path = tmp_path / "memory" / ".origin-key"
    (tmp_path / "aliased-key").hardlink_to(path)
    with pytest.raises(ValueError, match="regular|link"):
        memory_authority._key(str(tmp_path))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special files")
def test_fifo_origin_key_fails_without_waiting_for_a_writer(tmp_path):
    directory = tmp_path / "memory"
    directory.mkdir()
    os.mkfifo(directory / ".origin-key", 0o600)
    script = """
import sys
from unimem import memory_authority
try:
    memory_authority._key(sys.argv[1])
except ValueError:
    raise SystemExit(0)
raise SystemExit('FIFO was accepted as a private key')
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr


def test_key_open_cannot_follow_a_symlink_swapped_after_validation(tmp_path, monkeypatch):
    memory_authority._key(str(tmp_path), create=True)
    path = tmp_path / "memory" / ".origin-key"
    outside = tmp_path / "outside-key"
    outside.write_bytes(b"x" * 32)
    original_islink = os.path.islink
    swapped = False

    def racing_islink(filename):
        nonlocal swapped
        result = original_islink(filename)
        if os.fspath(filename) == str(path) and not swapped:
            swapped = True
            path.unlink()
            path.symlink_to(outside)
        return result

    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("descriptor-level no-follow support required")
    monkeypatch.setattr(os.path, "islink", racing_islink)
    with pytest.raises((OSError, ValueError)):
        memory_authority._key(str(tmp_path))
    assert swapped


def test_concurrent_first_signers_wait_for_key_initialization(tmp_path, monkeypatch):
    root = str(tmp_path)
    reserved = threading.Event()
    empty_read = threading.Event()
    finish = threading.Event()
    token_bytes = memory_authority.secrets.token_bytes
    fdopen = os.fdopen

    def reserve_then_write(size):
        reserved.set()
        assert finish.wait(5)
        return token_bytes(size)

    class ObserveRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            value = self.stream.read(size)
            if not value:
                empty_read.set()
            return value

    def observe(fd, mode, *args, **kwargs):
        stream = fdopen(fd, mode, *args, **kwargs)
        return ObserveRead(stream) if mode == "rb" else stream

    monkeypatch.setattr(memory_authority.secrets, "token_bytes", reserve_then_write)
    monkeypatch.setattr(os, "fdopen", observe)
    with ThreadPoolExecutor(2) as workers:
        creator = workers.submit(memory_authority._key, root, create=True)
        try:
            assert reserved.wait(5)
            follower = workers.submit(memory_authority._key, root, create=True)
            assert empty_read.wait(5)
        finally:
            finish.set()
        assert creator.result(timeout=5) == follower.result(timeout=5)
