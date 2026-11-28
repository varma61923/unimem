#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Build-time dependency only: python3 -m pip install "nuitka>=2.6,<3.0" ordered-set zstandard
mkdir -p build dist
python3 -m nuitka \
  --standalone \
  --onefile \
  --lto=yes \
  --assume-yes-for-downloads \
  --python-flag=no_docstrings \
  --python-flag=-OO \
  --output-dir=build \
  --output-filename=unimem-local \
  --include-package=unimem \
  --include-package-data=unimem \
  --nofollow-import-to=torch,sentence_transformers,transformers,gliner \
  unimem/__main__.py
mv -f build/unimem-local dist/unimem-local
chmod +x dist/unimem-local
python3 scripts/compile_extensions.py check-binary dist/unimem-local
./dist/unimem-local --version
./dist/unimem-local evolve recipes >/dev/null
printf 'Standalone sealed native binary: dist/unimem-local\n'
