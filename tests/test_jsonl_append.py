"""Appending a JSONL row must never leave a truncated line behind."""

from __future__ import annotations

import os

import pytest

from unimem import _jsonl


def test_a_short_write_is_completed_not_truncated(tmp_path, monkeypatch):
    real_write = os.write

    def dribble(fd, data):
        # Write at most 7 bytes per call, as a signal-interrupted or
        # nearly-full filesystem may.
        return real_write(fd, bytes(data[:7]))

    monkeypatch.setattr(_jsonl.os, "write", dribble)
    path = str(tmp_path / "rows.jsonl")
    row = {"id": "a" * 40, "text": "été " * 20}
    _jsonl.append_row(path, row)
    _jsonl.append_row(path, {"id": "b"})
    assert _jsonl.read_rows(path) == [row, {"id": "b"}]


def test_a_write_that_makes_no_progress_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(_jsonl.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="no progress"):
        _jsonl.append_row(str(tmp_path / "rows.jsonl"), {"id": "a"})


def test_a_durable_append_syncs_the_descriptor_it_wrote_with(tmp_path, monkeypatch):
    real_open, real_fsync = os.open, os.fsync
    opened, synced = [], []

    def spy_open(path, flags, *args):
        fd = real_open(path, flags, *args)
        opened.append(fd)
        return fd

    def spy_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(_jsonl.os, "open", spy_open)
    monkeypatch.setattr(_jsonl.os, "fsync", spy_fsync)
    path = str(tmp_path / "rows.jsonl")
    _jsonl.append_row(path, {"id": "a"}, fsync=True)
    assert len(opened) == 1 and synced == opened
    _jsonl.append_row(path, {"id": "b"})
    assert len(synced) == 1
    assert _jsonl.read_rows(path) == [{"id": "a"}, {"id": "b"}]


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_nofollow_refuses_a_symlinked_journal_before_writing(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    link = tmp_path / "rows.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _jsonl.append_row(str(link), {"id": "a"}, nofollow=True)
    assert target.read_text() == ""
