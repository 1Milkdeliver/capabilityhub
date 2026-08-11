# Security policy

CapabilityHub mediates tools that may access files, networks, credentials, and external systems. Security reports should not be filed as public issues when they could expose a working exploit or secret.

## Reporting

Use GitHub's private vulnerability reporting feature for this repository. Include the affected revision, capability/provider type, deployment assumptions, reproduction steps, and impact. Do not include real credentials or private retrieved content.

## Supported versions

The project is pre-alpha. Only the latest `main` revision is currently supported for security fixes. No deployment should expose CapabilityHub directly to untrusted networks until an explicit stable release documents that profile.

## Current release posture

The current tree is a local Python control core and a read-only local dashboard component. It does not ship a supported CLI, MCP server, remote API listener, sandbox, secret broker, or production provider adapter. Treat every provider integration and dashboard snapshot as application-owned security work. In particular, dashboard snapshots must not contain credentials, full capability content, retrieved passages, or raw provider output.

Security invariants below describe the intended and tested core boundary; they are not a claim that a complete production deployment profile exists today.

## Security invariants

- Third-party Skill scripts are inactive during discovery.
- Model-facing execution accepts only revision-bound registered references.
- Permissions are denied by default.
- Secrets are references resolved outside model-visible payloads.
- Provider output is untrusted and budget bounded.
- External gateways, sandboxes, and credential stores remain separate integrations.
