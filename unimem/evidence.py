"""Each lesson's measured causal evidence, for the local retrieval surfaces."""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone

from unimem import experiment, harm, holdout_io, integrity, paths

TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 128
"""Upper bound, in seconds, on how stale a cached evidence payload can be.

An entry is served only while the file-stats key still matches the store
(no new outcomes, assignments, config, episodes, or traces) AND it is younger
than this. File stats alone cannot catch every change — a write that preserves
both size and mtime slips past them — so this TTL is the backstop that bounds
how long such a blind spot can survive. Retrieval calls for_lessons on every
request; without the cache each one would recompute the whole analysis.
"""

_cache: OrderedDict[tuple[str, tuple], tuple[tuple, float, dict]] = OrderedDict()
_cache_lock = threading.Lock()


def _after_fork() -> None:
    global _cache_lock
    _cache_lock = threading.Lock()
    _cache.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
"""Evidence payloads, keyed by ((root, query key) -> (file-stats key, cached-at, payload)).

The key has TWO parts on purpose. The file-stats key (see _key) detects that
the store changed; the query key records WHAT was asked for (currently the
requested lesson set — None means "every measured lesson"). A subset query
must never be served the full payload or vice versa, and any future analysis
parameter (significance level, target effect, salt override, ...) MUST join
the query key, or two different questions will share one cached answer.
attach() deliberately uses the unfiltered query so one cache entry serves
every retrieval surface.
"""


@dataclass(frozen=True)
class Analysis:
    all_rows: list
    rows: list
    rate: float
    corrupt: int
    wanted_salt: str
    n_other_salt: int
    report: integrity.IntegrityReport | None
    effects: list


def analyse(root: str) -> Analysis:
    """The local experiment, computed the one way it is computed."""
    from unimem.commands import experiment_cmd

    all_rows, rate, corrupt = experiment_cmd._load(root)
    if not all_rows:
        return Analysis(all_rows, [], rate, corrupt, "", 0, None, [])
    rows, wanted_salt, n_other_salt = experiment_cmd.scope_to_current_salt(root, all_rows)
    if not rows:
        return Analysis(all_rows, rows, rate, corrupt, wanted_salt, n_other_salt, None, [])
    report = integrity.audit(rows)
    effects = experiment.analyze(experiment_cmd._observations(rows), sequential=True)
    return Analysis(all_rows, rows, rate, corrupt, wanted_salt, n_other_salt, report, effects)


def _stat(path: str) -> tuple:
    try:
        st = os.stat(path)
    except OSError:
        return (None, None, None, None, None)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _key(root: str) -> tuple:
    return (
        os.path.exists(holdout_io.holdout_log_path(root)),
        _stat(holdout_io.outcomes_log_path(root)),
        _stat(holdout_io.config_path(root)),
        _stat(paths.episodes_dir(root)),
        _stat(paths.traces_dir(root)),
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def for_lessons(root: str, *, lesson_slugs: Collection[str] | None = None) -> dict:
    """{"available", "reason", "measured_at", "by_lesson"} for this store.

    When `lesson_slugs` is given, only those lessons appear in `by_lesson`
    (missing ones are simply absent — attach() reports them as NOT_MEASURED);
    None returns every measured lesson. The requested set is part of the
    cache key, so a filtered query can neither poison nor reuse the full
    payload's entry. Entries live at most TTL_SECONDS (see above).
    """
    root = os.path.abspath(root)
    wanted = frozenset(lesson_slugs) if lesson_slugs is not None else None
    query_key = (wanted,)
    cache_key = (root, query_key)
    file_key = _key(root)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None and cached[0] == file_key and now - cached[1] < TTL_SECONDS:
            _cache.move_to_end(cache_key)
            return cached[2]

    measured_at = datetime.now(timezone.utc).isoformat()
    analysis = analyse(root)
    if analysis.report is None:
        evidence = {"available": False, "reason": "no experiment data", "measured_at": measured_at, "by_lesson": {}}
    elif not analysis.report.readable:
        evidence = {
            "available": False,
            "reason": (
                "The experiment is COMPROMISED, so no effects are shown. Call `experiment_status` for what to fix."
            ),
            "measured_at": measured_at,
            "by_lesson": {},
        }
    else:
        by_lesson = {
            e.lesson_slug: {
                "verdict": e.verdict,
                "effect": _round(e.effect),
                "ci_95": [_round(e.ci_low), _round(e.ci_high)],
                "n_injected": e.n_injected,
                "n_withheld": e.n_withheld,
            }
            for e in analysis.effects
        }
        if wanted is not None:
            by_lesson = {slug: item for slug, item in by_lesson.items() if slug in wanted}
        evidence = {"available": True, "reason": "", "measured_at": measured_at, "by_lesson": by_lesson}
    with _cache_lock:
        _cache[cache_key] = (file_key, now, evidence)
        _cache.move_to_end(cache_key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
    return evidence


def attach(root: str, result: dict, *lists: str) -> None:
    """Add evidence to the lesson dicts under each named key of `result`."""
    evidence = for_lessons(root)
    if not evidence["available"] and evidence["reason"] == "no experiment data":
        return
    result["evidence"] = {k: evidence[k] for k in ("available", "reason", "measured_at")}
    if not evidence["available"]:
        return
    for name in lists:
        for lesson in result.get(name) or []:
            lesson["evidence"] = evidence["by_lesson"].get(lesson.get("slug"), {"verdict": "NOT_MEASURED"})


def graduated(root: str) -> dict[str, dict]:
    """Memories the anytime-valid verdict already shows HELPING; empty when unreadable."""
    try:
        evidence = for_lessons(root)
    except Exception:  # noqa: BLE001 - same contract as withdrawn(): no evidence, no action
        return {}
    if not evidence.get("available"):
        return {}
    return harm.helps(evidence.get("by_lesson", {}))


def withdrawn(root: str, policy: str) -> dict[str, dict]:
    if policy != harm.POLICY_WITHDRAW:
        return {}
    try:
        evidence = for_lessons(root)
    except Exception:  # noqa: BLE001 - see docstring
        return {}
    if not evidence.get("available"):
        return {}
    withdrawn_lessons = harm.hurts(evidence.get("by_lesson", {}))
    if withdrawn_lessons:
        from unimem import lineage

        for slug in withdrawn_lessons:
            lineage.taint(root, slug, "randomized evidence: harm withdrawal")
    return withdrawn_lessons
