# CapabilityHub

CapabilityHub is an open-source control plane for discovering, routing, lazily loading, executing, and auditing AI-agent capabilities across five surfaces:

- Agent Skills (`SKILL.md`)
- MCP tools and resources
- local command-line tools
- HTTP/OpenAPI APIs
- retrieval and knowledge sources (RAG)

The project is designed around one constraint: **capabilities should consume model context only when a task actually needs them**.

## Status

CapabilityHub is in its architecture and validation phase. The first release will expose a deliberately small meta-tool surface for capability search, inspection, loading, execution, and budget reporting. Claims about token savings will be backed by reproducible eager-versus-lazy benchmarks.

## Design principles

1. Metadata first; full definitions on demand.
2. One normalized manifest, provider-specific adapters.
3. Reuse upstream gateways and clients instead of duplicating them.
4. Explicit dependency, conflict, permission, and lifecycle policies.
5. Measured context cost, not marketing estimates.
6. Reasoning strength is routed by task risk and complexity.
7. Safe failure: one provider must not take down the control plane.

## Planned interface

```text
search_capabilities(query, filters, budget)
inspect_capability(id, disclosure_level)
execute_capability(id, arguments, authorization)
get_budget_report(scope)
```

See the evolving planning files and `docs/` for the requirements, architecture, upstream integration decisions, and validation methodology.

## License

MIT. Third-party integrations retain their own licenses and notices; see `THIRD_PARTY.md` as integrations are added.

