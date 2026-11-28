from __future__ import annotations

import datetime
import json
import os

from unimem import approval, paths, release

ENVIRONMENTS = ("dev", "stage", "prod")

PROTECTED_ENVIRONMENTS = ("prod",)

ENVIRONMENTS_FILENAME = "environments.jsonl"


class EnvironmentError(ValueError): ...


def environments_log_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), ENVIRONMENTS_FILENAME)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _utc(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None else value.astimezone(datetime.timezone.utc)


def _parse_moment(raw: object) -> datetime.datetime:
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return _utc(datetime.datetime.fromisoformat(text))


def read_all(root: str) -> list[dict]:
    path = environments_log_path(root)
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("environment") and record.get("release_id"):
                out.append(record)
    return out


def promote(
    root: str,
    release_id: str,
    environment: str,
    *,
    actor: str = "",
    reason: str = "",
    activate_at: datetime.datetime | None = None,
) -> dict:
    if environment not in ENVIRONMENTS:
        raise EnvironmentError(f"unknown environment {environment!r}; known environments: " + ", ".join(ENVIRONMENTS))
    target = release.find(root, release_id)
    if target is None:
        raise EnvironmentError(f"no such release: {release_id}")

    if environment in PROTECTED_ENVIRONMENTS:
        policy = approval.load_policy(root)
        previous_id = current(root, environment)
        previous = release.find(root, previous_id) if previous_id else None
        previous_slugs = {e.slug for e in previous.entries} if previous else set()
        new_slugs = sorted({e.slug for e in target.entries} - previous_slugs)
        for slug in new_slugs:
            approval.check(
                policy,
                slug=slug,
                approver=actor,
                authors=approval.authors_of(root, slug),
            )

    moment = _utc(activate_at) if activate_at is not None else _now()
    record = {
        "release_id": release_id,
        "environment": environment,
        "promoted_at": _now().isoformat(),
        "activate_at": moment.isoformat(),
        "actor": actor or "unknown",
        "reason": reason,
    }
    path = environments_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record


def current(root: str, environment: str, *, now: datetime.datetime | None = None) -> str | None:
    moment = _utc(now) if now is not None else _now()
    eligible = [
        (r, _parse_moment(r["activate_at"]))
        for r in read_all(root)
        if r["environment"] == environment and _parse_moment(r["activate_at"]) <= moment
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda pair: pair[1])[0]["release_id"]


def pending(root: str, environment: str, *, now: datetime.datetime | None = None) -> list[dict]:
    """Promotions scheduled for the future, soonest first."""
    moment = _utc(now) if now is not None else _now()
    future = [
        (r, _parse_moment(r["activate_at"]))
        for r in read_all(root)
        if r["environment"] == environment and _parse_moment(r["activate_at"]) > moment
    ]
    return [r for r, _at in sorted(future, key=lambda pair: pair[1])]


def history(root: str, environment: str) -> list[dict]:
    return [r for r in read_all(root) if r["environment"] == environment]
