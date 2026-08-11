# Security policy

CapabilityHub mediates tools that may access files, networks, credentials, and external systems. Security reports should not be filed as public issues when they could expose a working exploit or secret.

## Reporting

Use GitHub's private vulnerability reporting feature for this repository. Include the affected revision, capability/provider type, deployment assumptions, reproduction steps, and impact. Do not include real credentials or private retrieved content.

## Supported versions

The project is pre-alpha. Only the latest `main` revision is currently supported for security fixes. No deployment should expose CapabilityHub directly to untrusted networks until an explicit stable release documents that profile.

## Current release posture

The current tree ships a pre-alpha local CLI, an experimental three-tool MCP server, and a loopback dashboard around the Python control core. The dashboard offers bounded metadata search plus project language and activation overrides; mutations require a random per-process CSRF header and same-origin browser requests. These interfaces are supported only for local experimentation; it does not ship a remote API listener, sandbox, secret broker, persistent control database, or production deployment profile. The CLI's execute command uses only an operator-supplied, side-effect-free static fixture. Real providers are opt-in embedding APIs. Dashboard responses must not contain credentials, full capability content, retrieved passages, raw provider output, commands, endpoint URLs, or absolute configuration paths.

Security invariants below describe the intended and tested core boundary; they are not a claim that a complete production deployment profile exists today.

## Security invariants

- Third-party Skill scripts are inactive during discovery.
- Model-facing execution accepts only revision-bound registered references.
- Search disclosure and execution permissions are denied by default.
- Approval-required execution accepts only a short-lived exact-intent reference bound to the actor, task, revision, operation, and normalized arguments.
- Inline input and output schemas are validated before and after provider invocation.
- Write-like operations require an idempotency key; uncertain provider outcomes are not automatically replayed.
- The local durable idempotency store records argument digests and outcome state, not raw arguments. Result persistence is disabled by default; completed duplicates are denied instead of re-executed.
- Secrets are references resolved outside model-visible payloads.
- Provider output is untrusted and budget bounded.
- External gateways, sandboxes, and credential stores remain separate integrations.
