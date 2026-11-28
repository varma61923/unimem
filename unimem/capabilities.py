"""UniMem bounded attenuating capabilities and request proofs of possession.

This is a UniMem encoding inspired by draft-niyikiza-oauth-attenuating-agent-
tokens-01, not JWT/AAT wire compatibility. Authentication pins keys externally.
Local links require both the previous MAC and the parent's private holder key:
portable prefix MACs alone cannot authorize an alternate delegation branch.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any

from unimem import frontier_protocol as fp

OPERATIONS = frozenset({"read", "write", "forget", "export"})
ALGORITHMS = frozenset({"hmac-sha256", "ed25519"})
SET_FIELDS = ("namespaces", "operations", "classes", "purposes", "labels", "delegates")
FIELDS = frozenset((*SET_FIELDS, "ceiling", "audience", "expires_at"))
CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("unimem_capability", default=None)
STICKY: ContextVar[tuple[str, ...]] = ContextVar("unimem_handoff_labels", default=())


def _label(value: Any) -> str:
    label = fp.text(value, "capability label", 256)
    if label != label.strip() or any(ord(char) < 32 or ord(char) == 127 for char in label) or "*" in label:
        raise ValueError("UniMem capability labels must be exact printable names")
    return label


def moment(value: str) -> datetime:
    return datetime.fromisoformat(fp.moment(value))


def policy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != FIELDS:
        raise ValueError("UniMem capability caveats have unknown or missing fields")
    result: dict[str, Any] = {}
    for field in SET_FIELDS:
        items = value[field]
        if not isinstance(items, list) or len(items) > 128 or (not items and field not in {"labels", "delegates"}):
            raise ValueError("UniMem capability caveats require bounded explicit lists")
        cleaned = sorted({_label(item) for item in items})
        if len(cleaned) != len(items):
            raise ValueError("UniMem duplicate capability caveat")
        result[field] = cleaned
    if not set(result["operations"]) <= OPERATIONS:
        raise ValueError("UniMem unsupported delegated operation")
    result["ceiling"] = fp.count(value["ceiling"], 0, 3, "integrity ceiling")
    result["audience"] = _label(value["audience"])
    result["expires_at"] = moment(value["expires_at"]).isoformat()
    return result


def subset(parent: Mapping[str, Any], child: Mapping[str, Any]) -> None:
    before, after = policy(parent), policy(child)
    if any(not set(after[field]) <= set(before[field]) for field in SET_FIELDS):
        raise PermissionError("UniMem delegated caveats cannot widen")
    if after["ceiling"] > before["ceiling"] or moment(after["expires_at"]) > moment(before["expires_at"]):
        raise PermissionError("UniMem delegated ceiling or expiry cannot widen")
    if after["audience"] != before["audience"] and after["audience"] not in before["delegates"]:
        raise PermissionError("UniMem audience is not an authorized delegate")


def _bytes(domain: str, record: Mapping[str, Any]) -> bytes:
    return domain.encode() + b"\x00" + fp.wire(dict(record))


def _sign(algorithm: str, key: bytes, data: bytes) -> str:
    if algorithm not in ALGORITHMS or not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("UniMem capability signing requires a pinned algorithm and 32-byte key")
    if algorithm == "hmac-sha256":
        return hmac.new(key, data, hashlib.sha256).hexdigest()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(key).sign(data).hex()


def _verify(algorithm: str, key: bytes, data: bytes, signature: str) -> None:
    if algorithm not in ALGORITHMS or not isinstance(key, bytes) or len(key) != 32 or not isinstance(signature, str):
        raise PermissionError("UniMem invalid pinned verification key or algorithm")
    if algorithm == "hmac-sha256":
        if len(signature) != 64 or not hmac.compare_digest(_sign(algorithm, key, data), signature):
            raise PermissionError("UniMem capability signature rejected")
        return
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(key).verify(bytes.fromhex(signature), data)
    except (InvalidSignature, ValueError):
        raise PermissionError("UniMem capability signature rejected") from None


def _token(token: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(token, Mapping) or set(token) != {"version", "algorithm", "links"}:
        raise ValueError("UniMem invalid capability envelope")
    if type(token["version"]) is not int or token["version"] != 1 or token["algorithm"] not in ALGORITHMS:
        raise ValueError("UniMem unsupported capability version or algorithm")
    links = token["links"]
    if not isinstance(links, list) or not 1 <= len(links) <= 16 or len(fp.wire(dict(token))) > 65536:
        raise ValueError("UniMem capability exceeds its chain bounds")
    for node in links:
        if not isinstance(node, dict) or set(node) != {"record", "signature", "authorization"}:
            raise ValueError("UniMem invalid capability chain node")
        if not isinstance(node["record"], dict):
            raise ValueError("UniMem invalid capability node record")
    return links


def _prefix(token: Mapping[str, Any], count: int) -> dict[str, Any]:
    return {"version": 1, "algorithm": token["algorithm"], "links": token["links"][:count]}


def grant(
    caveats: Mapping[str, Any], *, issuer: str, tenant: str, nonce: str, now: str, algorithm: str, key: bytes
) -> dict[str, Any]:
    caveats = policy(caveats)
    issued = moment(now)
    if not issued < moment(caveats["expires_at"]) <= issued + timedelta(days=7):
        raise ValueError("UniMem root capability lifetime must be positive and at most seven days")
    record = {
        "type": "unimem.capability.root.v1",
        "issuer": _label(issuer),
        "tenant": _label(tenant),
        "nonce": _label(nonce),
        "issued_at": issued.isoformat(),
        "caveats": caveats,
    }
    return {
        "version": 1,
        "algorithm": algorithm,
        "links": [
            {
                "record": record,
                "signature": _sign(algorithm, key, _bytes("unimem.capability.root.v1", record)),
                "authorization": "",
            }
        ],
    }


def attenuate(token: Mapping[str, Any], caveats: Mapping[str, Any], *, key: bytes) -> dict[str, Any]:
    links = _token(token)
    if len(links) >= 16:
        raise ValueError("UniMem capability chain depth exhausted")
    caveats = policy(caveats)
    subset(links[-1]["record"]["caveats"], caveats)
    record = {"type": "unimem.capability.child.v1", "parent": fp.digest(dict(token)), "caveats": caveats}
    data = _bytes("unimem.capability.child.v1", record)
    if token["algorithm"] == "hmac-sha256":
        signature = _sign("hmac-sha256", bytes.fromhex(links[-1]["signature"]), data)
        authorization = _sign(
            "hmac-sha256", key, _bytes("unimem.capability.parent-holder.v1", {"record": record, "signature": signature})
        )
    else:
        signature, authorization = _sign("ed25519", key, data), ""
    result = {
        **dict(token),
        "links": [*links, {"record": record, "signature": signature, "authorization": authorization}],
    }
    _token(result)
    return result


def verify(
    token: Mapping[str, Any],
    *,
    issuer: str,
    tenant: str,
    algorithm: str,
    key: bytes,
    holder_key: Callable[[str], bytes],
    now: str,
    revoked: Sequence[str] = (),
) -> dict[str, Any]:
    links = _token(token)
    if token["algorithm"] != algorithm:
        raise PermissionError("UniMem capability algorithm differs from issuer pin")
    root = links[0]["record"]
    if (
        set(root) != {"type", "issuer", "tenant", "nonce", "issued_at", "caveats"}
        or root.get("type") != "unimem.capability.root.v1"
        or root["issuer"] != issuer
        or root["tenant"] != tenant
    ):
        raise PermissionError("UniMem capability issuer or tenant binding rejected")
    issued, stamp = moment(root["issued_at"]), moment(now)
    root_policy = policy(root["caveats"])
    if issued > stamp or not issued < moment(root_policy["expires_at"]) <= issued + timedelta(days=7):
        raise PermissionError("UniMem capability issuance or lifetime rejected")
    _label(root["nonce"])
    if links[0]["authorization"] != "":
        raise ValueError("UniMem root has no parent authorization")
    _verify(algorithm, key, _bytes("unimem.capability.root.v1", root), links[0]["signature"])
    chain_ids = []
    for index, node in enumerate(links):
        record = node["record"]
        current_policy = policy(record["caveats"])
        if moment(current_policy["expires_at"]) <= stamp:
            raise PermissionError("UniMem capability expired")
        if index:
            if set(record) != {"type", "parent", "caveats"} or record.get("type") != "unimem.capability.child.v1":
                raise ValueError("UniMem invalid child record")
            if record["parent"] != fp.digest(_prefix(token, index)):
                raise PermissionError("UniMem capability parent binding rejected")
            parent = policy(links[index - 1]["record"]["caveats"])
            subset(parent, current_policy)
            data = _bytes("unimem.capability.child.v1", record)
            holder = holder_key(parent["audience"])
            if algorithm == "hmac-sha256":
                _verify(algorithm, bytes.fromhex(links[index - 1]["signature"]), data, node["signature"])
                _verify(
                    algorithm,
                    holder,
                    _bytes("unimem.capability.parent-holder.v1", {"record": record, "signature": node["signature"]}),
                    node["authorization"],
                )
            else:
                if node["authorization"] != "":
                    raise ValueError("UniMem Ed25519 link has an unexpected local authorization")
                _verify(algorithm, holder, data, node["signature"])
        chain_id = fp.digest(_prefix(token, index + 1))
        if chain_id in revoked:
            raise PermissionError("UniMem capability chain was revoked")
        chain_ids.append(chain_id)
    return {
        "tenant": tenant,
        "issuer": issuer,
        "caveats": policy(links[-1]["record"]["caveats"]),
        "chain_ids": chain_ids,
        "chain_digest": fp.digest(dict(token)),
    }


def permits(
    caveats: Mapping[str, Any],
    *,
    namespace: str,
    operation: str,
    memory_class: str,
    purpose: str,
    labels: Sequence[str],
) -> bool:
    rules = policy(caveats)
    return bool(
        namespace in rules["namespaces"]
        and operation in rules["operations"]
        and memory_class in rules["classes"]
        and purpose in rules["purposes"]
        and set(labels) <= set(rules["labels"])
    )


def request_digest(method: str, target: str, raw: bytes, *, purpose: str) -> str:
    return fp.digest(
        {
            "method": _label(method),
            "target": fp.text(target, "request target", 8192),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
            "purpose": _label(purpose),
        }
    )


def prove(
    token: Mapping[str, Any],
    *,
    key: bytes,
    algorithm: str,
    audience: str,
    tenant: str,
    request_digest: str,
    nonce: str,
    now: str,
) -> dict[str, Any]:
    _token(token)
    issued = moment(now)
    record = {
        "type": "unimem.capability.possession.v1",
        "chain_digest": fp.digest(dict(token)),
        "audience": _label(audience),
        "tenant": _label(tenant),
        "request_digest": _label(request_digest),
        "nonce": _label(nonce),
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(seconds=30)).isoformat(),
    }
    return {"record": record, "signature": _sign(algorithm, key, _bytes("unimem.capability.possession.v1", record))}


def verify_proof(
    token: Mapping[str, Any],
    verified: Mapping[str, Any],
    proof: Mapping[str, Any],
    *,
    key: bytes,
    algorithm: str,
    request_digest: str,
    now: str,
) -> str:
    if not isinstance(proof, Mapping) or set(proof) != {"record", "signature"}:
        raise ValueError("UniMem invalid possession proof")
    record = proof["record"]
    fields = {"type", "chain_digest", "audience", "tenant", "request_digest", "nonce", "issued_at", "expires_at"}
    if not isinstance(record, dict) or set(record) != fields or len(fp.wire(dict(proof))) > 4096:
        raise ValueError("UniMem invalid possession proof fields")
    issued, expiry, stamp = moment(record["issued_at"]), moment(record["expires_at"]), moment(now)
    if (
        record["type"] != "unimem.capability.possession.v1"
        or not issued <= stamp < expiry
        or expiry > issued + timedelta(seconds=30)
        or record["chain_digest"] != fp.digest(dict(token))
        or record["audience"] != verified["caveats"]["audience"]
        or record["tenant"] != verified["tenant"]
        or record["request_digest"] != request_digest
    ):
        raise PermissionError("UniMem possession proof binding or freshness rejected")
    _verify(algorithm, key, _bytes("unimem.capability.possession.v1", record), proof["signature"])
    return _label(record["nonce"])


def headers(
    token: Mapping[str, Any],
    *,
    key: bytes,
    holder_algorithm: str,
    method: str,
    target: str,
    raw: bytes,
    purpose: str,
    nonce: str,
    now: str,
) -> dict[str, str]:
    links = _token(token)
    proof = prove(
        token,
        key=key,
        algorithm=holder_algorithm,
        audience=links[-1]["record"]["caveats"]["audience"],
        tenant=links[0]["record"]["tenant"],
        request_digest=request_digest(method, target, raw, purpose=purpose),
        nonce=nonce,
        now=now,
    )
    return {
        "X-UniMem-Capability": base64.urlsafe_b64encode(fp.wire(dict(token))).decode().rstrip("="),
        "X-UniMem-Capability-Proof": base64.urlsafe_b64encode(fp.wire(proof)).decode().rstrip("="),
        "X-UniMem-Purpose": purpose,
    }


def header_parameters() -> list[dict[str, Any]]:
    """Optional transport metadata; primary authentication remains required."""
    return [
        {
            "in": "header",
            "name": name,
            "required": False,
            "description": description,
            "schema": {"type": "string", "minLength": 1, "maxLength": bound},
        }
        for name, bound, description in (
            (
                "X-UniMem-Capability",
                98304,
                "Base64url UniMem attenuating token. Enrolled delegates must supply all three UniMem headers "
                "in addition "
                "to primary authentication; an unconfigured server rejects delegated requests.",
            ),
            (
                "X-UniMem-Capability-Proof",
                8192,
                "Base64url holder signature bound to the token, tenant, exact method/target/body, purpose and fresh "
                "nonce. "
                "Regenerate for every HTTP request; proofs expire after at most 30 seconds.",
            ),
            ("X-UniMem-Purpose", 256, "Declared purpose, authorized by the leaf capability and signed by the holder."),
        )
    ]


COMMERCIAL_LICENSE_TYPE = "unimem.commercial.license.v1"
COMMERCIAL_PLANS = frozenset({"free", "team", "scale", "enterprise", "sovereign"})


def parse_ed25519_public_key(raw: str) -> bytes:
    """Decode a 32-byte Ed25519 public key from hex or base64url."""
    value = fp.text(raw, "license public key", 256).strip()
    if len(value) == 64:
        try:
            key = bytes.fromhex(value)
            if len(key) == 32:
                return key
        except ValueError:
            pass
    padded = value + "=" * (-len(value) % 4)
    try:
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("UniMem license public key must be 32-byte hex or base64url") from exc
    if len(key) != 32:
        raise ValueError("UniMem license public key must decode to exactly 32 bytes")
    return key


def issue_commercial_license(
    *,
    private_key: bytes,
    org_id: str,
    plan: str,
    deployment_modes: Sequence[str],
    max_agents: int,
    features: Sequence[str],
    issued_at: str,
    expires_at: str,
    license_id: str,
) -> str:
    """Sign an offline Ed25519 commercial deployment license token."""
    plan_name = fp.text(plan, "license plan", 32).strip().lower()
    if plan_name not in COMMERCIAL_PLANS:
        raise ValueError(f"unsupported commercial license plan: {plan!r}")
    modes = sorted({_label(mode) for mode in deployment_modes})
    if not modes or len(modes) > 16:
        raise ValueError("commercial license requires 1 to 16 deployment modes")
    feat = sorted({_label(item) for item in features})
    if len(feat) > 64:
        raise ValueError("commercial license exceeds feature bound")
    if type(max_agents) is not int or (max_agents != -1 and not 1 <= max_agents <= 10_000_000):
        raise ValueError("max_agents must be -1 (unlimited) or between 1 and 10,000,000")
    issued, expires = moment(issued_at), moment(expires_at)
    if expires <= issued:
        raise ValueError("commercial license expiry must follow issued_at")
    record = {
        "type": COMMERCIAL_LICENSE_TYPE,
        "license_id": _label(license_id),
        "org_id": _label(org_id),
        "plan": plan_name,
        "deployment_modes": modes,
        "max_agents": max_agents,
        "features": feat,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
    }
    envelope = {
        "version": 1,
        "algorithm": "ed25519",
        "record": record,
        "signature": _sign("ed25519", private_key, _bytes(COMMERCIAL_LICENSE_TYPE, record)),
    }
    return base64.urlsafe_b64encode(fp.wire(envelope)).decode("ascii").rstrip("=")


def verify_commercial_license(
    token_or_envelope: str | Mapping[str, Any],
    *,
    public_key: bytes,
    now: str | None = None,
    deployment_mode: str | None = None,
    required_features: Sequence[str] = (),
) -> dict[str, Any]:
    """Verify an offline Ed25519 commercial license certificate and return its claims."""
    if isinstance(token_or_envelope, str):
        raw_text = token_or_envelope.strip()
        if not raw_text or len(raw_text) > 65_536:
            raise PermissionError("UniMem commercial license token missing or oversized")
        if raw_text.startswith("{"):
            decoded = json.loads(raw_text)
        else:
            padded = raw_text + "=" * (-len(raw_text) % 4)
            try:
                decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                raise PermissionError("UniMem commercial license encoding rejected") from exc
    elif isinstance(token_or_envelope, Mapping):
        decoded = dict(token_or_envelope)
    else:
        raise PermissionError("UniMem commercial license must be a token string or mapping")

    if not isinstance(decoded, dict) or set(decoded) != {"version", "algorithm", "record", "signature"}:
        raise PermissionError("UniMem invalid commercial license envelope")
    if type(decoded["version"]) is not int or decoded["version"] != 1 or decoded["algorithm"] != "ed25519":
        raise PermissionError("UniMem unsupported commercial license version or algorithm")
    record = decoded["record"]
    required_fields = {
        "type",
        "license_id",
        "org_id",
        "plan",
        "deployment_modes",
        "max_agents",
        "features",
        "issued_at",
        "expires_at",
    }
    if not isinstance(record, dict) or set(record) != required_fields:
        raise PermissionError("UniMem commercial license record fields invalid")
    if record.get("type") != COMMERCIAL_LICENSE_TYPE or record.get("plan") not in COMMERCIAL_PLANS:
        raise PermissionError("UniMem commercial license type or plan rejected")
    modes = record.get("deployment_modes")
    feats = record.get("features")
    max_agents = record.get("max_agents")
    if (
        not isinstance(modes, list)
        or not modes
        or any(not isinstance(m, str) for m in modes)
        or not isinstance(feats, list)
        or any(not isinstance(f, str) for f in feats)
        or type(max_agents) is not int
        or (max_agents != -1 and max_agents < 1)
    ):
        raise PermissionError("UniMem commercial license claim bounds rejected")
    _verify("ed25519", public_key, _bytes(COMMERCIAL_LICENSE_TYPE, record), decoded["signature"])
    issued, expires = moment(record["issued_at"]), moment(record["expires_at"])
    stamp = moment(now or fp.now())
    if not issued <= stamp < expires:
        raise PermissionError("UniMem commercial license is not active or has expired")
    if deployment_mode is not None and deployment_mode not in set(modes):
        raise PermissionError(f"UniMem commercial license does not permit mode {deployment_mode!r}")
    if required_features and not set(required_features) <= set(feats):
        missing = sorted(set(required_features) - set(feats))
        raise PermissionError(f"UniMem commercial license missing required features: {', '.join(missing)}")
    return dict(record)


def enforce_commercial_license(
    *,
    env_prefix: str = "UNIMEM",
    deployment_mode: str | None = None,
    required_features: Sequence[str] = (),
    now: str | None = None,
) -> dict[str, Any] | None:
    """Validate commercial licensing when required or when a license credential is supplied."""
    from unimem.configuration import getenv as _configuration_getenv

    required_raw = (_configuration_getenv(f"{env_prefix}_COMMERCIAL_LICENSE_REQUIRED", "") or "").strip().lower()
    required = required_raw in {"1", "true", "yes", "on"}
    token = (_configuration_getenv(f"{env_prefix}_LICENSE_KEY", "") or "").strip()
    token_file = (_configuration_getenv(f"{env_prefix}_LICENSE_FILE", "") or "").strip()
    if not token and token_file:
        try:
            with open(token_file, encoding="utf-8") as handle:
                token = handle.read(65_537).strip()
        except OSError as exc:
            raise PermissionError(f"cannot read {env_prefix}_LICENSE_FILE: {exc}") from exc
    pub_raw = (_configuration_getenv(f"{env_prefix}_LICENSE_PUBLIC_KEY", "") or "").strip()
    if not token and not required:
        return None
    if not pub_raw:
        raise PermissionError(f"{env_prefix}_LICENSE_PUBLIC_KEY is required to verify commercial licenses")
    if not token:
        raise PermissionError(f"{env_prefix}_LICENSE_KEY or {env_prefix}_LICENSE_FILE is required")
    public_key = parse_ed25519_public_key(pub_raw)
    return verify_commercial_license(
        token,
        public_key=public_key,
        now=now,
        deployment_mode=deployment_mode,
        required_features=required_features,
    )
