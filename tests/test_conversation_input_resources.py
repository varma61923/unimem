"""Conversation imports own file inputs but borrow stdin."""

import builtins
import io
from pathlib import Path

import pytest

from unimem.commands import conversation_cmd


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('[{"text": "hello"}]', [{"text": "hello"}]),
        ('{"text": "hello"}\n{"text": "again"}\n', [{"text": "hello"}, {"text": "again"}]),
        (" \n", []),
        ("[", ValueError),
        ("42\n", ValueError),
    ],
)
def test_message_input_files_close_on_success_and_parse_failure(tmp_path, monkeypatch, payload, expected):
    source = tmp_path / "messages.json"
    source.write_text(payload, encoding="utf-8")
    opened = []

    def track(original):
        def open_input(*args, **kwargs):
            stream = original(*args, **kwargs)
            opened.append(stream)
            return stream

        return open_input

    monkeypatch.setattr(builtins, "open", track(builtins.open))
    monkeypatch.setattr(io, "open", track(io.open))
    monkeypatch.setattr(Path, "open", track(Path.open))
    if expected is ValueError:
        with pytest.raises(ValueError):
            conversation_cmd._read_messages(str(source))
    else:
        assert conversation_cmd._read_messages(str(source)) == expected
    assert opened and all(stream.closed for stream in opened)


def test_message_input_does_not_close_borrowed_stdin(monkeypatch):
    source = io.StringIO('{"text": "hello"}\n')
    monkeypatch.setattr(conversation_cmd.sys, "stdin", source)
    assert conversation_cmd._read_messages("-") == [{"text": "hello"}]
    assert not source.closed
