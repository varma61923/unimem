"""Malformed model identifiers never select another turn or abort indexing."""

import json

import pytest

from unimem.conversation import Store, anticipate


@pytest.mark.parametrize("identity", [None, True, 1.9, float("inf"), float("nan"), {}, []])
def test_invalid_identifiers_leave_batch_pending(tmp_path, monkeypatch, identity):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "validation") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": "The cat is named Miso."}])

        def complete(_):
            return json.dumps([{"id": identity, "questions": ["What is the cat named?"]}]), {}

        report = anticipate.expand(store, complete)
        assert report["failed_batches"] == 1
        assert report["indexed"] == 0
        assert len(anticipate.pending(store)) == 1


@pytest.mark.parametrize("identity", [1, "1"])
def test_valid_integer_identifiers_remain_supported(tmp_path, monkeypatch, identity):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "validation") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": "The cat is named Miso."}])

        def complete(_):
            return json.dumps([{"id": identity, "questions": ["What is the cat named?"]}]), {}

        assert anticipate.expand(store, complete)["indexed"] == 1
