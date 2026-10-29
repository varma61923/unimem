"""What one API key is allowed to do, as distinct from which org it speaks for."""

from __future__ import annotations

from typing import TypeGuard

SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"
SCOPE_SCIM = "scim"

ALL_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN, SCOPE_SCIM)

_LEGACY_IMPLIED_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)

DEFAULT_SCOPES: tuple[str, ...] = _LEGACY_IMPLIED_SCOPES


class ScopeError(ValueError): ...


def parse(raw: object) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_SCOPES
    if not isinstance(raw, (str, list, tuple)):
        raise ScopeError("scopes must be comma-separated text or a list of scope names")
    items = raw.split(",") if isinstance(raw, str) else raw
    if not all(isinstance(item, str) for item in items):
        raise ScopeError("scope names must be strings")
    requested = {item.strip().lower() for item in items if item.strip()}
    if not requested:
        raise ScopeError(
            "no scopes given. Pass at least one of "
            f"{', '.join(ALL_SCOPES)} -- a key with no scopes can do nothing at all."
        )
    unknown = sorted(requested - set(ALL_SCOPES))
    if unknown:
        raise ScopeError(f"unknown scope(s): {', '.join(unknown)}. Valid scopes are {', '.join(ALL_SCOPES)}.")
    return tuple(scope for scope in ALL_SCOPES if scope in requested)


def _valid_grants(granted: object) -> TypeGuard[list[str] | tuple[str, ...]]:
    return isinstance(granted, (list, tuple)) and all(
        isinstance(scope, str) and scope in ALL_SCOPES for scope in granted
    )


def satisfies(granted: object, required: str) -> bool:
    if granted is None:
        return required in _LEGACY_IMPLIED_SCOPES
    return _valid_grants(granted) and required in granted


def describe(granted: object) -> str:
    """Short human-readable rendering for CLI output and audit lines."""
    if granted is None:
        return f"{','.join(_LEGACY_IMPLIED_SCOPES)} (legacy key, issued before scopes existed)"
    if not _valid_grants(granted):
        return "invalid scope data"
    if not granted:
        return "none"
    return ",".join(scope for scope in ALL_SCOPES if scope in granted)
