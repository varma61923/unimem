"""Gateway benchmark failures release children and diagnostics without pipe stalls."""

import subprocess
import sys

import pytest

from benchmarks import deployed_recall as bench


class Process:
    def __init__(self, stderr, *, exited=False, stubborn=False):
        self.stderr = stderr
        self.exited = exited
        self.stubborn = stubborn
        self.actions = []

    def poll(self):
        return 1 if self.exited else None

    def terminate(self):
        self.actions.append("terminate")

    def kill(self):
        self.actions.append("kill")

    def wait(self, timeout):
        self.actions.append("wait")
        if self.stubborn and "kill" not in self.actions:
            raise subprocess.TimeoutExpired("gateway", timeout)
        return 1


def test_startup_exit_bounds_diagnostics_and_closes_the_file(monkeypatch):
    processes = []

    def launch(*args, stderr, **kwargs):
        stderr.write(b"x" * 2000 + b"failure-tail")
        stderr.flush()
        process = Process(stderr, exited=True)
        processes.append(process)
        return process

    monkeypatch.setattr(bench.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="failure-tail") as error:
        bench.Gateway("unused")
    assert len(str(error.value)) < 600
    assert processes[0].stderr.closed
    assert processes[0].actions == ["wait"]


def test_startup_timeout_kills_reaps_and_closes_a_stubborn_child(monkeypatch):
    processes = []

    def launch(*args, stderr, **kwargs):
        process = Process(stderr, stubborn=True)
        processes.append(process)
        return process

    clock = iter([0.0, 61.0])
    monkeypatch.setattr(bench.subprocess, "Popen", launch)
    monkeypatch.setattr(bench.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not become ready"):
        bench.Gateway("unused")
    assert processes[0].stderr.closed
    assert processes[0].actions == ["terminate", "wait", "kill", "wait"]


def test_health_probes_cannot_use_the_bulk_request_timeout(monkeypatch):
    timeouts = []

    def launch(*args, stderr, **kwargs):
        return Process(stderr)

    def health(self, method, path, *, timeout=None):
        timeouts.append(timeout)
        return 200, {}

    monkeypatch.setattr(bench.subprocess, "Popen", launch)
    monkeypatch.setattr(bench.Gateway, "call", health)
    gateway = bench.Gateway("unused")
    try:
        assert len(timeouts) == 1 and 0 < timeouts[0] <= 1
    finally:
        gateway.close()


def test_verbose_child_cannot_block_on_an_undrained_stderr_pipe(monkeypatch):
    launch = subprocess.Popen
    processes = []
    diagnostics = []

    def launch_writer(*args, stderr, **kwargs):
        process = launch(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1); sys.stderr.buffer.write(b'x' * 200000)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        processes.append(process)
        diagnostics.append(stderr)
        return process

    def health(self, method, path, *, timeout=300):
        self.process.stdin.write(b"x")
        self.process.stdin.close()
        self.process.wait(timeout=3)
        return 200, {}

    monkeypatch.setattr(bench.subprocess, "Popen", launch_writer)
    monkeypatch.setattr(bench.Gateway, "call", health)
    gateway = None
    try:
        gateway = bench.Gateway("unused")
        assert diagnostics[0].seek(0, 2) == 200000
    finally:
        if gateway is not None:
            gateway.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            if process.stdin is not None:
                process.stdin.close()
            if process.stderr is not None:
                process.stderr.close()
    assert diagnostics[0].closed
