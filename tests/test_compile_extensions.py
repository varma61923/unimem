"""Tests for native compilation discovery, stripping, source-leak auditing, and binary auditing."""

from __future__ import annotations

import subprocess

from scripts import compile_extensions as ce


def test_discover_and_strip_python_sources_keeps_shims_and_data_assets(tmp_path):
    pkg = tmp_path / "unimem"
    (pkg / "commands").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text("from unimem.cli import main\nmain()\n", encoding="utf-8")
    (pkg / "secret_engine.py").write_text("SECRET_FORMULA = 42\n", encoding="utf-8")
    (pkg / "secret_engine.c").write_text("/* generated C */\n", encoding="utf-8")
    (pkg / "secret_engine.cpython-314-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    (pkg / "commands" / "query_cmd.py").write_text("def run(): pass\n", encoding="utf-8")
    (pkg / "commands" / "query_cmd.cpython-314-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    (pkg / "schema.json").write_text("{}\n", encoding="utf-8")
    (pkg / "__pycache__" / "secret_engine.cpython-314.pyc").write_bytes(b"pyc")

    discovered = ce.discover_modules(pkg)
    dotted = [name for name, _, _ in discovered]
    assert dotted == ["unimem.commands.query_cmd", "unimem.secret_engine"]

    violations = ce.audit_no_source([pkg])
    assert any("uncompiled Python source" in v and "secret_engine.py" in v for v in violations)
    assert any("__pycache__" in v for v in violations)
    assert any("secret_engine.c" in v for v in violations)

    ce.strip_python_sources(pkg, [path for _, path, _ in discovered])
    assert ce.audit_no_source([pkg]) == []
    assert (pkg / "__init__.py").is_file()
    assert (pkg / "__main__.py").is_file()
    assert (pkg / "schema.json").is_file()
    assert (pkg / "secret_engine.cpython-314-x86_64-linux-gnu.so").is_file()
    assert not (pkg / "secret_engine.py").exists()


def test_trampoline_staging_moves_source_out_and_passes_no_source_audit(tmp_path):
    pkg = tmp_path / "hub"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "manage.py").write_text("SECRET_LOGIC = 'hidden'\ndef main(): return 0\n", encoding="utf-8")

    discovered = ce.discover_modules(pkg, keep_trampolines=frozenset({"manage.py"}))
    assert discovered[0][0] == "hub._manage_ext"
    targets = ce.stage_trampolines(discovered)
    assert targets == [("hub._manage_ext", pkg / "_manage_ext.py")]
    assert "SECRET_LOGIC" not in (pkg / "manage.py").read_text(encoding="utf-8")
    assert "SECRET_LOGIC" in (pkg / "_manage_ext.py").read_text(encoding="utf-8")

    ce.strip_python_sources(pkg, [path for _, path in targets])
    assert ce.audit_no_source([pkg]) == []
    assert (pkg / "manage.py").is_file()


def test_audit_binary_rejects_pyinstaller_and_non_native_scripts(tmp_path):
    script = tmp_path / "fake.sh"
    script.write_text("#!/bin/sh\necho secret\n", encoding="utf-8")
    assert ce.audit_binary(script)

    pyinstaller_bin = tmp_path / "pyinstaller-elf"
    pyinstaller_bin.write_bytes(b"\x7fELF" + b"\x00" * 128 + b"PYZ\x00" + b"bytecode")
    violations = ce.audit_binary(pyinstaller_bin)
    assert any("PyInstaller" in v for v in violations)

    native_elf = tmp_path / "native-elf"
    native_elf.write_bytes(b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 128)
    assert ce.audit_binary(native_elf) == []


def test_cli_check_no_source_and_nuitka_invocation(tmp_path, monkeypatch):
    pkg = tmp_path / "hub"
    (pkg / "alembic" / "versions").mkdir(parents=True)
    (pkg / "alembic" / "versions" / "001.py").write_text("# migration\n", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "billing.py").write_text("PRICE = 10\n", encoding="utf-8")

    assert ce.main(["check-no-source", str(pkg)]) == 1

    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: calls.append(cmd))
    built = ce.build_nuitka_extensions(pkg, [("hub.billing", pkg / "billing.py")], strip_symbols=False)
    assert built == [pkg / "billing.py"]
    assert "--module" in calls[0] and "--python-flag=no_docstrings" in calls[0]

    ce.strip_python_sources(pkg, built)
    assert ce.main(["check-no-source", str(pkg)]) == 0
