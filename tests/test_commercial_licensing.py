"""Tests for offline Ed25519 signed enterprise license verification and Gateway/Hub gating."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from hub import licensing
from unimem import capabilities, gateway


def _keypair() -> tuple[bytes, bytes]:
    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes, pub_bytes


def test_offline_ed25519_license_roundtrip_and_tamper_rejection():
    priv, pub = _keypair()
    now = datetime.now(timezone.utc)
    token = capabilities.issue_commercial_license(
        private_key=priv,
        org_id="acme-robotics",
        plan="enterprise",
        deployment_modes=["onprem", "robot-fleet"],
        max_agents=500,
        features=["learning", "robotics", "hub"],
        issued_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(days=30)).isoformat(),
        license_id="lic-001",
    )

    claims = capabilities.verify_commercial_license(
        token,
        public_key=pub,
        deployment_mode="robot-fleet",
        required_features=["learning", "robotics"],
    )
    assert claims["org_id"] == "acme-robotics"
    assert claims["max_agents"] == 500

    # Wrong public key fails closed.
    _, other_pub = _keypair()
    with pytest.raises(PermissionError, match="signature rejected"):
        capabilities.verify_commercial_license(token, public_key=other_pub)

    # Disallowed deployment mode and missing feature fail closed.
    with pytest.raises(PermissionError, match="does not permit mode"):
        capabilities.verify_commercial_license(token, public_key=pub, deployment_mode="saas")
    with pytest.raises(PermissionError, match="missing required features"):
        capabilities.verify_commercial_license(token, public_key=pub, required_features=["zk"])

    # Expired token fails closed.
    expired = capabilities.issue_commercial_license(
        private_key=priv,
        org_id="acme",
        plan="team",
        deployment_modes=["gateway"],
        max_agents=25,
        features=["learning"],
        issued_at=(now - timedelta(days=10)).isoformat(),
        expires_at=(now - timedelta(days=1)).isoformat(),
        license_id="lic-old",
    )
    with pytest.raises(PermissionError, match="expired"):
        capabilities.verify_commercial_license(expired, public_key=pub)


def test_gateway_and_hub_enforce_commercial_license_when_enabled(tmp_path, monkeypatch):
    priv, pub = _keypair()
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("UNIMEM_COMMERCIAL_LICENSE_REQUIRED", "1")
    monkeypatch.setenv("UNIMEM_LICENSE_PUBLIC_KEY", pub.hex())
    monkeypatch.delenv("UNIMEM_LICENSE_KEY", raising=False)
    monkeypatch.delenv("UNIMEM_LICENSE_FILE", raising=False)

    with pytest.raises(PermissionError, match="UNIMEM_LICENSE_KEY"):
        gateway.Gateway(str(tmp_path))

    token = capabilities.issue_commercial_license(
        private_key=priv,
        org_id="fleet-ops",
        plan="sovereign",
        deployment_modes=["gateway", "sovereign-airgapped"],
        max_agents=100,
        features=["learning"],
        issued_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(days=7)).isoformat(),
        license_id="lic-gw",
    )
    license_file = tmp_path / "license.key"
    license_file.write_text(token, encoding="utf-8")
    monkeypatch.setenv("UNIMEM_LICENSE_FILE", str(license_file))
    gw = gateway.Gateway(str(tmp_path))
    assert gw.commercial_license is not None
    assert gw.commercial_license["org_id"] == "fleet-ops"

    # Hub license gate fails closed when required without HUB_LICENSE_KEY and passes with matching token.
    from types import SimpleNamespace

    monkeypatch.setenv("HUB_COMMERCIAL_LICENSE_REQUIRED", "1")
    monkeypatch.setenv("HUB_LICENSE_PUBLIC_KEY", pub.hex())
    cfg = SimpleNamespace(sso_enabled=False, oidc_issuer="", connectors_enabled=False)
    with pytest.raises(PermissionError, match="HUB_LICENSE_KEY"):
        licensing.enforce_hub_license(cfg)

    hub_token = capabilities.issue_commercial_license(
        private_key=priv,
        org_id="hub-ent",
        plan="enterprise",
        deployment_modes=["onprem"],
        max_agents=-1,
        features=["hub", "sso"],
        issued_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(days=7)).isoformat(),
        license_id="lic-hub",
    )
    monkeypatch.setenv("HUB_LICENSE_KEY", hub_token)
    hub_claims = licensing.enforce_hub_license(SimpleNamespace(sso_enabled=True, oidc_issuer="", connectors_enabled=False))
    assert hub_claims is not None and hub_claims["org_id"] == "hub-ent"
