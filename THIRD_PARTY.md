# Third-party software and integration notices

This repository does not currently vendor, redistribute, or compile the source code of the projects listed below. They are researched as optional process-level integrations or format/protocol compatibility targets. Consequently, this file is a decision and attribution record, not a complete shipped-dependency notice. Before shipping any optional adapter or dependency, generate an SBOM and update this file with the exact name, version/digest, download source, and transitive notices.

## Approved boundary

Use upstream software through documented protocols, a separately installed executable/container, or a package isolated to an optional adapter extra. Do not copy source, bundled assets, Docker catalog entries, skills, documentation, or configuration from an upstream without a separate legal/provenance review.

| Project | Intended relationship | License | Notice obligation if redistributed or source is copied | Current disposition |
|---|---|---|---|---|
| [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) | `SKILL.md`/MCP compatibility; optional external application integration | MIT | Include copyright and MIT license text in substantial copies | No code dependency or vendoring planned |
| [IBM mcp-context-forge](https://github.com/IBM/mcp-context-forge) | Optional separately deployed gateway | Apache-2.0 | Preserve license and notices; state material changes; include any `NOTICE` file if supplied | Do not embed or fork; deploy separately only |
| [Docker mcp-gateway](https://github.com/docker/mcp-gateway) | Optional separately deployed local MCP runtime | MIT | Include copyright and MIT license text in substantial copies | Do not embed or fork; operator supplies runtime |
| [Apify mcpc](https://github.com/apify/mcp-cli) | Optional separately installed MCP CLI/client proxy | Apache-2.0 | Preserve license and notices; state material changes; include any `NOTICE` file if supplied | Do not bundle; use pinned operator-installed executable/package |
| [voicetreelab/lazy-mcp](https://github.com/voicetreelab/lazy-mcp) | Optional MCP upstream / interface reference | MIT | Include copyright and MIT license text in substantial copies | No dependency planned; do not copy proxy code |
| [Datalayer agent-skills](https://github.com/datalayer/agent-skills) | Optional Python adapter extra / format reference | BSD-3-Clause | Retain copyright, conditions, disclaimer; do not use contributor names for endorsement | No core dependency; optional adapter needs exact package notice |
| [AWS Labs CLI Agent Orchestrator](https://github.com/awslabs/cli-agent-orchestrator) | Optional separately deployed orchestration target | Apache-2.0 | Preserve license and notices; state material changes; include any `NOTICE` file if supplied | Do not bundle or reimplement its runtime |

## Shipped Python dependencies added by CapSift

| Package | Purpose | License | Source |
|---|---|---|---|
| `jsonschema` | Validate inline Draft 2020-12 capability input and output contracts | MIT | [python-jsonschema/jsonschema](https://github.com/python-jsonschema/jsonschema) |
| `types-jsonschema` (development only) | Static type information for the JSON Schema integration | Apache-2.0 | [python/typeshed](https://github.com/python/typeshed) |
| `mcp` and `mcp-types` (optional) | Official MCP server/client transports and wire models | MIT | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) |

Release artifacts must include the complete transitive dependency and license inventory produced from the locked build environment; this table records the direct dependency decision only.

## Protocols and content formats

- [Model Context Protocol](https://modelcontextprotocol.io/specification/) and the [Agent Skills specification](https://agentskills.io/specification) are interoperability specifications, not a grant to copy a particular implementation's source or branded assets. Verify their current licensing/terms at the point of distribution.
- A user-supplied Skill package, MCP server, Docker image, or catalog is third-party content. Store its provenance, declared license, digest/version, and trust decision. Do not redistribute it merely because the Hub can discover it.

## Release checklist

Before a release enables an upstream integration:

1. Pin the exact released package version or container digest; do not depend on a moving branch/tag.
2. Scan the direct and transitive dependency graph and generate an SBOM.
3. Add the exact artifact, license text, copyright, notices, source URL, and modification statement (if applicable) here and in distributed notices.
4. Review trademark/branding use separately; permissive code licenses do not grant endorsement rights.
5. Verify the adapter does not package secrets, upstream configuration, sample credentials, or user-supplied skill content.
6. Re-run contract, denial-path, and provenance tests after every upgrade.

Repository metadata and licenses were inspected on 2026-08-11 from the primary source links above. License metadata is not a substitute for legal review.
