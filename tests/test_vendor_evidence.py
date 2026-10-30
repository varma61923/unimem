from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unimem import frontier_protocol as fp
from unimem import holdout_io, lineage, memory_adapters

FIXTURES = {
    "mem0": {"results": [{"id": "m1", "memory": "Verify the object class"}]},
    "zep": {"edges": [{"uuid": "e1", "fact": "Verify the object class"}]},
    "graphiti": {"edges": [{"uuid_": "g1", "fact": "Verify the object class"}]},
    "letta": {"passages": [{"id": "p1", "text": "Verify the object class"}]},
    "agentcore": {"memoryRecordSummaries": [{"memoryRecordId": "r1", "content": {"text": "Verify the object class"}}]},
    "vertex": {"memories": [{"name": "projects/p/memories/m1", "fact": "Verify the object class"}]},
    "foundry": {"items": [{"id": "f1", "text": "Verify the object class"}]},
    "anthropic": {"items": [{"key": "a1", "content": "Verify the object class"}]},
}


class Response:
    def __init__(self, data):
        self.data, self.content = data, json.dumps(data).encode()

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class Client:
    def __init__(self, data):
        self.data, self.requests = data, []

    def post(self, endpoint, **kwargs):
        self.requests.append(kwargs)
        return Response(self.data)


@pytest.mark.parametrize("provider", sorted(FIXTURES))
def test_pinned_vendor_fixtures_stable_ids_and_safe_provenance(provider, tmp_path):
    client = Client(FIXTURES[provider])
    adapter = memory_adapters.VendorReadAdapter(
        provider,
        endpoint="https://memory.example/retrieve",
        scope={"tenant_id": "acme"},
        client=client,
        headers={"Authorization": "secret-token"},
    )
    first, second = adapter.search("object"), adapter.search("object")
    assert first == second and first[0].text == "Verify the object class"
    assert "secret-token" not in str(first)
    with pytest.raises(ValueError, match="scopes are pinned"):
        adapter.search("object", tenant_id="other")
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="foreign")
    measured = memory_adapters.MeasuredMemory(adapter, root=root, check_every=1000)
    for i in range(30):
        recalled = measured.recall("object", occasion_id=str(i))
        measured.record_outcome(str(i), succeeded=bool(recalled))
    assert lineage.labels(root, first[0].id)["integrity"] == "external"
    detailed = measured.recall_detailed("object", occasion_id="metadata")
    if detailed.items:
        assert not detailed.items[0].raw["ifc"]["procedure"]
    assert provider in memory_adapters.comparative_report(root)["stores"]
    assert "secret-token" not in Path(fp.filename(root, "lineage.jsonl")).read_text()


def test_credential_bearing_transport_errors_are_sanitized():
    class BadClient:
        def post(self, *args, **kwargs):
            raise RuntimeError("Authorization: super-secret")

    adapter = memory_adapters.VendorReadAdapter(
        "vertex", endpoint="https://memory.example/retrieve", scope={"tenant": "acme"}, client=BadClient()
    )
    with pytest.raises(ValueError) as exc:
        adapter.search("object")
    assert "super-secret" not in str(exc.value)


def test_one_bad_vendor_item_is_skipped_not_fatal(tmp_path):
    data = {
        "results": [
            {"id": "huge", "memory": "x" * 70_000},
            {"id": "empty", "memory": ""},
            {"id": "nested", "memory": {"unexpected": 1}},
            {"id": "ok", "memory": "Verify the class"},
        ]
    }
    adapter = memory_adapters.VendorReadAdapter(
        "mem0", endpoint="https://memory.example/retrieve", scope={"tenant_id": "acme"}, client=Client(data)
    )
    assert [item.text for item in adapter.search("class")] == ["Verify the class"]
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="foreign")
    measured = memory_adapters.MeasuredMemory(adapter, root=root, check_every=1000)
    assert measured.recall_detailed("class", occasion_id="one").items is not None


def _httpx_adapter(handler):
    httpx = pytest.importorskip("httpx")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return memory_adapters.VendorReadAdapter(
        "mem0", endpoint="https://memory.example/retrieve", scope={"tenant_id": "acme"}, client=client
    )


def test_vendor_trickling_response_has_a_total_deadline(monkeypatch):
    httpx = pytest.importorskip("httpx")
    elapsed, pulled, closed = [0.0], [], []
    monkeypatch.setattr(memory_adapters, "time", SimpleNamespace(monotonic=lambda: elapsed[0]), raising=False)

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            # Each chunk arrives within the 10s socket timeout, but the body
            # takes much longer. The payload stays under the byte ceiling.
            for chunk in [b" "] * 10 + [json.dumps(FIXTURES["mem0"]).encode()]:
                elapsed[0] += 6.0
                pulled.append(1)
                yield chunk

        def close(self):
            closed.append(True)

    adapter = _httpx_adapter(lambda request: httpx.Response(200, stream=Trickle()))
    with pytest.raises(ValueError, match="memory read failed"):
        adapter.search("object")
    assert len(pulled) == 2
    assert closed == [True]


def test_httpx_vendor_reads_stream_and_stop_at_the_size_limit():
    # Regression: the whole body was buffered before the 1 MiB check, so a
    # hostile endpoint could stream hundreds of MiB into memory.
    httpx = pytest.importorskip("httpx")
    pulled = []

    def endless():
        for _ in range(200):
            pulled.append(1)
            yield b" " * 65536

    adapter = _httpx_adapter(lambda request: httpx.Response(200, content=endless()))
    with pytest.raises(ValueError, match="read failed"):
        adapter.search("object")
    assert len(pulled) * 65536 <= fp.MAX_BYTES + 2 * 65536


def test_httpx_vendor_reads_refuse_an_oversized_declared_length():
    httpx = pytest.importorskip("httpx")
    big = b"{}" + b" " * (fp.MAX_BYTES + 10)
    adapter = _httpx_adapter(lambda request: httpx.Response(200, content=big))
    with pytest.raises(ValueError, match="read failed"):
        adapter.search("object")


def test_httpx_vendor_reads_decode_a_normal_response():
    httpx = pytest.importorskip("httpx")
    adapter = _httpx_adapter(lambda request: httpx.Response(200, json=FIXTURES["mem0"]))
    assert [item.text for item in adapter.search("object")] == ["Verify the object class"]


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "gzip, identity"])
def test_vendor_reads_reject_compression_before_decoding(encoding):
    httpx = pytest.importorskip("httpx")
    pulled, closed = [], []

    class Compressed(httpx.SyncByteStream):
        def __iter__(self):
            # A decoder must never see these bytes: its output allocation
            # could exceed the byte ceiling before iter_bytes yields a chunk.
            pulled.append(True)
            yield b"compressed payload"

        def close(self):
            closed.append(True)

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=Compressed())

    adapter = _httpx_adapter(handler)
    with pytest.raises(ValueError, match="memory read failed"):
        adapter.search("object")
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert pulled == []
    assert closed == [True]
