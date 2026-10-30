from unimem import fact_store, hierarchical
from unimem.privacy_erasure import purge_fact


def test_hard_fact_erasure_cascades_history_and_vectors(tmp_path):
    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "privatephrase parser checksum")
    result = purge_fact(root, fact.id, reason="owner request")
    assert fact.id in result["deleted_facts"]
    assert not hierarchical.load_facts(root)
    assert not fact_store.events(root)
    assert b"privatephrase" not in (tmp_path / "memory" / "facts" / "facts.sqlite3").read_bytes()


def test_hard_erasure_removes_dense_candidates_and_control_revisions(tmp_path):
    import asyncio
    import sqlite3

    from unimem import memory_control
    from unimem.fact_vector_projection import index_path
    from unimem.vector_store import SQLiteVectorIndex, VectorRecord

    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "sensitivephrase parser checksum")
    memory_control.put(
        root, "mental-model", "sensitivephrase derivative", record_id="derived", data={"sources": [fact.id]}
    )

    async def seed():
        index = await SQLiteVectorIndex.open(
            index_path(root), tenant="local", namespace="facts", model="test", dimension=2
        )
        await index.upsert([VectorRecord(fact.id, [1, 0])])
        await index.close()

    asyncio.run(seed())
    purge_fact(root, fact.id, reason="erase all materializations")
    with sqlite3.connect(index_path(root)) as db:
        assert db.execute("SELECT count(*) FROM unimem_vectors").fetchone()[0] == 0
    assert not list((tmp_path / "memory" / "controls" / "mental-model").glob("*.md"))


def test_hard_erasure_unlinks_extracted_entity_names(tmp_path):
    # Regression: purge_fact removed the fact but left the names extracted from
    # its text (often the very PII being erased) in memory/entities/.
    from unimem import entity_store

    root = str(tmp_path)
    erased, _ = hierarchical.add_fact(root, 'Patient "Alice Johnson" lives at ZetaCorp.Office-7')
    kept, _ = hierarchical.add_fact(root, 'Customer "Grace Hopper" prefers email')
    assert "alice johnson" in entity_store.load_entities(root)
    result = purge_fact(root, erased.id, reason="gdpr")
    assert "entity index links" in result["erased_surfaces"]
    entities = entity_store.load_entities(root)
    assert "alice johnson" not in entities
    assert "zetacorp.office-7" not in entities
    assert all(erased.id not in e["memory_ids"] for e in entities.values())
    assert kept.id in entities["grace hopper"]["memory_ids"]
    assert b"Alice Johnson".lower() not in (tmp_path / "memory" / "entities" / "entities.jsonl").read_bytes().lower()
