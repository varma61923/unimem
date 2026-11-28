#!/usr/bin/env python3
"""Compile Python packages into native C/Rust extensions and audit zero source leakage."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

DEFAULT_SHIMS = frozenset({"__init__.py", "__main__.py"})
DEFAULT_EXCLUDED_DIRS = frozenset({".git", "__pycache__", "tests", "alembic"})
FORBIDDEN_SUFFIXES = frozenset({".pyc", ".pyo", ".c", ".h", ".pxd", ".pyx"})
PYINSTALLER_MARKERS = (b"MEI\x0c\x0b\x0a\x0b\x0e", b"PYZ\x00")
TRAMPOLINE_HEADER = "# Compiled extension trampoline (no proprietary logic)."
MAX_TRAMPOLINE_BYTES = 512


def _trampoline_source(target_dotted: str) -> str:
    return (
        f"{TRAMPOLINE_HEADER}\n"
        "import importlib as _imp\n\n"
        f"_ext = _imp.import_module({target_dotted!r})\n"
        '_SKIP = {"__name__", "__package__", "__file__", "__loader__", "__spec__"}\n'
        "globals().update({k: v for k, v in vars(_ext).items() if k not in _SKIP})\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(_ext.main())\n"
    )


def _is_trampoline_file(path: Path) -> bool:
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if len(raw) > MAX_TRAMPOLINE_BYTES:
        return False
    return raw.decode("utf-8", errors="ignore").startswith(TRAMPOLINE_HEADER)


def discover_modules(
    root: Path,
    *,
    package_prefix: str | None = None,
    keep_shims: frozenset[str] = DEFAULT_SHIMS,
    keep_trampolines: frozenset[str] = frozenset(),
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDED_DIRS,
) -> list[tuple[str, Path, Path | None]]:
    """Return `(dotted_name, compile_source_path, trampoline_path_or_None)` tuples."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"compilation root does not exist: {root}")
    prefix = (package_prefix or root.name).strip(".")
    modules: list[tuple[str, Path, Path | None]] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs)
        filenames.sort()
        cur_path = Path(current)
        rel_dir = cur_path.relative_to(root)
        for filename in filenames:
            if not filename.endswith(".py") or filename in keep_shims:
                continue
            src = cur_path / filename
            stem = filename[:-3]
            if filename in keep_trampolines:
                ext_stem = f"_{stem}_ext"
                rel_parts = (*rel_dir.parts, ext_stem) if rel_dir.parts else (ext_stem,)
                dotted = ".".join((prefix, *rel_parts)) if prefix else ".".join(rel_parts)
                ext_src = cur_path / f"{ext_stem}.py"
                modules.append((dotted, ext_src, src))
            else:
                rel_parts = (*rel_dir.parts, stem) if rel_dir.parts else (stem,)
                dotted = ".".join((prefix, *rel_parts)) if prefix else ".".join(rel_parts)
                modules.append((dotted, src, None))
    modules.sort(key=lambda item: item[0])
    return modules


def stage_trampolines(modules: Sequence[tuple[str, Path, Path | None]]) -> list[tuple[str, Path]]:
    """Prepare `_<name>_ext.py` sources and write verified `.py` loader trampolines."""
    compile_targets: list[tuple[str, Path]] = []
    for dotted, ext_src, orig_src in modules:
        if orig_src is not None:
            shutil.move(str(orig_src), str(ext_src))
            orig_src.write_text(_trampoline_source(dotted), encoding="utf-8")
        compile_targets.append((dotted, ext_src))
    return compile_targets


def strip_python_sources(
    root: Path,
    compiled_paths: Sequence[Path],
    *,
    keep_shims: frozenset[str] = DEFAULT_SHIMS,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDED_DIRS,
) -> list[Path]:
    """Delete compiled `.py` sources, C intermediates, bytecode files, and `__pycache__` dirs."""
    root = Path(root).resolve()
    removed: list[Path] = []
    for path in compiled_paths:
        resolved = Path(path).resolve()
        if resolved.name not in keep_shims and resolved.is_file():
            resolved.unlink()
            removed.append(resolved)

    for current, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        cur_path = Path(current)
        if any(part in exclude_dirs - {"__pycache__", "tests"} for part in cur_path.relative_to(root).parts):
            continue
        if cur_path.name == "__pycache__":
            shutil.rmtree(cur_path)
            removed.append(cur_path)
            continue
        for filename in filenames:
            target = cur_path / filename
            if target.suffix in FORBIDDEN_SUFFIXES:
                target.unlink()
                removed.append(target)
    return removed


