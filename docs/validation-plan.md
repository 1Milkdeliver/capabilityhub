# CapSift validation plan

## Claim under test

CapSift may claim a context/cost benefit only if it exposes substantially less capability material than an eager catalog **while preserving correct routing, safe execution, and control** for Skills, MCP, CLI, API, and RAG. This plan tests that claim reproducibly. It deliberately separates measured facts from provider- or model-specific estimates.

The primary comparison is between two configurations over identical manifests, tasks, provider availability, cache state, and model settings:

| Configuration | Always supplied to the agent | On-demand material |
|---|---|---|
| Eager baseline | Every enabled capability's complete definition: schemas, instructions, examples, and RAG excerpts | None; all is preloaded |
| Lazy CapSift | A fixed meta-tool contract plus compact catalog metadata (ID, type, title, tags, short description, permission class, revision) | Search results, inspected definitions, and RAG excerpts, only after an auditable action |

The eager baseline must not be weakened: it contains the same enabled capabilities and current revisions as the lazy run. The lazy system must record every disclosed byte/token, decision, tool call, permission decision, cache hit, and execution result.

## Reproducible benchmark protocol

1. Pin a benchmark release containing immutable manifest fixtures, task fixtures, expected outcomes, and SHA-256 digests. Each run records the Git revision, fixture digest, OS/runtime, provider adapter versions, tokenizer version, model ID, reasoning tier, prompt template revision, and random seed.
2. Materialize the manifest into both configurations. Verify that the provider coverage matrix is complete and that the eager catalog is a byte-for-byte concatenation of the same definitions that lazy loading could disclose.
3. Run each task in cold-cache and warm-cache modes. Use a fresh conversation/context per task; run at least 30 trials per stochastic model/task/configuration pair. For deterministic routers, run once per fixture plus a rerun to verify identical event traces.
4. Freeze provider simulators: no live web, clock, filesystem, or remote service dependency. Networked adapters are tested only through recorded request/response fixtures, including errors and timeouts.
5. Score outcomes from structured traces and expected assertions, blind to configuration. Publish raw JSONL events, per-task results, summaries, and failure artifacts; do not publish only averages.

### Task families and fixtures

Fixtures should be small enough to inspect and large enough to make eager disclosure expensive. Every provider must appear both alone and in mixed-provider workloads.

| Fixture | Contents | Assertions |
|---|---|---|
| `single/<type>` | One relevant and several irrelevant capabilities for each of Skill, MCP, CLI, API, RAG | Finds/uses the intended item; does not load irrelevant bodies |
| `mixed/operations` | A realistic project catalog (recommended: 100+ entries) spanning all five types | Correct cross-type routing and minimum necessary disclosures |
| `conflicts` | Duplicate aliases, incompatible versions, same-name tools, and policy conflicts | Reports ambiguity or applies deterministic precedence; never silently executes the wrong item |
| `permissions` | Read-only, network, secret, shell, and denied capabilities | Enforces permission gate before execution and logs the decision |
| `failure` | Invalid schema, malformed response, timeout, unavailable server, non-zero CLI exit, stale RAG index | Correct error classification, bounded retries, and no false success |
| `adversarial` | Prompt-injection text in descriptions/docs/RAG, deceptive tool names, keyword stuffing, huge definitions, cyclic dependencies | Treats untrusted content as data; applies size/dependency limits and selects safely |
| `churn` | Revision changes, disable/enable events, permission changes, RAG document deletion | Invalidates affected cache entries; never uses stale material |
| `load-unused` | Plausible decoys designed to cause inspection without execution | Quantifies unnecessary loads and limits speculative disclosure |

Task expected outcomes should distinguish: correct capability selected, correct control action (search/inspect/execute), authorized execution, semantic result, and safe refusal. A task may be correct without executing if policy requires refusal.

## Measurements

### Token and context accounting

Store all payloads as UTF-8 and calculate three reported measures:

- **Exact prompt tokens:** tokenize the fully rendered agent input with the selected model's documented tokenizer. Include system/meta-tool definitions, catalog metadata, loaded bodies, schemas, tool results, and RAG excerpts. Report input tokens, output tokens, and total separately.
- **Canonical portable tokens:** tokenize the same normalized UTF-8 payload with a pinned, open tokenizer (for example `cl100k_base`, version and vocabulary hash recorded). This is the cross-model comparison metric; it is not claimed to be a provider bill.
- **Payload bytes:** raw UTF-8 bytes of each disclosure category. This makes results auditable if a tokenizer changes.

Normalize line endings to LF, serialize JSON canonically (sorted keys, no insignificant whitespace), and redact secrets before accounting. Record token deltas by event: initial context, catalog metadata, search response, inspect/load response, execution schema/result, RAG retrieval, and retry/error. A run's context saving is `(eager_total - lazy_total) / eager_total`; report the distribution, not merely an aggregate.

