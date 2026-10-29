"""Local generation validates limits and preserves revision/token identities."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from unimem import local_llm, offline
from unimem.llm import LLMUnavailable


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "5"])
def test_invalid_limits_fail_before_loading_weights(monkeypatch, limit):
    def unexpected_load(model):
        pytest.fail("invalid generation limits must not load model weights")

    monkeypatch.setattr(local_llm, "_load", unexpected_load)
    with pytest.raises(ValueError, match="positive"):
        local_llm.complete(SimpleNamespace(model="unused"), "prompt", max_new_tokens=limit)


@pytest.fixture
def model_runtime(monkeypatch):
    calls = []

    def tokenizer(model, **kwargs):
        calls.append(("tokenizer", model, kwargs))
        return object()

    def network(model, **kwargs):
        calls.append(("network", model, kwargs))
        return SimpleNamespace(eval=lambda: None)

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(float32="float32"))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer),
        AutoModelForCausalLM=SimpleNamespace(from_pretrained=network),
    ))
    monkeypatch.setattr(local_llm, "_LOADED", {})
    monkeypatch.setattr(offline, "enabled", lambda: True)
    return calls


def test_model_cache_distinguishes_pinned_revisions(monkeypatch, model_runtime):
    monkeypatch.setenv("UNIMEM_LOCAL_LLM_REVISION", "revision-a")
    first = local_llm._load("example/model")
    monkeypatch.setenv("UNIMEM_LOCAL_LLM_REVISION", "revision-b")
    second = local_llm._load("example/model")
    monkeypatch.setenv("UNIMEM_LOCAL_LLM_REVISION", "revision-a")
    assert local_llm._load("example/model") is first
    assert second is not first
    assert [kwargs["revision"] for kind, _model, kwargs in model_runtime if kind == "network"] == [
        "revision-a", "revision-b",
    ]
    assert all(kwargs["local_files_only"] for _kind, _model, kwargs in model_runtime)


def test_model_validation_failures_use_the_provider_unavailable_contract(monkeypatch, model_runtime):
    def unsupported(*args, **kwargs):
        raise ValueError("unsupported model architecture")

    monkeypatch.setattr(sys.modules["transformers"].AutoTokenizer, "from_pretrained", unsupported)
    with pytest.raises(LLMUnavailable, match="could not be loaded"):
        local_llm._load("unsupported/model")


def test_zero_is_a_valid_padding_token_id(monkeypatch):
    class Tokens(list):
        @property
        def shape(self):
            return (len(self),)

        def __getitem__(self, key):
            result = super().__getitem__(key)
            return Tokens(result) if isinstance(key, slice) else result

    class Tokenizer:
        pad_token_id = 0
        eos_token_id = 42

        def __call__(self, text, **kwargs):
            return {"input_ids": Tokens([101, 102, 103])}

        def decode(self, tokens, **kwargs):
            assert tokens == [17, 18]
            return " answer "

    options = []

    def generate(**kwargs):
        options.append(kwargs)
        return [Tokens([101, 102, 103, 17, 18])]

    runtime = Tokenizer(), SimpleNamespace(generate=generate), SimpleNamespace(inference_mode=nullcontext)
    monkeypatch.setattr(local_llm, "_load", lambda model: runtime)
    assert local_llm.complete(SimpleNamespace(model="unused"), "prompt", max_new_tokens=5) == (
        "answer", {"input_tokens": 3, "output_tokens": 2},
    )
    assert options[0]["pad_token_id"] == 0