def _strip_shared_objects(root: Path, strip_bin: str | None = None) -> None:
    tool = strip_bin if strip_bin is not None else shutil.which("strip")
    if not tool:
        return
    shared_objects = [str(p) for p in Path(root).rglob("*.so") if p.is_file()]
    for start in range(0, len(shared_objects), 64):
        subprocess.run([tool, "--strip-unneeded", *shared_objects[start : start + 64]], check=True)


def build_cython_extensions(
    root: Path,
    modules: Sequence[tuple[str, Path]],
    *,
    strip_symbols: bool = True,
) -> list[Path]:
    """Compile Python modules in-place to stripped Cython `.so` extensions."""
    if not modules:
        return []
    from Cython.Build import cythonize
    from setuptools import Distribution, Extension

    root = Path(root).resolve()
    parent_dir = root.parent
    extensions = [
        Extension(
            name=dotted,
            sources=[os.path.relpath(src, parent_dir)],
            extra_compile_args=["-O2", "-fvisibility=hidden"],
        )
        for dotted, src in modules
    ]
    directives = {
        "language_level": "3",
        "binding": True,
        "embedsignature": True,
        "docstrings": False,
        "always_allow_keywords": True,
        "annotation_typing": False,
    }
    cwd = os.getcwd()
    try:
        os.chdir(parent_dir)
        with tempfile.TemporaryDirectory(prefix="unimem-cython-") as tmp:
            tmp_path = Path(tmp)
            ext_modules = cythonize(
                extensions,
                compiler_directives=directives,
                build_dir=str(tmp_path / "c"),
                nthreads=max(2, os.cpu_count() or 2),
                quiet=True,
            )
            dist = Distribution({"name": "unimem-compiled", "ext_modules": ext_modules})
            cmd = dist.get_command_obj("build_ext")
            cmd.inplace = True
            cmd.build_temp = str(tmp_path / "obj")
            cmd.build_lib = str(tmp_path / "lib")
            cmd.ensure_finalized()
            cmd.run()
    finally:
        os.chdir(cwd)

    if strip_symbols:
        _strip_shared_objects(root)
    return [path for _, path in modules]


def build_nuitka_extensions(
    root: Path,
    modules: Sequence[tuple[str, Path]],
    *,
    strip_symbols: bool = True,
    runner=None,
) -> list[Path]:
    """Compile Python modules via Nuitka `--module` into standalone `.so` extensions."""
    if not modules:
        return []
    run = runner or subprocess.run
    root = Path(root).resolve()
    compiled: list[Path] = []
    for _dotted, src in modules:
        run(
            [
                sys.executable,
                "-m",
                "nuitka",
                "--module",
                "--lto=yes",
                "--python-flag=no_docstrings",
                "--python-flag=-OO",
                "--remove-output",
                f"--output-dir={src.parent}",
                str(src),
            ],
            check=True,
        )
        compiled.append(src)
    if strip_symbols:
        _strip_shared_objects(root)
    return compiled


def audit_no_source(
    roots: Sequence[Path],
    *,
    allow_shims: frozenset[str] = DEFAULT_SHIMS,
    exclude_dirs: frozenset[str] = frozenset({"alembic"}),
    allow_tests: bool = False,
) -> list[str]:
    """Return human-readable violation strings if any uncompiled source or bytecode remains."""
    violations: list[str] = []
    for root_arg in roots:
        root = Path(root_arg).resolve()
        if not root.exists():
            violations.append(f"missing audit directory: {root}")
            continue
        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            cur_path = Path(current)
            if ".git" in dirnames:
                violations.append(f"forbidden VCS directory: {cur_path / '.git'}")
            if not allow_tests and "tests" in dirnames:
                violations.append(f"forbidden test directory: {cur_path / 'tests'}")
            if "__pycache__" in dirnames:
                violations.append(f"forbidden bytecode directory: {cur_path / '__pycache__'}")
            dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs and d != ".git")
            for filename in sorted(filenames):
                target = cur_path / filename
                if target.suffix in FORBIDDEN_SUFFIXES:
                    violations.append(f"forbidden artifact/intermediate: {target}")
                elif target.suffix == ".py" and filename not in allow_shims and not _is_trampoline_file(target):
                    violations.append(f"uncompiled Python source: {target}")
    return violations


