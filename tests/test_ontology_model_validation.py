"""Corrupt optional typing models degrade to the existing offline evidence."""

import json

import pytest

from unimem import ontology, ontology_classify, ontology_learned


@pytest.mark.parametrize("data", [None, [], "invalid"])
def test_model_root_must_be_an_object(data):
    with pytest.raises(ValueError):
        ontology_learned.Model.from_dict(data)


@pytest.mark.parametrize(
    "override",
    [
        {"format": True},
        {"labels": []},
        {"labels": ["person", "person"], "bias": [0.0, 0.0]},
        {"weights": []},
        {"weights": {"feature": []}},
        {"bias": [float("nan")]},
        {"bias": [True]},
        {"weights": {"feature": [float("inf")]}},
        {"meta": []},
        {"bias": [10**1000]},
    ],
)
def test_invalid_artifacts_are_unavailable_instead_of_crashing_load(tmp_path, monkeypatch, override):
    data = {"format": 1, "labels": ["person"], "weights": {}, "bias": [0.0], "meta": {}}
    data.update(override)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(ontology_learned, "MODEL_FILE", str(path))
    monkeypatch.setattr(ontology_learned, "_MODEL", None)
    monkeypatch.setattr(ontology_learned, "_LOADED", False)
    assert ontology_learned.load() is None


def test_training_requires_examples():
    with pytest.raises(ValueError, match="training rows"):
        ontology_learned.train([])


def test_models_own_their_validated_vectors():
    labels, weights, bias = ["person"], {"shape=Xx": [1.0]}, [0.0]
    model = ontology_learned.Model(labels, weights, bias, {})
    labels.clear()
    weights["shape=Xx"].clear()
    bias.clear()
    assert model.probabilities("Alice", "Alice arrived.") == {"person": 1.0}


def test_overflowing_optional_scores_preserve_rule_based_classification(monkeypatch):
    model = ontology_learned.Model(["person"], {"shape=x.x": [1e308]}, [1e308], {})
    with pytest.raises(ValueError, match="scores must be finite"):
        model.probabilities("main.py", "Edit main.py.")
    monkeypatch.setenv("UNIMEM_TYPING_MODEL", "on")
    monkeypatch.setattr(ontology_learned, "load", lambda: model)
    result = ontology_classify.classify(
        "main.py", "Edit main.py.", onto=ontology.Ontology.default(), methods=("keyword",)
    )
    assert result.type == "file"
    assert "learned" not in {source for source, _kind, _score in result.evidence}
