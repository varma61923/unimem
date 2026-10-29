"""`unimem demo`: the whole memory loop -- remember, measure, govern -- offline, in seconds.

Everything except the agent is real: a fresh store, bitemporal facts, the
deterministic holdout randomization, the integrity audit, the causal analysis and
harm withdrawal. The agent and its outcomes are simulated with planted effects, so
the report can show the ground truth next to what UniMem measured.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any

MEMORIES = (
    ("verify-owner", "Verify the account owner before any password reset.", 0.20),
    ("retry-429", "On HTTP 429, back off exponentially and retry.", 0.0),
    ("legacy-refund-api", "Issue refunds through the v1 refunds endpoint.", -0.20),
)
BASE_SUCCESS = 0.55


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "demo",
        help="A 30-second offline tour of the memory loop: remember, measure, govern.",
    )
    p.add_argument("--occasions", type=int, default=1000,
                   help="Simulated tasks to run through the holdout (default 1000, minimum 100).")
    p.add_argument("--seed", type=int, default=7, help="Seed for the simulated agent's outcomes.")
    p.add_argument("--keep", metavar="DIR", default=None,
                   help="Write the demo store to DIR (must not exist) and keep it for inspection.")
    p.add_argument("--json", action="store_true", help="Print the result as JSON.")
    p.set_defaults(func=run)


def _cli(*argv: str) -> str:
    from unimem import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    if code:
        raise RuntimeError(f"unimem {' '.join(argv[:2])} failed: {err.getvalue().strip()}")
    return out.getvalue()


def _remember(root: str) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    switch = today - timedelta(days=120)
    _cli("fact", "add", "Refunds are issued through the v1 refunds endpoint.", "--category", "tool_rule",
         "--valid-from", (today - timedelta(days=540)).isoformat(),
         "--valid-until", (switch - timedelta(days=1)).isoformat(), "--dest", root)
    _cli("fact", "add", "Refunds are issued through the v2 payments API.", "--category", "tool_rule",
         "--valid-from", switch.isoformat(), "--dest", root)
    question = "How are refunds issued?"
    as_of = (today - timedelta(days=300)).isoformat()

    def top(*extra: str) -> str:
        items = json.loads(_cli("recall", question, "--channel", "facts", "--json", *extra, "--dest", root))["items"]
        return str(items[0]["text"]) if items else ""

    return {"question": question, "today": top(), "as_of": as_of, "then": top("--as-of", as_of)}


def _measure(root: str, occasions: int, seed: int) -> dict[str, Any]:
    from unimem import holdout_io
    from unimem.measure import CausalMemory

    memories = [{"id": key, "text": text} for key, text, _ in MEMORIES]
    planted = {key: effect for key, _, effect in MEMORIES}
    holdout_io.configure(root, rate=0.5, salt=f"demo-{seed}", note="unimem demo")
    memory = CausalMemory(lambda task, **_: memories, root=root, durable=False)
    rng = random.Random(seed)
    for i in range(occasions):
        occasion = f"ticket-{i:05d}"
        shown = memory.recall("customer support ticket", occasion_id=occasion, context="support")
        p = BASE_SUCCESS + sum(planted[m["id"]] for m in shown)
        memory.record_outcome(occasion, succeeded=rng.random() < min(0.98, max(0.02, p)))
    report = json.loads(_cli("experiment", "--json", "--dest", root))
    rows = []
    for key, _, effect in MEMORIES:
        found: dict[str, Any] = next((e for e in report["effects"] if e["lesson_slug"] == key), {})
        rows.append({
            "memory": key, "planted": effect, "verdict": found.get("verdict", "UNDERPOWERED"),
            "effect": found.get("effect"), "ci": [found.get("ci_low"), found.get("ci_high")],
            "with": found.get("rate_injected"), "without": found.get("rate_withheld"),
        })
    return {"occasions": occasions, "integrity": report["integrity"]["verdict"], "memories": rows}


def _govern(root: str) -> dict[str, Any]:
    from unimem import retrieval_io
    from unimem.measure import CausalMemory

    retrieval_io.configure(root, harm_policy="withdraw", note="unimem demo")
    memories = [{"id": key, "text": text} for key, text, _ in MEMORIES]
    memory = CausalMemory(lambda task, **_: memories, root=root, durable=False, check_every=1, graduate=True)
    result = memory.recall_detailed("customer support ticket", occasion_id="ticket-next", record=False)
    settled = set(result.withdrawn) | set(result.graduated)
    return {"withdrawn": sorted(result.withdrawn), "graduated": sorted(result.graduated),
            "under_test": [key for key, _, _ in MEMORIES if key not in settled]}


def _pct(value: float | None) -> str:
    return "   n/a" if value is None else f"{value * 100:+5.0f}%"


def _print(result: dict[str, Any]) -> None:
    r, m, g = result["remember"], result["measure"], result["govern"]
    print(f"UniMem demo  ·  offline, no LLM, no API key  ·  {result['seconds']:.1f}s\n")
    print("1/3 REMEMBER  facts that know when they were true")
    print(f"    Q: {r['question']}")
    print(f"    today            → {r['today']}")
    print(f"    as of {r['as_of']} → {r['then']}\n")
    print(f"2/3 MEASURE   {m['occasions']:,} simulated support tickets; every memory randomly withheld on half")
    print(f"    integrity audit: {m['integrity']}")
    print(f"    {'memory':<20}{'planted':>9}{'measured':>10}   {'95% interval':<17}verdict")
    for row in m["memories"]:
        low, high = row["ci"]
        interval = "" if low is None else f"[{low * 100:+.0f}%, {high * 100:+.0f}%]"
        planted, measured = _pct(row["planted"]), _pct(row["effect"])
        print(f"    {row['memory']:<20}{planted:>9}{measured:>10}   {interval:<17}{row['verdict']}")
    print()
    print("3/3 GOVERN    the evidence changes what the agent sees next")
    print(f"    withdrawn  (proven harmful, never delivered)  {', '.join(g['withdrawn']) or 'none'}")
    print(f"    graduated  (proven helpful, always delivered) {', '.join(g['graduated']) or 'none'}")
    print(f"    under test (still randomized)                 {', '.join(g['under_test']) or 'none'}\n")
    print("The agent and its outcomes are simulated with planted effects. The store, randomization,")
    print("integrity audit, causal analysis and withdrawal are the real product.")
    if result.get("store"):
        print(f"Store kept at {result['store']}  ·  try: unimem experiment --dest {result['store']}")
    print("Next: unimem init --dest ./mem   ·   unimem install --target claude-code")


def run(args: argparse.Namespace) -> int:
    if args.occasions < 100 or args.occasions > 100_000:
        print("[unimem] error: --occasions must be between 100 and 100000", file=sys.stderr)
        return 2
    if args.keep and os.path.exists(args.keep):
        print(f"[unimem] error: {args.keep} already exists; choose a new directory", file=sys.stderr)
        return 2
    root = args.keep or tempfile.mkdtemp(prefix="unimem-demo-")
    started = time.monotonic()
    try:
        _cli("init", "--dest", root)
        result: dict[str, Any] = {"remember": _remember(root), "measure": _measure(root, args.occasions, args.seed),
                                  "govern": _govern(root)}
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    result["seconds"] = round(time.monotonic() - started, 2)
    if args.keep:
        result["store"] = os.path.abspath(root)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print(result)
    return 0
