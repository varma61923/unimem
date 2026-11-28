"""Zero-dependency HTTP/HTTPS remote client for UniMem Gateway and Hub APIs."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from typing import Any

LEARNING_SCHEMA = "unimem.learning.v1"
LEARNING_OPERATIONS = frozenset(
    {"capabilities", "configure", "assign", "delivery", "outcome", "run", "status", "rollback"}
)
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 60.0


class UniMemClientError(RuntimeError):
    """Raised when the remote UniMem peer rejects or returns an invalid payload."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None  # Never forward bearer credentials across HTTP redirects.


def _read_bounded(response: Any, limit: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("UniMem remote response exceeded timeout")
        chunk = response.read(min(65_536, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"gateway response exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _normalize_scope(context: Mapping[str, Any] | None, agent_id: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    if context:
        for key, value in context.items():
            key_text = str(key).strip()
            value_text = str(value).strip()
            if not key_text or not value_text:
                raise ValueError("scope keys and values must be non-empty strings")
            merged[key_text] = value_text
    if agent_id:
        merged["agent"] = agent_id
    return merged


def _learning_reply(result: Any) -> dict[str, Any]:
    if isinstance(result, dict) and result.get("error"):
        error = result["error"]
        code = error.get("code", "learning_refused") if isinstance(error, dict) else str(error)
        detail = error.get("message", code) if isinstance(error, dict) else str(result.get("detail", code))
        raise UniMemClientError(f"learning operation refused ({code}): {detail}")
    if not isinstance(result, dict) or result.get("schema") != LEARNING_SCHEMA:
        raise UniMemClientError("peer does not support unimem.learning.v1")
    return result


class LearningClient:
    """Versioned remote `unimem.learning.v1` operations."""

    def __init__(self, client: RemoteMemoryClient) -> None:
        self._client = client

    def capabilities(self) -> dict[str, Any]:
        result = _learning_reply(self._client._learning_request("capabilities", {}))
        operations = result.get("operations")
        if not isinstance(operations, list) or any(not isinstance(op, str) for op in operations):
            raise UniMemClientError("peer returned malformed learning capabilities")
        return result

    def call(self, operation: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if operation not in LEARNING_OPERATIONS:
            raise ValueError(f"unknown learning operation: {operation!r}")
        if operation == "capabilities":
            return self.capabilities()
        supported = self.capabilities().get("operations", [])
        if operation not in supported:
            raise UniMemClientError(f"peer does not support learning {operation}")
        return _learning_reply(self._client._learning_request(operation, dict(payload or {})))

    def configure(self, mode: str, **options: Any) -> dict[str, Any]:
        return self.call("configure", {**options, "mode": mode})

    def assign(self, occasion_id: str, memory_ids: list[str], **options: Any) -> dict[str, Any]:
        return self.call("assign", {**options, "occasion_id": occasion_id, "memory_ids": memory_ids})

    def delivery(self, occasion_id: str, memory_ids: list[str], **options: Any) -> dict[str, Any]:
        return self.call("delivery", {**options, "occasion_id": occasion_id, "memory_ids": memory_ids})

    def outcome(self, occasion_id: str, succeeded: bool | None = None, **options: Any) -> dict[str, Any]:
        body: dict[str, Any] = {**options, "occasion_id": occasion_id}
        if succeeded is not None:
            body["succeeded"] = succeeded
        return self.call("outcome", body)

    def run(self, **options: Any) -> dict[str, Any]:
        return self.call("run", options)

    def status(self, **options: Any) -> dict[str, Any]:
        return self.call("status", options)

    def rollback(self, reason: str, **options: Any) -> dict[str, Any]:
        return self.call("rollback", {**options, "reason": reason})


class RemoteMemoryClient:
    """HTTP/HTTPS client for compiled UniMem Gateways and hosted Hub servers."""

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        agent_id: str = "",
        context: Mapping[str, Any] | None = None,
        container_tag: str = "",
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an http:// or https:// base URL")
        if any(char.isspace() for char in url) or parsed.username or parsed.password:
            raise ValueError("url must not contain credentials or whitespace")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.url = url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self.context = _normalize_scope(context, agent_id)
        self.container_tag = container_tag.strip()
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.learning = LearningClient(self)

    def _headers(self, *, content_type: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.container_tag:
            headers["X-Container-Tag"] = self.container_tag
        return headers

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=self.timeout) as response:
            raw = _read_bounded(response, self.max_response_bytes, self.timeout)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("gateway response must be a JSON object")
        return payload

    def _request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.url}/v1/memory/{operation}",
            data=json.dumps(payload, allow_nan=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        return self._open_json(request)

    def _learning_request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in LEARNING_OPERATIONS:
            raise ValueError(f"unknown learning operation: {operation!r}")
        read_only = operation in {"capabilities", "status"}
        endpoint = f"{self.url}/v1/learning/{operation}"
        body = {**payload, "context": self.context}
        if read_only:
            endpoint += "?" + urllib.parse.urlencode(body, doseq=True)
        request = urllib.request.Request(
            endpoint,
            data=None if read_only else json.dumps(body, allow_nan=False).encode("utf-8"),
            headers=self._headers(content_type=not read_only),
            method="GET" if read_only else "POST",
        )
        return self._open_json(request)

    def health_live(self) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.url}/v1/health/live", headers=self._headers(content_type=False))
        return self._open_json(request)

    def health_ready(self) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.url}/v1/health/ready", headers=self._headers(content_type=False))
        return self._open_json(request)

    def _occasion(self, occasion_id: str | None) -> str | None:
        if occasion_id is not None:
            return occasion_id
        if not self.agent_id:
            return None
        return f"occ-{uuid.uuid4().hex[:16]}"

    def add(self, text: str, *, local: bool = True, memory_type: str = "general", **extra: Any) -> dict[str, Any]:
        body = {"text": text, "local": local, "context": self.context, "memory_type": memory_type, **extra}
        return self._request("add", body)

    def profile(
        self,
        query: str = "",
        *,
        limit: int = 10,
        occasion_id: str | None = None,
        action_class: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "profile",
            {
                "query": query,
                "limit": limit,
                "context": self.context,
                "occasion_id": self._occasion(occasion_id),
                "action_class": action_class,
            },
        )

    def search(
        self,
        query: str,
        *,
        recipe: str = "balanced",
        retriever: str = "hybrid",
        limit: int = 10,
        center: str = "",
        as_of: str | None = None,
        action_class: str = "",
        known_at: str | None = None,
        graph_hops: int = 2,
        reranker: str | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "query": query,
            "recipe": recipe,
            "retriever": retriever,
            "limit": limit,
            "center": center,
            "as_of": as_of,
            "context": self.context,
            "action_class": action_class,
        }
        if known_at is not None:
            body["known_at"] = known_at
        if graph_hops != 2:
            body["graph_hops"] = graph_hops
        if reranker is not None:
            body["reranker"] = reranker
        return self._request("search", body)["results"]

    def reflect(
        self,
        query: str,
        *,
        budget: int = 600,
        occasion_id: str | None = None,
        exploration_slots: int = 0,
        action_class: str = "",
        adaptive_budget: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query,
            "budget": budget,
            "context": self.context,
            "occasion_id": self._occasion(occasion_id),
            "exploration_slots": exploration_slots,
            "action_class": action_class,
        }
        if adaptive_budget:
            body["adaptive_budget"] = True
        return self._request("reflect", body)

    def turn(
        self,
        session_id: str,
        query: str,
        *,
        text: str = "",
        budget: int = 600,
        event: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": session_id,
            "query": query,
            "text": text,
            "budget": budget,
            "context": self.context,
        }
        if event:
            body.update({"event": event, "payload": payload or {}})
        return self._request("turn", body)

    def propose(self, text: str, *, sources: list[str]) -> dict[str, Any]:
        return self._request("propose", {"text": text, "sources": sources, "context": self.context})

    def put_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        data = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        return self._request("record-put", {"record": data, "context": self.context})

    def search_records(self, query: str = "", *, limit: int = 20) -> list[dict[str, Any]]:
        return self._request("record-search", {"query": query, "limit": limit, "context": self.context})["results"]

    def forget_record(self, identity: str, *, reason: str) -> dict[str, Any]:
        return self._request("record-forget", {"id": identity, "reason": reason, "context": self.context})

    def reap_due_records(self, *, limit: int = 16) -> list[dict[str, Any]]:
        return self._request("record-reap-due", {"limit": limit, "context": self.context})["results"]

    def team_read(self, identities: list[str], *, team_space: str = "default") -> dict[str, Any]:
        return self._request("team-read", {"identities": identities, "team_space": team_space, "context": self.context})

    def batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("batch", {"items": items, "context": self.context})


MemoryClient = RemoteMemoryClient
