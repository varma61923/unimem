from unimem.litellm_wrapper import UniMemLiteLLM, wrap_completion


def test_augment_messages_prepends_system():
    wrapper = UniMemLiteLLM()
    messages = [{"role": "user", "content": "How do I deploy?"}]
    augmented = wrapper._augment_messages(messages, "User prefers Docker over Kubernetes.")

    assert len(augmented) == 2
    assert augmented[0]["role"] == "system"
    assert "User prefers Docker over Kubernetes." in augmented[0]["content"]
    assert augmented[1]["content"] == "How do I deploy?"


def test_augment_messages_extends_existing_system():
    wrapper = UniMemLiteLLM()
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ]
    augmented = wrapper._augment_messages(messages, "User timezone is UTC+2.")

    assert len(augmented) == 2
    assert augmented[0]["role"] == "system"
    assert "You are a helpful assistant." in augmented[0]["content"]
    assert "User timezone is UTC+2." in augmented[0]["content"]


def test_completion_with_custom_fn(tmp_path):
    root = str(tmp_path / "store")
    wrapper = UniMemLiteLLM(root=root, auto_extract=False)

    called_with = {}

    def mock_completion(model, messages, **kwargs):
        called_with["model"] = model
        called_with["messages"] = messages
        return {"choices": [{"message": {"role": "assistant", "content": "I am ready."}}]}

    resp = wrapper.completion(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Test prompt"}],
        completion_fn=mock_completion,
    )
    assert resp["choices"][0]["message"]["content"] == "I am ready."
    assert called_with["model"] == "gpt-4o"
    assert len(called_with["messages"]) >= 1


def test_wrap_completion_factory():
    fn = wrap_completion()

    def mock_completion(model, messages, **kwargs):
        return {"result": "ok", "model": model}

    # Pass mock completion_fn via kwargs
    res = fn("claude-sonnet-5", [{"role": "user", "content": "hi"}], completion_fn=mock_completion)
    assert res["result"] == "ok"
    assert res["model"] == "claude-sonnet-5"


def test_real_store_recall_and_capture_keep_roles_and_space_boundaries(tmp_path):
    from unimem.conversation import Store

    root = str(tmp_path)
    with Store(root, "alice") as store:
        store.add("previous", [{"role": "user", "text": "The launch codename is Kestrel."}])
    with Store(root, "bob") as store:
        store.add("previous", [{"role": "user", "text": "The launch codename is PRIVATE_ORCHID."}])
    received = []

    def complete(**kwargs):
        received.extend(kwargs["messages"])
        return {"choices": [{"message": {"content": "Kestrel launch is ready."}}]}

    wrapper = UniMemLiteLLM(root=root, space="alice")
    original = [{"role": "user", "content": "What is the launch codename?"}]
    wrapper.completion("test-model", original, completion_fn=complete)
    assert "Kestrel" in received[0]["content"]
    assert "PRIVATE_ORCHID" not in str(received)
    assert original == [{"role": "user", "content": "What is the launch codename?"}]
    with Store(root, "alice", create=False) as store:
        rows = store.db.execute("SELECT role, text FROM turns ORDER BY id").fetchall()
        assert [(row["role"], row["text"]) for row in rows][-2:] == [
            ("user", "What is the launch codename?"),
            ("assistant", "Kestrel launch is ready."),
        ]
    with Store(root, "bob", create=False) as store:
        assert store.stats()["turns"] == 1


def test_completed_exchange_is_redacted_and_recalled_on_the_next_call(tmp_path):
    from unimem.conversation import Store

    wrapper = UniMemLiteLLM(root=str(tmp_path))
    secret = "Bearer test-private-token-1234567890"

    def complete(**kwargs):
        return {"choices": [{"message": {"content": "Recorded safely."}}]}

    wrapper.completion(
        "test-model", [{"role": "user", "content": "The launch codename is Kestrel. " + secret}], completion_fn=complete
    )
    with Store(str(tmp_path), "default", create=False) as store:
        assert store.stats()["turns"] == 2
        assert secret not in str([tuple(row) for row in store.db.execute("SELECT text FROM turns")])
    captured = []
    wrapper.completion(
        "test-model",
        [{"role": "user", "content": "What is the launch codename?"}],
        completion_fn=lambda **kw: captured.extend(kw["messages"]) or {},
    )
    assert "Kestrel" in captured[0]["content"]


def test_read_only_mode_does_not_create_an_empty_store(tmp_path):
    wrapper = UniMemLiteLLM(root=str(tmp_path), auto_extract=False)
    wrapper.completion(
        "test-model",
        [{"role": "user", "content": "hello"}],
        completion_fn=lambda **kwargs: {"choices": [{"message": {"content": "hi"}}]},
    )
    assert not (tmp_path / "memory").exists()


def test_multimodal_query_ignores_nontext_parts():
    wrapper = UniMemLiteLLM()
    assert (
        wrapper._extract_last_user_message(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "test"}},
                        {"type": "text", "text": "launch codename"},
                        {"type": "text", "text": None},
                    ],
                }
            ]
        )
        == "launch codename"
    )


def test_system_content_parts_and_original_messages_are_preserved():
    wrapper = UniMemLiteLLM()
    parts = [{"type": "text", "text": "Be helpful."}]
    messages = [{"role": "system", "content": parts}, {"role": "user", "content": "launch"}]
    augmented = wrapper._augment_messages(messages, "The launch codename is Kestrel.")
    assert augmented[0]["content"][:-1] == parts
    assert "Kestrel" in augmented[0]["content"][-1]["text"]
    assert messages[0]["content"] == [{"type": "text", "text": "Be helpful."}]


def test_capture_failure_is_visible_without_logging_exchange_or_exception_secrets(tmp_path, monkeypatch, caplog):
    from unimem.conversation import Store

    secret = "test-private-exception-content"

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(Store, "add", fail)
    response = {"choices": [{"message": {"content": "A normal answer."}}]}
    result = UniMemLiteLLM(root=str(tmp_path)).completion(
        "test-model", [{"role": "user", "content": "launch"}], completion_fn=lambda **kw: response
    )
    assert result is response
    assert "Auto-extraction failed (RuntimeError)" in caplog.text
    assert secret not in caplog.text


def test_sdk_shaped_response_is_captured_in_the_configured_session(tmp_path):
    from types import SimpleNamespace

    from unimem.conversation import Store

    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Kestrel is ready."))])
    wrapped = wrap_completion(root=str(tmp_path), space="alice", session="run-42")
    assert (
        wrapped("test-model", [{"role": "user", "content": "launch"}], completion_fn=lambda **kw: response) is response
    )
    with Store(str(tmp_path), "alice", create=False) as store:
        assert [row[0] for row in store.db.execute("SELECT id FROM sessions")] == ["run-42"]
        assert store.stats()["turns"] == 2
