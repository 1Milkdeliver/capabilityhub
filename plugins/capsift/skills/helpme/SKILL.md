---
name: helpme
description: Show and navigate the localized progressive CapSift help menu. Trigger for /helpme, /helpme language, /helpme with a menu topic, or requests about CapSift status, capabilities, consumption, runtime, security, evaluation, settings, language, and project information.
license: MIT
---

# CapSift `/helpme`

Render help from the static message catalogs in `references/locales/`. Do not ask a
model to translate catalog messages at runtime.

Before claiming an action is available, resolve its exact command in the plugin-root
`menu-map.json`. An absent command uses the explicit `*` unavailable mapping. MCP entries
call the bundled server, CLI entries require an installed full CapSift CLI, menu and
navigation entries are static, and unavailable entries must display their mapped reason.

## Language selection

Resolve the locale once in this order:

1. Locale explicitly supplied by the current command.
2. Current-task preference.
3. Project preference in `.capabilityhub/config.json`.
4. Global CapSift preference for the current operating system.
5. Codex or operating-system locale when the preference is `auto`.
6. `en` as the final fallback.

Supported locales are `zh-CN` and `en`. Normalize `zh`, `zh-Hans`, and Chinese locale
variants to `zh-CN`; normalize English variants to `en`. Never translate capability
names, commands, paths, error codes, or credential identifiers.

For `/helpme language set <locale>`, use task scope unless another scope was explicitly
selected. A `project` setting stores only the locale key in `.capabilityhub/config.json`.
A `global` setting stores only the locale key in the platform's CapSift config
directory. Never overwrite unrelated configuration keys. `preview` never persists.
When local CLI execution is available, read persistent settings with
`capsift language show --pretty` and save project/global choices with
`capsift language set <locale> --scope <project|global> --pretty`. Keep task scope
in the current interaction only. Report a failed CLI write instead of claiming success.

## Progressive routing

- Bare `/helpme`: read only the resolved catalog's `root` object and render every group
  and item in order. Professional terms remain visible; their parenthesized descriptions
  explain them in plain language.
- Keep `/helpme language` visible on the root menu. It opens language settings directly.
- `/helpme skills` and selection `1`: hand off to the `myskills` skill without loading
  the Skill inventory.
- Root shortcuts `inventory`, `search`, `loaded`, `providers`, `routing`, `lifecycle`,
  `mcp`, `status`, `dashboard`, `budget`, `security`, and `settings` go directly to that
  function. Numeric selections map to the visible root order only while this menu is the
  active interaction.
- For `/helpme inventory`, call `capability.search` with an empty query,
  `include_inventory: true`, `include_cards: false`, `limit: 1`, and a bounded output
  budget. Render `inventory.active_total`, all five values in
  `inventory.active_by_kind`, `inventory.generation`, and `inventory.status`; do not
  infer totals from cards or query-level `kind_counts`. If status is `partial`, show only
  non-zero `excluded_by_reason` counts. If it is `stale`, show
  `last_refresh_error_code` and say the last complete snapshot is being used.
- For `/helpme search <task>`, call `capability.search` for compact cards only. Show at
  most five results unless the user requests more, and do not load any result body.
- For `/helpme loaded`, use `capsift loaded --limit 20 --pretty`. Explain that
  this is a bounded list of recent successful loads reconstructed from redacted project
  audit, not a promise that full bodies remain resident in the chat context.
- For `/helpme providers`, use `capsift providers --pretty`. Show the real Provider
  name, discovered count, active count, and kinds. Do not replace Provider names with
  capability kinds.
- For `/helpme routing <task>`, use `capsift routing <task> --limit 5 --pretty`.
  Show rank and `match_reason`; state that the current method is deterministic lexical
  routing with zero model calls and does not expose hidden model reasoning.
- For `/helpme status`, report Inventory freshness, generation, and safe counts. Say
  clearly that configured or discovered MCP/API/RAG entries are not proof of a live
  network connection.
- For `/helpme dashboard`, give the local `capsift dashboard` command and explain
  that it shows live Inventory, local wiring, no-model compact search, project language,
  Providers, recent Loaded entries, Routing reasons, and enable/disable/quarantine
  activation controls, exact-intent approval decisions, resident Context metadata, and
  advisory Reasoning state. Explain that these controls do not delete or execute files. Do
  not claim that it opens a browser automatically or displays alerts.
- For `/helpme runtime health`, use `capsift health --pretty` when local CLI
  execution is available. This check must not scan the capability catalog; it checks
  only the project path, bundled Dashboard files, MCP SDK presence, local Codex config
  parsing, and package version.
