---
name: myskills
description: Open and control the localized My Skills menu. Trigger for /myskills, explicit requests to find or list the user's Skills, questions about loaded Skills, Skill providers or routing, and explicit requests to enable, pause, update, quarantine, or check a named Skill. Do not trigger for generic uses of the word skill or native Codex commands.
license: MIT
---

# `/myskills`

Use the language-selection rules from the sibling `helpme` skill, then render the matching
static catalog in `references/locales/`. Never translate stable menu text at runtime.

## Interaction

- Bare `/myskills`: render both visible groups and all items. Provider, Routing,
  Inventory, Lifecycle, Risks, and Conflicts are never hidden.
- Accept the visible number only while the My Skills menu is the active interaction.
- Accept exact commands and aliases: `find`, `list`/`ls`, `loaded`/`using`, `show`/`info`,
  `providers`, `routing`/`why`, `lifecycle`, `risks`, `conflicts`/`check`.
- Recognize explicit natural-language intents such as “帮我找一个处理 PDF 的 Skill”,
  “我加载了哪些 Skill”, or “为什么选择 documents”. Use low reasoning for status,
  listing, and direct commands; use medium reasoning only when task-to-Skill matching is
  ambiguous.
- Never intercept native `/help`, `/skills`, `/status`, `/model`, or `/mcp` commands.
- Do not trigger on source code, documentation, or casual text that merely contains the
  word “skill”.

## Safety and context

- Search only compact Skill metadata first. Return at most five candidates unless the
  user requests more.
- Load a Skill body only after an explicit selection.
- Skills are load-only in CapabilityHub; do not offer a `run` command.
- Read-only actions execute directly. An explicit `enable` or `disable` command may act
  on one exact Skill and must provide an undo hint. Ambiguous mutations return choices
  without changing state.
- Never delete a Skill. Use pause or quarantine instead.
- Label unavailable management actions honestly when the runtime is not connected.
- Never reveal credentials, hidden reasoning, or full unselected Skill content.
