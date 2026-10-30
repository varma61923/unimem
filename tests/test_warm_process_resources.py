"""Detached warm workers keep a parent-side owner until their exit is reaped."""

import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from unimem import warm

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the warm worker is Unix-only")


@pytest.mark.parametrize("thread_failure", [False, True])
def test_spawn_reaps_its_child_even_when_supervision_cannot_start(tmp_path, monkeypatch, thread_failure):
    launch = subprocess.Popen
    reaped = threading.Event()
    processes = []
    real_waits = []

    def child(*args, **kwargs):
        code = "import time; time.sleep(60)" if thread_failure else "pass"
        process = launch([sys.executable, "-c", code], **kwargs)
        original_wait = process.wait

        def wait(*args, **kwargs):
            result = original_wait(*args, **kwargs)
            reaped.set()
            return result

        process.wait = wait
        processes.append(process)
        real_waits.append(original_wait)
        return process

    monkeypatch.setattr(warm.subprocess, "Popen", child)
    if thread_failure:

        def unavailable():
            raise RuntimeError("cannot start thread")

        monkeypatch.setattr(warm.threading, "Thread", lambda **kwargs: SimpleNamespace(start=unavailable))
    try:
        assert warm._spawn("unused", str(tmp_path / "worker.sock"), None, dict(os.environ)) is not thread_failure
        assert reaped.wait(timeout=3)
        assert processes[0].returncode is not None
    finally:
        for process, wait in zip(processes, real_waits):
            if process.poll() is None:
                process.kill()
            wait(timeout=3)