- For `/helpme runtime connections`, use `capsift connections --pretty` when
  available. Explain `configured_not_probed` as "configured, but no connection test was
  made" and never turn a configured count into a reachable or healthy claim. Only when
  the user explicitly asks to test connections, use `capsift connections --probe
  --pretty`. Explain that this performs bounded DNS/TCP/TLS setup only: `reachable` is
  transport reachability, `tls_verified` is transport security, and application
  authentication and health remain unknown. It never invokes an MCP tool or API operation.
- If the user explicitly asks for the local API, provide `capsift http-serve
  --project-root <absolute path>`. Explain that it is numeric-loopback only, prints a
  one-process bearer token to the launching terminal, uses an immutable startup snapshot,
  and is not a remote or multi-user service. Never repeat that token into chat.
- For `/helpme lifecycle`, use `capsift lifecycle list --pretty`. A user may set a
  discovered coordinate to `enabled`, `disabled`, or `quarantined` through
  `capsift lifecycle set <coordinate> <state> --scope <project|global> --pretty`.
  This changes catalog activation only; it does not uninstall, delete, update, or run
  the capability. Require an exact coordinate and confirmation for quarantine.
- For staged update details, use `capsift updates list --pretty`. Explain the
  explicit `stage` → `health-pass`/`health-fail` → `activate` flow and retained rollback
  pointer. Never report a candidate healthy unless an operator supplied that result.
  Every one of those three transitions requires explicit local artifact bytes, publisher,
  registry, and a configured verifier; the default fails closed. Development mode permits
  unsigned local bytes only when explicitly selected and is not public-key trust.
- For `/helpme security audit`, use `capsift audit --limit 50 --pretty`. Explain
  that task identifiers are hashed and arguments, credentials, and provider output are
  not included. Do not claim tamper evidence, cross-machine history, or complete search
  history when the local MCP runtime was not connected.
- If `CAPABILITYHUB_AUDIT_KEY` is configured, offer `capsift secure-audit verify
  --pretty`; rotation and export are explicit operator actions. Never display or request
  the signing key value in chat.
- For `/helpme security approvals`, use `capsift approvals list --status pending
  --pretty`. Create a request with `capsift approvals request <revision> <operation>
  --arguments <json> --pretty`, then approve or deny its returned ID explicitly. Execute
  with `--approval-id <id>`. Never display stored argument digests, and never use the
  fixture-only `--approved` shortcut for a configured Provider.
- For `/helpme consumption context`, use `capsift context list --pretty`. Explain
  that this is metadata for sections already disclosed by `load`, not a copy of their
  bodies. `pin`, `unpin`, and `remove` take an exact returned key; `remove` only forgets
  residency metadata and never deletes the source capability.
- For `/helpme consumption reasoning`, use `capsift reasoning state <task-id>
  --pretty`. A new recommendation uses `reasoning recommend`; low is preferred unless
  risk/policy requires more or the caller explicitly records a failed attempt with new
  evidence. Explain that advice does not itself call a model or spend tokens.
- For `/helpme consumption budget`, use `capsift budget-report --pretty`. The
  report is restart-safe local accounting for bytes, portable tokens, reasoning tokens,
  loads, and executions; it is not an API-provider billing statement.
- For manifest portability, `validate` accepts bounded JSON or safe YAML, `export-manifest`
  emits canonical JSON, `migrate-manifest` previews a legacy migration, and `compatibility`
  checks the v1alpha1 feature handshake. Use `activation-lock export` to capture exact active
  revisions and `activation-lock verify <file>` to reject missing, extra, or drifted state.
  Unknown required security semantics must fail closed.
- `/helpme <topic>`: read only that topic from the resolved catalog.
- `/helpme language`: render the catalog's `language` menu.
- `/helpme back`: return to the parent of the last menu rendered in this interaction;
  when no parent is known, render the root menu.
- `/helpme home`: always render the root menu. Also recognize explicit localized intent
  such as `返回上一级`, `返回主菜单`, `back`, and `home` while a CapSift menu is active.
- After every non-root menu or result, append the resolved catalog's `navigation` items
  exactly once. Do not append them to the root menu.
- Unknown topic: render the localized unknown-topic message and the root menu once.
- Keep every displayed item in the form `command  (what it does)` or
  `command  （作用说明）`.
- Never claim that a planned management action is live. When a selected action is not
  connected to the current runtime, label it as unavailable and explain the current
  read-only or pre-alpha boundary in one sentence.
- Do not discover or preload capability definitions, Skill bodies, provider schemas,
  credentials, or live runtime state merely to render a menu.
- Fetch live state only after the user selects an item that requires it.
- Keep each response under 220 Chinese characters or 120 English words before commands
  and live values.

## Dynamic text

Use catalog templates for stable UI text. If the user explicitly asks to translate
unknown third-party prose, translate only that requested prose and label it as dynamic
translation. Never save model-generated translations into a locale catalog automatically.
