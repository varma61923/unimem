"""Planning respects zero-result budgets and caller-supplied adapters."""

import pytest

from unimem import query_plan, search_recipes


def test_zero_limit_does_not_call_model_or_touch_store(tmp_path):
    def complete(_prompt):
        pytest.fail("zero-limit retrieval must not invoke the model")

    assert query_plan.retrieve(str(tmp_path / "absent"), "office", limit=0, complete=complete) == []
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("query", [None, "", "x" * 20001])
def test_zero_limit_still_validates_query(tmp_path, query):
    with pytest.raises(ValueError, match="query"):
        query_plan.retrieve(str(tmp_path), query, limit=0)


def test_falsey_backend_is_preserved(tmp_path, monkeypatch):
    class Backend:
        def __bool__(self):
            return False

        def neighbors(self, node_id, *, as_of=None):
            return []

    backend = Backend()

    def search(root, part, **options):
        assert options["backend"] is backend
        return [{"id": "x", "score": 1.0}]

    monkeypatch.setattr(search_recipes, "search", search)
    assert query_plan.retrieve(str(tmp_path), "office", backend=backend, entity_ids=[]) == [{"id": "x", "score": 1.0}]
