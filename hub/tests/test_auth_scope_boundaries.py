"""Stored scope arrays are validated on both authentication paths and in cache."""

import pytest
from sqlalchemy import update

from hub import auth
from hub.db import session_scope
from hub.models import ApiKey, Organization

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_auth_cache():
    auth.configure_auth_cache(30)
    try:
        yield
    finally:
        auth.configure_auth_cache(0)


@pytest.mark.parametrize("stored_scopes", [["admin", None], ["admin", "unknown-scope"]])
@pytest.mark.parametrize("legacy", [False, True])
async def test_invalid_stored_scope_arrays_never_grant_authority(session_factory, stored_scopes, legacy):
    async with session_scope(session_factory) as session:
        org = Organization(name="scope-validation")
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id, scopes=["admin"])
        changes = {"scopes": stored_scopes}
        if legacy:
            changes["key_hmac"] = None
        await session.execute(update(ApiKey).where(ApiKey.id == issued.key_id).values(**changes))

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is not None
    assert resolved.scopes == ()
    cached = auth.cached_key(issued.raw_key)
    assert cached is not None
    assert cached.key_id == issued.key_id
    assert cached.scopes == ()
    token = auth.current_scopes.set(cached.scopes)
    try:
        for scope in ("read", "write", "admin"):
            with pytest.raises(auth.ScopeDenied):
                auth.require_scope(scope)
    finally:
        auth.current_scopes.reset(token)
