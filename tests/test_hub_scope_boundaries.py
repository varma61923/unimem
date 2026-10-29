"""Authorization treats malformed scope data as a denial, never as iterable grants."""

import pytest

from hub import scopes


@pytest.mark.parametrize("granted", [{"admin": False}, {"read": True}, "admin", 42, False, [["admin"]], [None]])
def test_malformed_grants_fail_closed_without_exceptions(granted):
    assert not scopes.satisfies(granted, scopes.SCOPE_ADMIN)
    assert not scopes.satisfies(granted, scopes.SCOPE_READ)
    assert scopes.describe(granted) == "invalid scope data"


@pytest.mark.parametrize("requested", [{"admin": False}, 42, False, ["read", None], ["read", ["admin"]]])
def test_issuance_rejects_nontext_or_nonsequence_scope_data(requested):
    with pytest.raises(scopes.ScopeError):
        scopes.parse(requested)


def test_valid_and_legacy_scope_contracts_are_preserved():
    assert scopes.parse("write,READ,write") == ("read", "write")
    assert scopes.parse(["admin", "read"]) == ("read", "admin")
    assert scopes.satisfies(["read", "admin"], "admin")
    assert scopes.describe(["admin", "read"]) == "read,admin"
    assert scopes.satisfies(None, "admin")
    assert not scopes.satisfies(None, "scim")
    assert not scopes.satisfies([], "read")
    assert scopes.describe([]) == "none"


def test_authentication_normalization_does_not_turn_mapping_keys_into_grants():
    pytest.importorskip("sqlalchemy")
    from hub import auth

    assert auth._scopes_of({"admin": False}) == ()
    assert auth._scopes_of([["admin"]]) == ()
    assert auth._scopes_of(42) == ()
    assert auth._scopes_of(["read", "admin"]) == ("read", "admin")
    assert auth._scopes_of(None) is None
