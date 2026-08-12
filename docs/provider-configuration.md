# Project provider configuration

CapSift keeps discovery inert by default. A project manifest becomes executable only when
`spec.driver.name` is one of four built-in adapters and its bounded `config` is valid. Configuration
lives in the same reviewed JSON manifest under `.capabilityhub/manifests/`; there is no second
capability definition to keep in sync.

Supported names are `cli-process`, `http-api`, `local-rag`, and `mcp-stdio`. They map respectively to
an absolute executable with fixed argument templates, a fixed-origin JSON endpoint, a project-contained
text root, and an absolute-command MCP stdio server. Invalid driver configuration is counted in
Inventory and is never wired to the execution service.

## CLI

```json
"driver": {
  "name": "cli-process",
  "config": {
    "executable": "C:/absolute/path/tool.exe",
    "operations": {"lookup": {"argv": ["lookup", "{query}"], "output": "json"}},
    "environmentFrom": {"SERVICE_TOKEN": "MY_SERVICE_TOKEN"}
  }
}
```

Placeholders occupy complete arguments. No shell is used. The child receives only environment values
explicitly named by `environmentFrom`; secrets do not belong in manifests.

## HTTP API

```json
"driver": {
  "name": "http-api",
  "config": {
    "baseUrl": "https://api.example.test",
    "operations": {
      "lookup": {"method": "POST", "path": "/v1/items/{item_id}", "body": ["question"]}
    },
    "headerEnvironment": {"Authorization": "EXAMPLE_AUTH_HEADER"}
  }
}
```

Cleartext HTTP is limited to loopback. Redirects are denied, URLs are encoded, response bytes are
capped before JSON parsing, and header values come from named environment variables.

## Local RAG

```json
"driver": {
  "name": "local-rag",
  "config": {"root": "docs", "operation": "retrieve", "suffixes": [".md", ".txt"]}
}
```

The root is relative to and must stay inside the project. Retrieval accepts `query` and optional
`top_k` (1-20), and returns bounded snippets with relative citations.

## MCP stdio

```json
"driver": {
  "name": "mcp-stdio",
  "config": {
    "command": "C:/absolute/path/server.exe",
    "args": ["serve"],
    "tools": {"search": "upstream_search"},
    "environmentFrom": {"UPSTREAM_TOKEN": "MY_UPSTREAM_TOKEN"}
  }
}
```

Only mapped tools can be called. The adapter verifies advertised tools and enforces deadline and output
limits through the official MCP SDK.

## Execute

```bash
capsift search "task words" --project-root /absolute/project
capsift load REVISION --operation OPERATION --project-root /absolute/project
capsift execute REVISION OPERATION --arguments '{"key":"value"}' --project-root /absolute/project
```

Add grants or approval flags only when required. `--fixture-output` remains available solely for
deterministic tests. Normal execution is revision-bound, budgeted, audited, and supports persistent
conservative idempotency.
