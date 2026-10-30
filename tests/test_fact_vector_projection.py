import json
import sqlite3

from unimem import fact_embeddings, fact_store, fact_vector_projection, hierarchical, search_recipes


class Model:
    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.extend(texts)
        return [[1.0, 0.0] if "grammar" in text or "parse" in text else [0.0, 1.0] for text in texts]


def test_event_projection_dense_union_restart_and_revocation(tmp_path, monkeypatch):
    root = str(tmp_path)
    model = Model()
    monkeypatch.setattr(fact_embeddings, "_resolve", lambda *_: (model, ("tag", "projection-test")))
    fact_store.migrate(root)
    wanted, _ = hierarchical.add_fact(root, "grammar boundary rules", scopes=["agent:a"])
    secret, _ = hierarchical.add_fact(root, "grammar private rules", scopes=["agent:b"])
    assert fact_vector_projection.drain(root, model="projection-test")["processed"] == 2
    calls = len(model.calls)
    fact_embeddings._VECTORS.clear()
    assert fact_vector_projection.drain(root, model="projection-test")["processed"] == 0
    assert len(model.calls) == calls
    report = {}
    results = search_recipes.search(root, "parse", embedder="projection-test", context=["agent:a"], explain=report)
    assert [row["id"] for row in results] == [wanted.id]
    assert report["dense_candidates"]
    hierarchical.forget_fact(root, wanted.id)
    assert search_recipes.search(root, "parse", embedder="projection-test", context=["agent:a"]) == []


def test_pending_canonical_revision_cannot_use_old_dense_scores(tmp_path, monkeypatch):
    root, model = str(tmp_path), Model()
    monkeypatch.setattr(fact_embeddings, "_resolve", lambda *_: (model, ("tag", "projection-test")))
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "grammar boundary rules")
    fact_vector_projection.drain(root, model="projection-test")
    assert fact_vector_projection.search(root, "parse", model="projection-test")
    with hierarchical.mutate_facts(root) as facts:
        facts[fact.id].statement = "database retention rules"
    assert fact_vector_projection.search(root, "parse", model="projection-test") == []
    fact_vector_projection.drain(root, model="projection-test")
    assert fact_vector_projection.search(root, "database", model="projection-test")[0][0] == fact.id


def test_event_freshness_index_upgrades_existing_stores_and_bounds_revision_lookup(tmp_path):
    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "grammar boundary rules")
    for revision in range(4):
        with hierarchical.mutate_facts(root) as facts:
            facts[fact.id].statement = f"grammar boundary rules revision {revision}"

    # Model an older store whose schema predates the optional lookup index.
    fact_store.close_connections(root)
    with sqlite3.connect(fact_store.database_path(root)) as connection:
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        connection.execute("DROP INDEX IF EXISTS event_fact_seq")
    fact_store.close_connections(root)

    with fact_store.transaction(root) as facts:
        connection = facts.connection
        index = connection.execute("PRAGMA index_list(events)").fetchall()
        assert any(row[1] == "event_fact_seq" for row in index)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT candidate.value FROM json_each(?) AS candidate "
                "WHERE EXISTS (SELECT 1 FROM events e "
                "WHERE e.fact_id=candidate.value AND e.seq>?)",
                (json.dumps([fact.id]), 0),
            )
        )
        assert "event_fact_seq" in plan
        assert "seq>?" in plan
