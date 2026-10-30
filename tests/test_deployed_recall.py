"""The deployed-gateway benchmark writes and asks over HTTP and checks parity with in-process recall."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks import deployed_recall

pytestmark = pytest.mark.e2e


def _locomo(path):
    conversation = {
        "speaker_a": "Ana",
        "speaker_b": "Ben",
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [
            {"speaker": "Ana", "dia_id": "D1:1", "text": "I adopted a beagle named Pepper last week."},
            {"speaker": "Ben", "dia_id": "D1:2", "text": "Congratulations! Where did you find her?"},
            {"speaker": "Ana", "dia_id": "D1:3", "text": "At the shelter on Pine Street."},
        ],
        "session_2_date_time": "10:00 am on 20 June, 2023",
        "session_2": [
            {"speaker": "Ben", "dia_id": "D2:1", "text": "I started learning the cello in June."},
        ],
    }
    qa = [
        {"question": "What is the name of Ana's dog?", "answer": "Pepper", "evidence": ["D1:1"], "category": 4},
        {"question": "Which instrument is Ben learning?", "answer": "cello", "evidence": ["D2:1"], "category": 4},
    ]
    path.write_text(json.dumps([{"sample_id": "conv-1", "conversation": conversation, "qa": qa}]))


@pytest.mark.parametrize("persistent", [True, False])
def test_gateway_serves_the_same_contexts_as_in_process_recall(tmp_path, monkeypatch, capsys, persistent):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    # URL-safe tokens may start with '-', which argparse otherwise treats as
    # another option when the token is passed as a separate argv item.
    monkeypatch.setattr(deployed_recall.secrets, "token_urlsafe", lambda _n: "-test-token-with-leading-dash")
    data = tmp_path / "locomo.json"
    _locomo(data)
    out = tmp_path / "result.json"
    roots = []
    original_gateway = deployed_recall.Gateway

    class TrackedGateway(original_gateway):
        def __init__(self, root):
            roots.append(Path(root))
            super().__init__(root)

    monkeypatch.setattr(deployed_recall, "Gateway", TrackedGateway)
    root_args = ["--root", str(tmp_path / "store")] if persistent else []
    code = deployed_recall.main(
        [
            "--dataset",
            "locomo",
            "--data",
            str(data),
            *root_args,
            "--budget",
            "200",
            "--adaptive-budget",
            "--max-budget",
            "400",
            "--concurrency",
            "2",
            "--out",
            str(out),
        ]
    )
    result = json.loads(out.read_text())
    assert code == 0
    assert result["parity"] == {"identical_contexts": 2, "of": 2}
    assert result["ingest"]["turns"] == 4 and result["questions"] == 2
    assert result["overall"]["evidence"] == 1.0
    assert result["recall"]["p50_ms"] > 0 and result["recall"]["queries_per_second"] > 0
    assert len(roots) == 1 and roots[0].exists() is persistent


def test_temporary_store_is_removed_when_gateway_startup_fails(tmp_path, monkeypatch):
    data = tmp_path / "locomo.json"
    _locomo(data)
    roots = []

    def fail_startup(root):
        roots.append(Path(root))
        assert roots[-1].is_dir()
        raise RuntimeError("startup failed")

    monkeypatch.setattr(deployed_recall, "Gateway", fail_startup)
    with pytest.raises(RuntimeError, match="startup failed"):
        deployed_recall.main(["--dataset", "locomo", "--data", str(data)])
    assert len(roots) == 1 and not roots[0].exists()


@pytest.mark.parametrize("empty_conversations", [True, False])
def test_empty_evaluations_are_rejected_before_starting_a_gateway(tmp_path, monkeypatch, empty_conversations):
    data = tmp_path / "locomo.json"
    _locomo(data)
    cases = json.loads(data.read_text())
    cases[0]["qa"] = []
    data.write_text(json.dumps([] if empty_conversations else cases))

    def unexpected_gateway(root):
        pytest.fail("an empty evaluation must not launch a gateway")

    monkeypatch.setattr(deployed_recall, "Gateway", unexpected_gateway)
    with pytest.raises(ValueError, match="no benchmark questions selected"):
        deployed_recall.main(["--dataset", "locomo", "--data", str(data)])
