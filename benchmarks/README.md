# CapabilityHub benchmark suite

This directory defines the repeatable evidence for CapabilityHub's staged-disclosure claim. The suite compares a full eager catalog against the same catalog exposed through CapabilityHub's fixed meta-tools and lazy retrieval. It measures saved context without treating a token estimate as universal model billing.

## Required fixture layout

```text
benchmarks/
  fixtures/
    manifests/<fixture>.json
    capabilities/<id>/<revision>/definition.json
    tasks/<fixture>.jsonl
    providers/<type>/<scenario>.json
    expected/<fixture>.json
  runs/<run-id>/                 # generated; never used as fixture input
```

Definitions must be immutable fixture data. A manifest enumerates all enabled records; each record includes `id`, `type`, `revision_digest`, compact metadata, definition path, permission class, dependencies, and size limits. Task JSONL supplies an ID, prompt, fixture name, expected route, allowed equivalents, expected result/refusal, and minimum-evidence annotation. Provider scenario files deterministically emulate Skills, MCP, CLI, API, and RAG behavior, including errors.

## Benchmark modes

| Mode | Catalog/input behavior | Required assertion |
|---|---|---|
| `eager` | Entire enabled capability catalog/definitions in initial prompt | Baseline is complete and revision-matched |
| `lazy-cold` | Metadata + fixed meta-tool contract; empty cache | Disclosure only follows logged actions |
| `lazy-warm` | Same lazy protocol with valid cache | Reuse is auditable and authorization-scoped |
| `lazy-relevant-invalidation` | Warm cache then relevant definition/index/policy change | No stale use |
| `lazy-unrelated-invalidation` | Warm cache then unrelated revision change | Relevant cache remains usable |

Use the exact task order for every paired eager/lazy trial. Start a new agent context for every task. Never include a precomputed answer, expected route, or hidden fixture annotation in the agent-visible prompt.

## Provider coverage matrix

Every release requires every cell below. `Control` means discovery/inspection/permission/execution/audit works, not merely that a definition parses.

| Provider | Discover | Inspect/load | Execute/retrieve | Permission denial | Failure | Revision/cache |
|---|---:|---:|---:|---:|---:|---:|
| Skill | required | required | required | required | required | required |
| MCP | required | required | required | required | required | required |
| CLI | required | required | required | required | required | required |
| API | required | required | required | required | required | required |
| RAG | required | required | required | required | required | required |

Mixed fixtures must also prove one task can select a capability of each type without absorbing all other provider definitions. MCP and API scenarios must include schema validation and remote-error simulation; CLI scenarios include non-zero exit/timeout; RAG includes no-hit, stale-index, oversized result, and malicious document cases.

## Event contract and accounting

Record one JSON object per event, with at least:

```json
{
  "run_id": "...",
  "task_id": "...",
  "configuration": "lazy-cold",
  "cache_state": "cold",
  "sequence": 12,
  "event": "capability_loaded",
  "capability_id": "...",
  "provider_type": "mcp",
  "revision_digest": "sha256:...",
  "payload_bytes": 0,
  "portable_tokens": 0,
  "exact_input_tokens": 0,
  "model": "...",
  "reasoning_tier": "...",
  "authorization": "allowed",
  "outcome": "success"
}
```

Emit events for initial context construction, search, inspect/load, permission decision, cache hit/miss/invalidation/eviction, provider request/result, retry, execution, retrieval, and final assessment. Canonicalize payloads (UTF-8, LF, sorted JSON keys) before measuring bytes and portable tokens. Exact tokens use the selected model tokenizer; portable tokens use a pinned open tokenizer. Preserve both counts, tokenizer names/versions/hashes, and rendered prompt digests.

## Scoring

Score against structured expected results, not an evaluator's subjective preference. Produce per-task and aggregate values for selection accuracy, control accuracy, provider coverage, unauthorized execution, false success, stale cache use, load-but-unused, over-disclosure, token counts, tool calls, and latency. Compute a 10,000-resample stratified bootstrap confidence interval for eager/lazy accuracy differences and token reduction. Run >=30 stochastic trials per task/configuration/model/tier pair; deterministic configurations must have identical repeated traces.

The release gates are defined in [the validation plan](../docs/validation-plan.md): all provider cells, zero hard failures, context reduction, accuracy non-inferiority, loading discipline, cache correctness, and bounded latency. Report cold and warm cache results separately.

## Adversarial and regression requirements

The suite must include deceptive/duplicate names, keyword-stuffed metadata, prompt injections in capability descriptions and RAG, oversized definitions/results, cyclic dependencies, conflicting revisions, unsupported schemas, denied credentials, malformed responses, timeouts, and failing CLI commands. Any regression in a protected metric beyond the plan's threshold fails the run. Pin the accepted reference run and compare new results by fixture, provider type, cache state, and reasoning tier; an aggregate pass cannot mask a provider-specific failure.

## Model and reasoning-tier matrix

For each supported model revision, run the core stratified suite at each advertised reasoning tier with fixed temperature, seed, output cap, tool budget, and timeout. Report provider-reported reasoning tokens only when actually available; otherwise record `unobserved`. Exact token and price comparisons are model-specific; portable tokens, bytes, control traces, selection accuracy, and coverage are the cross-model evidence.

## Minimum report

Each generated run must include raw `events.jsonl`, task scores, environment and fixture digests, configuration/model/tier/cache metadata, per-provider matrix, p10/p50/p95 latency and token summaries, confidence intervals, and the eager/lazy rendered prompt digests. A reviewer must be able to replay the report entirely from these artifacts and the pinned fixtures.
