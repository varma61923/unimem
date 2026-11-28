"""Offline Ed25519 enterprise license verification for self-hosted and SaaS Hub servers."""

from __future__ import annotations

from typing import Any

from unimem import capabilities


def enforce_hub_license(config: Any) -> dict[str, Any] | None:
    """Validate optional/required commercial licensing for a running UniMem Hub instance."""
    features: list[str] = ["hub"]
    if getattr(config, "sso_enabled", False) or getattr(config, "oidc_issuer", ""):
        features.append("sso")
    if getattr(config, "connectors_enabled", False):
        features.append("connectors")
    return capabilities.enforce_commercial_license(
        env_prefix="HUB",
        deployment_mode="onprem",
        required_features=features,
    )
