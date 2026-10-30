import pytest

from unimem.context_template import render_slots, select


@pytest.mark.parametrize("template", ["%{ }", "%{   }"])
def test_empty_slot_names_are_rejected_as_invalid_templates(template):
    with pytest.raises(ValueError, match="slot requires a name"):
        select(template, {})


def test_selection_does_not_enumerate_past_the_requested_limit():
    def rows():
        yield {"id": "one", "text": "first"}
        yield {"id": "two", "text": "second"}
        raise AssertionError("unselected rows must not be enumerated")

    assert [row["id"] for row in select("%{lessons limit=2}", {"lessons": rows()})] == ["one", "two"]


def test_bounded_slots_escape_and_replace_delta():
    slots = {
        "lessons": [
            {"id": "one", "text": "<tool>pytest</tool>", "kind": "pitfall"},
            {"id": "two", "text": "other", "kind": "procedure"},
        ]
    }
    rendered = render_slots("%{lessons limit=1 types=[pitfall]}", slots, budget=100, previous="stable")
    assert rendered.startswith("stable\n<memory_update>")
    assert "&lt;tool&gt;" in rendered and "other" not in rendered
    assert render_slots("%{lessons limit=1}", slots, budget=0, previous=rendered) == "stable"
    with pytest.raises(ValueError):
        render_slots("%{unknown limit=1}", slots, budget=100)
    with pytest.raises(ValueError):
        render_slots("%{lessons limit=999}", slots, budget=100)
