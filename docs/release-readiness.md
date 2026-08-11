# Release readiness

## Current decision

**Do not label the current tree production-ready.** It is a pre-alpha, local Python
control core with a tested CLI, experimental MCP SDK adapter, and deterministic
structural-disclosure benchmark. It does not yet have a production provider adapter,
secret broker, sandbox, persistence layer, authentication/tenant boundary, or
deployment hardening guide.

## Evidence currently available

| Area | Evidence | Release interpretation |
|---|---|---|
| Manifest and registry | JSON `v1alpha1` parsing, immutable revisions, activation pointers, dependency/conflict checks | Suitable for local/core experimentation; not a compatibility guarantee |
| Search/load/execute core | Deterministic lexical search, scope-bound references, section/operation loading, policy/budget/audit tests | Python-library behavior only; no public transport contract |
| Skill intake | Filesystem-only `SKILL.md` discovery without script execution | Content discovery, not Skill execution or sandboxing |
| Local dashboard | Read-only loopback server with live Inventory and local Health snapshot | Local inspection only; no remote connection probe, authentication, or mutations |
| CLI and MCP | Seven local commands and three MCP tools tested with the official SDK's in-memory client | Experimental local interface; read-only catalog generations refresh atomically after a lightweight change check |
| Benchmark | Pinned 100-definition, five-kind fixture run in `benchmarks/reference-run.json` | Structural exposure evidence only; not model-quality evidence |

## Benchmark claim boundary

The reference run uses oracle-supplied expected target revisions. The harness can therefore compare equal eager definitions against a fixed lazy meta-tool payload plus the selected search card and selected definition, and can verify that every fixture provider kind is represented. It cannot establish whether a model would choose the correct capability from natural-language intent.

Do not describe the result as tool-selection accuracy, semantic routing accuracy, reasoning-token reduction, provider latency reduction, production cost reduction, or successful provider execution. The portable estimator (`utf8-bytes-div-4-v1`) is a reproducible context-size proxy, not any model provider's billable tokenizer.

## Required before a beta or production claim

- Stabilize and version the CLI/MCP contracts beyond the current pre-alpha adapter.
- Add authenticated tenant/principal handling, durable state, lifecycle/rollback, and a documented deployment profile.
- Add production-grade provider isolation, secret handling, network/process policy enforcement, and explicit incident/recovery procedures.
- Run adversarial, failure, cache-invalidation, authorization, and multi-tenant validation against actual adapters.
- Add model-backed, stratified evaluation for semantic selection accuracy and report model/version, reasoning tier, prompts, seeds, latency, tool calls, tokenizer, and pricing assumptions separately.
- Publish compatible API/versioning, support, privacy/retention, and operational documentation.

## Lightweight release checks

From the repository root after installing development dependencies:

```bash
python -m pytest
python -m ruff check src tests benchmarks
python -m mypy
python -c "from benchmarks.harness import assert_release_thresholds, run_benchmark; assert_release_thresholds(run_benchmark())"
```

The final command only verifies the deterministic fixture gate. It is necessary for a benchmark artifact update, but it is not sufficient for a software release decision.