def audit_binary(binary_path: Path) -> list[str]:
    """Verify a standalone binary is a native executable with no PyInstaller archive markers."""
    path = Path(binary_path)
    violations: list[str] = []
    if not path.is_file():
        return [f"missing binary: {path}"]
    with path.open("rb") as handle:
        head = handle.read(64)
        if not head.startswith((b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"MZ")):
            violations.append(f"not a recognized native executable header: {path}")
        handle.seek(0)
        overlap = b""
        while chunk := handle.read(1024 * 1024):
            window = overlap + chunk
            for marker in PYINSTALLER_MARKERS:
                if marker in window:
                    violations.append(f"forbidden extractable PyInstaller archive marker in {path}")
                    break
            overlap = window[-32:]
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    comp = sub.add_parser("compile", help="Compile modules in-place to stripped native extensions.")
    comp.add_argument("--root", action="append", required=True, type=Path, help="Package directory to compile.")
    comp.add_argument("--package-prefix", action="append", default=[], help="Optional dotted prefix matching --root.")
    comp.add_argument("--compiler", choices=("cython", "nuitka"), default="cython")
    comp.add_argument("--strip-sources", action="store_true", help="Remove compiled `.py` files and C intermediates.")
    comp.add_argument("--keep-shim", action="append", default=[], help="Basename to leave as `.py` entrypoint/marker.")
    comp.add_argument(
        "--keep-trampoline",
        action="append",
        default=[],
        help="Compile module to `_<name>_ext.so` and leave a minimal `python -m` trampoline.",
    )
    comp.add_argument("--exclude-dir", action="append", default=[], help="Subdirectory names to skip.")
    comp.add_argument("--no-strip-symbols", action="store_true", help="Skip `strip --strip-unneeded` on `.so` outputs.")

    nosrc = sub.add_parser("check-no-source", help="Fail if non-shim `.py` or `.pyc`/`.c` sources exist.")
    nosrc.add_argument("roots", nargs="+", type=Path)
    nosrc.add_argument("--allow-shim", action="append", default=[], help="Basename permitted as a `.py` shim.")
    nosrc.add_argument("--exclude-dir", action="append", default=["alembic"], help="Directory names exempt from check.")
    nosrc.add_argument("--allow-tests", action="store_true")

    chkbin = sub.add_parser("check-binary", help="Audit a compiled standalone native binary.")
    chkbin.add_argument("binary", type=Path)

    args = parser.parse_args(argv)
    if args.command == "compile":
        shims = DEFAULT_SHIMS | frozenset(args.keep_shim)
        trampolines = frozenset(args.keep_trampoline)
        excluded = DEFAULT_EXCLUDED_DIRS | frozenset(args.exclude_dir)
        prefixes = list(args.package_prefix)
        for index, root in enumerate(args.root):
            prefix = prefixes[index] if index < len(prefixes) else None
            discovered = discover_modules(
                root,
                package_prefix=prefix,
                keep_shims=shims,
                keep_trampolines=trampolines,
                exclude_dirs=excluded,
            )
            modules = stage_trampolines(discovered)
            if args.compiler == "cython":
                built = build_cython_extensions(root, modules, strip_symbols=not args.no_strip_symbols)
            else:
                built = build_nuitka_extensions(root, modules, strip_symbols=not args.no_strip_symbols)
            if args.strip_sources:
                strip_python_sources(root, built, keep_shims=shims, exclude_dirs=excluded)
        return 0

    if args.command == "check-no-source":
        shims = DEFAULT_SHIMS | frozenset(args.allow_shim)
        violations = audit_no_source(
            args.roots,
            allow_shims=shims,
            exclude_dirs=frozenset(args.exclude_dir),
            allow_tests=args.allow_tests,
        )
        for item in violations:
            print(f"source-leak-audit: {item}", file=sys.stderr)
        return 1 if violations else 0

    if args.command == "check-binary":
        violations = audit_binary(args.binary)
        for item in violations:
            print(f"binary-audit: {item}", file=sys.stderr)
        return 1 if violations else 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
