---
name: helpme
description: Show and navigate the localized progressive CapabilityHub help menu. Trigger for /helpme, /helpme language, /helpme with a menu topic, or requests about CapabilityHub status, capabilities, consumption, runtime, security, evaluation, settings, language, and project information.
license: MIT
---

# CapabilityHub `/helpme`

Render help from the static message catalogs in `references/locales/`. Do not ask a
model to translate catalog messages at runtime.

## Language selection

Resolve the locale once in this order:

1. Locale explicitly supplied by the current command.
2. Current-task preference.
3. Project preference in `.capabilityhub/config.json`.
4. Global CapabilityHub preference for the current operating system.
5. Codex or operating-system locale when the preference is `auto`.
6. `en` as the final fallback.

Supported locales are `zh-CN` and `en`. Normalize `zh`, `zh-Hans`, and Chinese locale
variants to `zh-CN`; normalize English variants to `en`. Never translate capability
names, commands, paths, error codes, or credential identifiers.

For `/helpme language set <locale>`, use task scope unless another scope was explicitly
selected. A `project` setting stores only the locale key in `.capabilityhub/config.json`.
A `global` setting stores only the locale key in the platform's CapabilityHub config
directory. Never overwrite unrelated configuration keys. `preview` never persists.

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
- For `/helpme inventory`, call `capability.search` with an empty query, `limit: 1`,
  and a bounded output budget. Render `total_matches` and all five `kind_counts`; do not
  infer totals from the returned cards. Label the result as the connected runtime's
  active snapshot.
- `/helpme <topic>`: read only that topic from the resolved catalog.
- `/helpme language`: render the catalog's `language` menu.
- `/helpme back`: return to the parent of the last menu rendered in this interaction;
  when no parent is known, render the root menu.
- `/helpme home`: always render the root menu. Also recognize explicit localized intent
  such as `返回上一级`, `返回主菜单`, `back`, and `home` while a CapabilityHub menu is active.
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
