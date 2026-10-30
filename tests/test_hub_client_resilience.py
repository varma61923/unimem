import pytest

from unimem import hub_client


class TestRetryPolicy:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("read timeout"),
            ConnectionRefusedError("connection refused"),
            ConnectionResetError("connection reset by peer"),
            RuntimeError("server returned 503 Service Unavailable"),
            RuntimeError("upstream returned 502"),
        ],
    )
    def test_transport_failures_are_retried(self, exc):
        assert hub_client._is_retryable(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("401 Unauthorized"),
            RuntimeError("invalid, revoked, or expired API key"),
            RuntimeError("403 Forbidden"),
        ],
    )
    def test_auth_failures_are_not_retried(self, exc):
        assert hub_client._is_retryable(exc) is False

    def test_unknown_failures_are_not_retried(self):
        assert hub_client._is_retryable(ValueError("something we don't recognize")) is False


class TestDefaults:
    def test_a_finite_timeout_is_configured(self):
        assert hub_client.DEFAULT_TIMEOUT_SECONDS > 0
        assert hub_client.DEFAULT_TIMEOUT_SECONDS < 300

    def test_retries_are_bounded(self):
        assert 1 <= hub_client.DEFAULT_MAX_ATTEMPTS <= 5

    def test_backoff_is_positive(self):
        assert hub_client.RETRY_BASE_DELAY_SECONDS > 0


class _Result:
    is_error = False
    content: list = []

    def __init__(self, payload):
        self.structured_content = payload


def _scripted_hub(monkeypatch, failures, payload):
    """Patch the Hub transport: raise each of *failures* in turn, then answer *payload*."""
    import asyncio
    import contextlib

    calls: list[dict] = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(dict(arguments))
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
            return _Result(payload)

    @contextlib.asynccontextmanager
    async def fake_open(hub_url, api_key, timeout_seconds=30, max_attempts=3, on_first_pause=None):
        session = hub_client.HubSession(hub_url, api_key, timeout_seconds, max_attempts)
        session._session = Session()
        yield session

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(hub_client, "open_hub_session", fake_open)
    monkeypatch.setattr(hub_client.asyncio, "sleep", no_sleep)
    return calls, asyncio.run


class TestWriteReplaySafety:
    def test_kb_submission_retries_carry_one_stable_idempotency_key(self, monkeypatch):
        # Regression: a read timeout after a committed insert was replayed
        # without a key, creating a duplicate KB submission.
        calls, run = _scripted_hub(monkeypatch, [TimeoutError("read timeout")], {"id": "kb-1"})
        result = run(hub_client.submit_kb_entry("https://hub.example", "k", "t", "c", "s"))
        assert result["id"] == "kb-1"
        assert len(calls) == 2
        keys = {call["idempotency_key"] for call in calls}
        assert len(keys) == 1 and next(iter(keys)).startswith("kb:")

    def test_kb_submission_keeps_a_caller_supplied_key(self, monkeypatch):
        calls, run = _scripted_hub(monkeypatch, [], {"id": "kb-1"})
        run(hub_client.submit_kb_entry("https://hub.example", "k", "t", "c", "s", idempotency_key="mine"))
        assert calls == [{**calls[0], "idempotency_key": "mine"}]

    def test_delete_is_not_replayed_after_an_ambiguous_timeout(self, monkeypatch):
        # Regression: the replay of an applied delete answered not_found and the
        # CLI reported "no such trace" for a trace it had just deleted.
        calls, run = _scripted_hub(monkeypatch, [TimeoutError("read timeout")], {"error": "not_found"})
        with pytest.raises(hub_client.HubConnectionError):
            run(hub_client.delete_trace("https://hub.example", "k", "tr-1"))
        assert len(calls) == 1

    def test_delete_is_replayed_when_the_request_never_left(self, monkeypatch):
        calls, run = _scripted_hub(monkeypatch, [ConnectionRefusedError("refused")], {"deleted": True})
        assert run(hub_client.delete_trace("https://hub.example", "k", "tr-1")) is True
        assert len(calls) == 2

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ConnectionRefusedError("refused"), True),
            (__import__("socket").gaierror("dns"), True),
            (type("ConnectTimeout", (Exception,), {})("connect"), True),
            (TimeoutError("read timeout"), False),
            (ConnectionResetError("reset"), False),
        ],
    )
    def test_never_sent_classification(self, exc, expected):
        wrapped = RuntimeError("transport failed")
        wrapped.__cause__ = exc
        assert hub_client._never_sent(wrapped) is expected
