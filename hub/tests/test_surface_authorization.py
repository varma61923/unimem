"""Every Hub surface must apply the same role and delegation gates as MCP.

DB-free: storage is faked, so these run without HUB_TEST_DATABASE_URL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from starlette.applications import Starlette

from hub import auth, crud, delegation, otlp, rbac, rest
from hub.abuse import make_rate_limiter
from hub.auth import AuthenticatedKey, AuthenticatedUser
from hub.config import HubConfig

pytestmark = pytest.mark.asyncio

_CONFIG = HubConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/unimem",
    oidc_issuer="https://login.example.test/",
    oidc_audience="https://hub.example.test/mcp",
    oidc_jwks='{"keys": []}',
)
_TRACE = {"title": "t", "context_text": "c", "solution_text": "s"}


@asynccontextmanager
async def _no_db(*_args, **_kwargs):
    yield object()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def rest_as(monkeypatch):
    """Build the REST app with an OIDC person of the given role signed in."""
    written: list[str] = []

    async def fake_contribute(session, org_id, *args, **kwargs):
        written.append(org_id)
        return {"id": "t1", "quarantined": False}

    async def admit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(rest, "session_scope", _no_db)
    monkeypatch.setattr(crud, "contribute_trace", fake_contribute)
    monkeypatch.setattr(delegation, "admit_request", admit)

    def build(role: str):
        async def verify(_session, _token, _provider, *, jwks_cache=None):
            return AuthenticatedUser(id="u", org_id="org-1", role=role, email="a@example.test")

        monkeypatch.setattr(auth, "verify_user_token", verify)
        app = Starlette()
        rest.add_rest_routes(app, object(), config=_CONFIG, rate_limiter=make_rate_limiter(_CONFIG))
        return app, written

    return build


@pytest.mark.parametrize("role", [rbac.ROLE_ANALYST, rbac.ROLE_DEPLOYER])
async def test_a_role_without_curate_cannot_contribute_over_rest(rest_as, role):
    # Regression: MCP refused contribute_trace for these roles (no `curate`),
    # but REST only checked the `write` scope and returned 201.
    app, written = rest_as(role)
    async with _client(app) as client:
        response = await client.post("/api/v1/traces", headers={"Authorization": "Bearer h.p.s"}, json=_TRACE)
    assert response.status_code == 403, response.text
    assert "curate" in response.json()["detail"]
    assert written == []


@pytest.mark.parametrize("role", [rbac.ROLE_CURATOR, rbac.ROLE_OWNER])
async def test_a_role_with_curate_still_contributes_over_rest(rest_as, role):
    app, written = rest_as(role)
    async with _client(app) as client:
        response = await client.post("/api/v1/traces", headers={"Authorization": "Bearer h.p.s"}, json=_TRACE)
    assert response.status_code == 201, response.text
    assert written == ["org-1"]


@pytest.mark.parametrize(
    ("role", "operation", "allowed"),
    [
        (rbac.ROLE_CURATOR, "record-forget", False),  # erasure is `security`, as for MCP delete
        (rbac.ROLE_ANALYST, "record-put", False),
        (rbac.ROLE_SECURITY_ADMIN, "record-forget", True),
    ],
)
async def test_memory_operations_check_the_role_capability(rest_as, role, operation, allowed):
    app, _ = rest_as(role)
    async with _client(app) as client:
        response = await client.post(
            f"/v1/memory/{operation}", headers={"Authorization": "Bearer h.p.s"}, content=b"not json"
        )
    # Allowed requests get past authorization and fail on the malformed body.
    assert (response.status_code != 403) is allowed, response.text


async def test_a_plain_api_key_is_unaffected_by_role_checks(monkeypatch):
    key = AuthenticatedKey(org_id="org-1", key_prefix="unimem_live_k", scopes=("read", "write"), key_id="k")

    async def verify_key(_session, _raw):
        return key

    async def admit(*_args, **_kwargs):
        return None

    async def fake_contribute(session, org_id, *args, **kwargs):
        return {"id": "t1", "quarantined": False}

    monkeypatch.setattr(rest, "session_scope", _no_db)
    monkeypatch.setattr(auth, "verify_api_key", verify_key)
    monkeypatch.setattr(delegation, "admit_request", admit)
    monkeypatch.setattr(crud, "contribute_trace", fake_contribute)
    app = Starlette()
    rest.add_rest_routes(app, object(), config=_CONFIG, rate_limiter=make_rate_limiter(_CONFIG))
    async with _client(app) as client:
        response = await client.post("/api/v1/traces", headers={"X-API-Key": "unimem_live_x"}, json=_TRACE)
    assert response.status_code == 201, response.text


async def test_otlp_ingest_refuses_an_enrolled_delegate(monkeypatch):
    # Regression: REST/MCP refused a capability-bound key without its
    # capability, but OTLP never consulted the gate and wrote the trace.
    config = replace(_CONFIG, otlp_ingest_enabled=True)
    key = AuthenticatedKey(org_id="org-1", key_prefix="unimem_live_abcd", scopes=("read", "write"), key_id="k1")
    written: list[str] = []

    async def verify_key(_session, _raw):
        return key

    async def enrolled_delegate(request, authenticated, cfg, factory):
        raise PermissionError("UniMem enrolled delegate must supply capability and possession headers")

    async def fake_contribute(session, org_id, *args, **kwargs):
        written.append(kwargs.get("context_text", ""))
        return {"id": "t1"}

    monkeypatch.setattr(auth, "verify_api_key", verify_key)
    monkeypatch.setattr(delegation, "authorize", enrolled_delegate)
    monkeypatch.setattr(otlp, "session_scope", _no_db)
    monkeypatch.setattr(crud, "contribute_trace", fake_contribute)
    app = Starlette()
    otlp.add_otlp_routes(app, object(), config=config, rate_limiter=make_rate_limiter(config))
    span = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanId": "aa",
                                "name": "x",
                                "attributes": [
                                    {"key": "input.value", "value": {"stringValue": "unscoped ctx"}},
                                    {"key": "output.value", "value": {"stringValue": "sol"}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    async with _client(app) as client:
        response = await client.post("/v1/traces", headers={"X-API-Key": "unimem_live_x"}, json=span)
    assert response.status_code == 403, response.text
    assert written == []


async def test_requires_capability_is_false_without_a_capability_directory():
    key = AuthenticatedKey(org_id="org-1", key_prefix="unimem_live_abcd", scopes=("read",))
    assert await delegation.requires_capability(key, _CONFIG, object()) is False
    assert await delegation.requires_capability(key, None, object()) is False
