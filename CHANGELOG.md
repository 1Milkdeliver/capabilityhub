# Changelog

All notable user-facing changes to CapSift are documented here.

## [0.2.0] - 2026-08-14

### Added

- CapSift naming while retaining the previous `capabilityhub` compatibility interfaces.
- A bilingual local Dashboard for conversations, capability discovery, filtering, lifecycle
  controls, approvals, context metadata, Providers, routing, budgets, and audit status.
- Unified, progressively disclosed Skill, MCP, CLI, API, and RAG capability manifests.
- Lightweight application-update checks with a 24-hour cache and SHA-256 verified downloads.
- Authenticated local and reference remote control paths, scoped state, provider supervision,
  release certification, and platform confinement gates.

### Changed

- Capability instructions are searched and loaded on demand instead of being exposed eagerly.
- The Codex plugin provides `/helpme` and `/myskills` without overriding native commands.

### Security

- Release assets are built once, tested across mandatory gates, bound to their source revision,
  signed as one certification subject, and published only after every required gate succeeds.

[0.2.0]: https://github.com/1Milkdeliver/capsift/releases/tag/v0.2.0
