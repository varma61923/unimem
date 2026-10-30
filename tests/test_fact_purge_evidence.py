"""Hard erasure follows typed fact receipts, preserving other namespaces and holds."""

from pathlib import Path

import pytest

from unimem import fact_store, frontmatter, hierarchical, incident, lesson_admission, privacy_erasure
from unimem.fact_evidence import bind_evidence


@pytest.fixture(params=["jsonl", "sqlite"])
def evidence_store(tmp_path, request):
    root = str(tmp_path)
    if request.param == "sqlite":
        fact_store.migrate(root)
    source, _ = hierarchical.add_fact(root, "Secret premise retained for erasure testing")
    child, _ = hierarchical.add_fact(root, "Derived private claim", evidence=[bind_evidence(root, "fact", source.id)])
    grandchild, _ = hierarchical.add_fact(
        root, "Transitive private conclusion", evidence=[bind_evidence(root, "fact", child.id)]
    )
    path = Path(root, "memory", "lessons", f"lesson_{source.id}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"name": source.id, "status": "active", "description": "Independent lesson"}
    body = "Independent guidance that shares a textual identifier, not a fact source.\n"
    metadata[lesson_admission.RECEIPT_FIELD] = lesson_admission.issue(root, str(path), metadata, body, actor="reviewer")
    frontmatter.write(str(path), metadata, body)
    independent, _ = hierarchical.add_fact(
        root, "Independent lesson-supported claim", evidence=[bind_evidence(root, "lesson", source.id)]
    )
    return root, source, child, grandchild, independent


def test_hard_erasure_cascades_fact_receipts_without_crossing_lesson_namespace(evidence_store):
    root, source, child, grandchild, independent = evidence_store
    result = privacy_erasure.purge_fact(root, source.id, reason="owner request")
    assert set(result["deleted_facts"]) == {source.id, child.id, grandchild.id}
    assert set(hierarchical.load_facts(root)) == {independent.id}
    if fact_store.enabled(root):
        assert all(row.get("fact_id") not in {source.id, child.id, grandchild.id} for row in fact_store.events(root))


def test_a_hold_on_a_derived_fact_blocks_the_entire_erasure(evidence_store, monkeypatch):
    root, source, child, grandchild, independent = evidence_store
    monkeypatch.setattr(incident, "held", lambda _root: {"derived-hold": [grandchild.id]})
    with pytest.raises(PermissionError, match="legal hold"):
        privacy_erasure.purge_fact(root, source.id, reason="owner request")
    assert set(hierarchical.load_facts(root)) == {source.id, child.id, grandchild.id, independent.id}
