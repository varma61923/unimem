"""Regression tests ensuring compiled `.so` extensions and frozen binaries dispatch safely."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from unimem import hook_bridge, hook_kit, paths, warm


def test_warm_spawn_command_handles_compiled_so_and_frozen_modes(monkeypatch, tmp_path):
    script = str(tmp_path / "query.py")
    sock = str(tmp_path / "warm.sock")

    # Source checkout mode keeps explicit .py script invocation.
    cmd_src = warm._spawn_command(script, sock, str(tmp_path))
    assert cmd_src[0] == warm.sys.executable
    assert warm.__file__.endswith(".py")
    assert os.path.abspath(warm.__file__) in cmd_src

    # Compiled `.so` extension mode (no warm.py on disk) invokes via `-c from unimem.warm import main`.
    monkeypatch.setattr(warm, "__file__", str(tmp_path / "warm.cpython-314-x86_64-linux-gnu.so"))
    cmd_so = warm._spawn_command(script, sock, str(tmp_path))
    assert "-c" in cmd_so and any("unimem.warm" in arg for arg in cmd_so)
    assert all(not arg.endswith(".so") for arg in cmd_so)

    # Frozen standalone binary mode invokes internal subcommand instead of python `-P`.
    monkeypatch.setattr(warm.sys, "frozen", True, raising=False)
    cmd_frozen = warm._spawn_command(script, sock, None)
    assert cmd_frozen[:4] == [warm.sys.executable, "__internal_warm", "serve", "--script"]


def test_hook_kit_manifest_and_bridge_capture_handle_compiled_modes(monkeypatch, tmp_path):
    kit = hook_kit.HookKit(str(tmp_path), "agent-1", scope={"tenant": "acme"})

    # Source mode uses hook_bridge.py with `-I -S`.
    argv_src = kit._bridge_argv()
    assert "-I" in argv_src and "-S" in argv_src
    assert any(a.endswith("hook_bridge.py") for a in argv_src)

    # Compiled `.so` mode (missing hook_bridge.py on disk) runs inline bridge entry without `-S`.
    monkeypatch.setattr(hook_kit, "__file__", str(tmp_path / "hook_kit.cpython-314-x86_64-linux-gnu.so"))
    argv_so = kit._bridge_argv()
    assert "-S" not in argv_so
    assert "-c" in argv_so and any("unimem.hook_bridge" in arg for arg in argv_so)

    # Frozen binary mode dispatches cleanly via `__internal_hook_bridge` and `__internal_hook_kit`.
    monkeypatch.setattr(hook_kit.sys, "frozen", True, raising=False)
    argv_frozen = kit._bridge_argv()
    assert argv_frozen[:2] == [hook_kit.sys.executable, "__internal_hook_bridge"]

    captured: list[list[str]] = []

    class _Popen:
        def __init__(self, cmd, **kwargs):
            captured.append(cmd)

    monkeypatch.setattr(hook_bridge.sys, "frozen", True, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _Popen)
    args = hook_bridge._parse_args(["--dest", str(tmp_path), "--agent-id", "a", "--event", "Stop"])
    hook_bridge._capture(args, {"event_id": "e1"})
    assert captured and captured[0][:3] == [hook_bridge.sys.executable, "__internal_hook_kit", "--capture-only"]


def test_package_data_loaders_work_beside_compiled_so_without_source_py(monkeypatch, tmp_path):
    pkg = tmp_path / "unimem"
    (pkg / "schemas").mkdir(parents=True)
    (pkg / "schemas" / "fact.schema.json").write_text('{"type": "object"}\n', encoding="utf-8")
    fake_so = pkg / "paths.cpython-314-x86_64-linux-gnu.so"
    fake_so.write_bytes(b"")
    monkeypatch.setattr(paths, "__file__", str(fake_so))
    resolved = Path(paths.schemas_dir())
    assert resolved == pkg / "schemas"
    assert (resolved / "fact.schema.json").is_file()
