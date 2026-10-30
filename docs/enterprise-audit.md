# Diagnostic audit manifest

Baseline: `f8dbc93`, branch `fix/principal-audit-hardening`.

## Execution and ownership map (phase 1)

Static Python inventory covers 465 production modules and 6,607 function definitions
(including nested functions/methods, excluding tests and migrations). Import edges
are useful for navigation but cannot prove dynamic plugin code is dead.

| Surface | Entry and wiring | State / lifecycle |
| --- | --- | --- |
| CLI | pyproject script -> unimem.cli:main -> unimem.commands | local memory directory |
| MCP | unimem.mcp_server, unimem.mcp_tools | canonical store; bounded async workers |
| Local HTTP / UI | unimem.gateway; packaged unimem/ui | local memory; authority-bound reads and writes |
| Native hooks | generated HookKit manifests -> hook_bridge -> hook_service -> HookKit | Unix socket, journal, delivery acknowledgements |
| SDK integrations | unimem.integrations, framework_middleware; sdk/{typescript,go,rust,java} | caller-owned runtime and credentials |
| Hub | hub.main -> hub.server | PostgreSQL async sessions, tenant context / RLS |
| Hub HTTP routes | server installs admin, console, signup, rest, otlp, connector, scim and health routes | configuration controls optional surfaces; OpenAPI freshness gate |
| Local jobs | unimem.jobs handler registry, daemon, distillation_worker | SQLite WAL, leases, fencing, retries and dedupe |
| Hub workers | server lifespan starts scheduler alerts, webhooks, connector sweep, auth invalidation | shutdown stop event and task lifecycle |
| Retrieval | fact_index, search_recipes, memory_journal, vector and graph backends | canonical records plus rebuildable projections / caches |

Existing graph extraction and integrity tooling: `docs/architecture-exploration.md`
and `scripts/graphify_audit.py`. Static candidates require caller, plugin, CLI,
packaging and compatibility review before removal. Ruff reports no unused imports.
No inspected orphan, draft template or abstract capability stub has sufficient evidence
for safe deletion. Conditional Hub routes are deliberately wired, not abandoned.

## Prioritized findings

| ID | Priority | Phase | Evidence / action |
| --- | --- | --- | --- |
| A01 | P1 | 2 | CI failed two functional hook tests under production deadlines. Use functional scheduling headroom; preserve production latency tests. |
| A02 | P1 | 4 | Hook selection assumes trigger objects and numeric priorities inside arbitrary record payloads. Skip malformed selection metadata; valid records must still be delivered. |
| A03 | P2 | 3 | Journal.vectors performs one SELECT per candidate; HookKit caches missing embeddings in individual transactions. Batch both paths. |
| A04 | P2 | 2 | Optional embedding/provider failures clear candidates silently. Emit bounded error-type telemetry without content. |
| A05 | P3 | 1/5 | Consolidated execution map and evidence limits were absent. Maintain this review record. |

## Verification and limits

Baseline CI: 8,750 passed, 236 skipped, two hook failures; coverage 84.35%.
Other baseline CI jobs passed (except explicitly skipped Scorecard).
Focused tests and lint run after each increment. Final verification uses the full
suite, types, builds and remote CI. Phase gates must not be described as full CI
success until the corresponding workflow actually finishes.

This is an evidence-based review, not proof that every vulnerability, race, loose
type or unreachable callback has been eliminated. Global strict typing and blanket
retry/compression changes require per-interface compatibility and threat analysis.
Optional engines, public SDK exports and unsupported write capabilities are retained.
Codex Security Cloud previously denied workspace access; local scanners and CI results
must not be presented as a successful Cloud scan.

## Implemented increments

- Phase 1: execution/ownership inventory and reviewed removal decisions.
- Phase 2: functional hook tests use explicit scheduling headroom; native response
  deadlines remain 50 ms. Optional provider failure telemetry exports only error type.
- Phase 3: candidate vectors load in one indexed join, preserving misses, duplicate
  requests and caller order. Missing embeddings persist in one atomic transaction;
  non-finite vectors fail before any writes. Single-vector callers retain their API.
- Phase 4: malformed trigger objects/values and nonnumeric or excessive priorities
  are skipped per record. Priorities are finite numbers within +/-1e100. A malformed
  record cannot suppress an otherwise valid directive. No raw record data is logged.
- Phase 5: regression tests cover unsafe selectors, safe provider degradation,
  cache model/content isolation, large batches, atomic rejection and SQL query count.
  CI uploads a generated coverage report even if tests fail, retaining diagnostics.

Focused gates: 53 hook tests after phase 2; 46 journal/hook tests after phase 3;
132 hook/journal/envelope tests after phase 4; 42 journal/service/graph tests after
phase 5, all with warnings promoted to errors. Full lint/format pass. Mypy baseline
passes 632 files; its notes about untyped test bodies are not proof of strict typing.
Full-suite coverage, packaging, dependency audits and final remote CI are pending
at the time of this commit. Never infer a completed remote result from these gates.
