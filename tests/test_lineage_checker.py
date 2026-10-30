"""lineage_checker must match lineage_blocked while reading each ledger once."""

from __future__ import annotations

import json
import os

import pytest

from unimem import _jsonl, hierarchical, memory_authority, paths


def _store(tmp_path, n=12):
    root = str(tmp_path)
    facts = [hierarchical.add_fact(root, f"service{i} deploys through pipeline{i}")[0] for i in range(n)]
    hierarchical.forget_fact(root, facts[0].id)
    hierarchical.forget_fact(root, facts[1].id)
    hierarchical.forget_fact(root, facts[1].id, undo=True)
    return root, facts


def test_checker_agrees_with_lineage_blocked(tmp_path):
    root, facts = _store(tmp_path)
    check = memory_authority.lineage_checker(root)
    ids = [f.id for f in facts] + ["unknown-id"]
    assert [check(i) for i in ids] == [memory_authority.lineage_blocked(root, i) for i in ids]
    assert check(facts[0].id) and not check(facts[1].id) and not check(facts[2].id)


def test_listing_reads_each_ledger_once_not_once_per_fact(tmp_path, monkeypatch):
    # Regression: list_facts re-read and re-verified forgetting.jsonl and
    # origins.jsonl for every fact, making a listing O(N * ledger size).
    root, facts = _store(tmp_path, n=25)
    reads: list[str] = []
    real = _jsonl.read_rows

    def counting(path):
        reads.append(os.path.basename(path))
        return real(path)

    monkeypatch.setattr(_jsonl, "read_rows", counting)
    listed = hierarchical.list_facts(root)
    assert facts[0].id not in {f.id for f in listed}
    assert len(listed) == len(facts) - 1
    assert reads.count("forgetting.jsonl") == 1
    assert reads.count("origins.jsonl") <= 1


def test_a_store_without_forgetting_never_reads_origins(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "nothing forgotten here")
    reads: list[str] = []
    real = _jsonl.read_rows
    monkeypatch.setattr(_jsonl, "read_rows", lambda p: reads.append(os.path.basename(p)) or real(p))
    assert memory_authority.lineage_checker(root)(fact.id) is False
    assert "origins.jsonl" not in reads


def test_a_tampered_forgetting_receipt_still_fails_closed(tmp_path):
    root, facts = _store(tmp_path)
    ledger = os.path.join(paths.memory_dir(root), "forgetting.jsonl")
    rows = _jsonl.read_rows(ledger)
    rows[0]["target_id"] = facts[5].id
    with open(ledger, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in rows)
    check = memory_authority.lineage_checker(root)
    with pytest.raises(PermissionError, match="invalid"):
        check(facts[2].id)
    with pytest.raises(PermissionError):
        hierarchical.list_facts(root)
