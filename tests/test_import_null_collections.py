"""Optional null export collections remain empty, without losing valid peers."""

import pytest

from unimem import interop


@pytest.mark.parametrize(
    "importer, document",
    [
        (interop.import_cognee_dump, {"chunks": None}),
        (interop.import_hindsight_dump, {"facts": None, "mental_models": None}),
        (interop.import_everos_dump, {"agent_skills": None, "agent_cases": None, "memcells": None}),
    ],
)
def test_null_optional_collections_are_empty(importer, document):
    assert importer(document) == []


def test_null_collection_does_not_discard_valid_sibling_facts():
    rows = interop.import_hindsight_dump({"mental_models": None, "facts": [{"id": "f", "text": "Miso is a cat."}]})
    assert len(rows) == 1
    assert rows[0]["data"]["statement"] == "Miso is a cat."