### Model-independent quality and control metrics

Use the following trace-derived measures across any model/router:

| Metric | Definition | Desired direction |
|---|---|---|
| Selection accuracy | Tasks selecting the expected capability or allowed equivalent / eligible tasks | Higher |
| Control accuracy | Correct discovery, inspection, permission, execution, and error-handling sequence / tasks | Higher |
| Provider coverage | Provider-type test cells passed / required cells | 100% |
| Unauthorized execution rate | Executions denied by fixture policy / execution attempts | 0 |
| False-success rate | Failures represented as successes / injected failures | 0 |
| Load-but-unused rate | Loaded bodies never used in a subsequent successful decision/execution / all loaded bodies | Lower |
| Over-disclosure ratio | Disclosed portable tokens beyond the minimum evidence set / minimum evidence tokens | Lower |
| Stale-cache rate | Uses a superseded definition/retrieval after invalidation / invalidation tests | 0 |
| Determinism | Matching canonical event traces on repeated deterministic runs | 100% |

The benchmark harness must calculate a minimum evidence set from fixture annotations: the smallest catalog fields, definition body, schema, and RAG excerpts needed for the expected route. This supports over-disclosure and load-but-unused measurements without judging model prose.

### Latency and cost trade-offs

Report wall-clock p50/p95 separately for discovery, first usable definition, first execution, completion, and provider round trips. Compare cold and warm cache; never average them together. Lazy loading can increase first-action latency; the report must show this alongside token savings. Compute estimated monetary cost only as a scenario layer: `input_tokens * published input price + output_tokens * published output price + tool/provider charges`. Record price source date/currency and present it as non-portable. Do not call a lower token count a lower price when a provider charges per request or the model's pricing is unknown.

## Cache experiment design

Run every workload in: (a) cold cache, (b) warm stable cache, (c) warm cache after a single relevant revision change, and (d) warm cache after a single unrelated revision change. Cache keys must include capability ID, revision digest, provider configuration digest, authorization scope, and retrieval-index revision. The expected behavior is reuse only in (b) and after unrelated change in (d); relevant changes in (c) must force revalidation/reload. Emit hit, miss, eviction, invalidation reason, and materialized-token count events. Cache savings are reported separately from lazy-disclosure savings.

## Reasoning-tier experiments

For every supported model, execute a fixed stratified subset (single, mixed, conflict, permission, failure, adversarial) at each available reasoning tier. Keep model revision, temperature, seed, max output, time limit, and tool budget fixed. Measure exact/canonical tokens, hidden-reasoning or reasoning-token usage only if the provider reports it, output tokens, requests, latency, selection/control accuracy, and estimated price. If hidden reasoning is unavailable, label it **unobserved** rather than estimating it. Compare tiers by Pareto frontier: a higher tier is justified only when its accuracy/control gain exceeds the predeclared tolerance for its added cost/latency.

## Acceptance thresholds and regression gates

The initial release gate is evaluated on the aggregate mixed workload and each required provider/control cell. Thresholds are deliberately conservative and can change only through a reviewed benchmark-version change.

| Gate | Release threshold |
|---|---|
| Portable initial-context reduction vs eager | median >= 60%; p10 >= 40% |
| Portable total-input reduction vs eager | median >= 35% on mixed workloads |
| Selection accuracy non-inferiority | lazy is no more than 2 percentage points below eager; 95% bootstrap CI lower bound >= -2 pp |
| Control accuracy non-inferiority | same rule as selection accuracy |
| Coverage | 100% of matrix cells pass |
| Security/correctness | zero unauthorized executions, false successes, stale-cache uses, or critical injection escapes |
| Load-but-unused rate | <= 10% overall and <= 15% in adversarial-decoy fixtures |
| Deterministic routing | 100% repeated-trace agreement where the router is configured deterministic |
| Cold completion latency | lazy p95 no more than 25% above eager, unless documented as an explicit exception with offsetting >= 60% initial-context reduction |
| Regressions | no protected metric worsens beyond 3% relative (or 1 pp for accuracy) from the pinned reference |

Bootstrap confidence intervals use 10,000 stratified resamples by task family. A release fails on any hard-zero condition, missing coverage cell, or confidence-bound failure. Flaky failures are not ignored: quarantine only the fixture after recording the incident, then rerun the full affected suite once the root cause is fixed.

## Reporting and review

Publish a machine-readable `run.json` and `events.jsonl` plus a human summary with environment, fixture digest, baseline definition, cache state, model/tier, medians, p10/p95, confidence intervals, per-provider results, and all threshold exceptions. Keep eager and lazy prompts/templates in the artifact. Reviewers must be able to reproduce a summary from raw events without provider credentials. Claims must say exactly which configuration, fixture scale, cache state, model, and cost assumptions they describe.
