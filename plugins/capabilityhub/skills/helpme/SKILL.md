---
name: helpme
description: Show the progressive CapabilityHub help menu and handle its topics. Trigger when the user enters /helpme, /helpme with a topic, or asks for CapabilityHub status, dashboard, budgets, providers, MCP setup, benchmarks, or security help.
license: MIT
---

# CapabilityHub `/helpme`

Treat the first word after `/helpme` as a topic. Never discover or preload the capability
catalog merely to render help.

## Default menu

For bare `/helpme`, respond with this compact menu and nothing from the catalog:

```text
CapabilityHub
  /helpme status      运行状态与版本
  /helpme dashboard   打开本地只读面板
  /helpme budget      Token、加载与执行预算
  /helpme providers   Skill / MCP / CLI / API / RAG
  /helpme mcp         MCP 安装与三工具接入
  /helpme benchmark   查看节省量验证结果
  /helpme security    权限、审批与敏感数据边界

只加载你选择的主题。
```

## Topic routing

- `status`: Report only known plugin/runtime versions and connection state. Do not scan
  capabilities. If the runtime is unavailable, say so and provide the shortest install
  or verification command.
- `dashboard` or `open`: Explain that it is loopback-only and read-only. Start
  `capabilityhub dashboard` only when execution is available, then open the reported
  `http://127.0.0.1:<port>` URL. Otherwise show that one command.
- `budget` or `budgets`: Explain the four counters (`portable_tokens`, `bytes`, `loads`,
  `executions`) and show live values only when a connected runtime supplies them.
- `providers`: Show the five supported provider kinds and configured counts only. Do not
  load provider definitions, Skill bodies, schemas, or credentials.
- `mcp`: State that the Python package needs the `mcp` extra. Explain the three tools
  `capability.search`, `capability.load`, and `capability.execute`; do not silently
  install or launch the runtime.
- `benchmark`: Summarize the pinned structural-disclosure result and always state that it
  is oracle-routed, not evidence of semantic routing accuracy or billable-token savings.
- `security`: Summarize scoped references, budgets, permissions, approvals, audit
  minimization, and the pre-alpha boundary. Never reveal secrets or sensitive sections.
- Unknown topic: say `未知主题：<topic>` and render the default menu once.

Keep each topic response under 180 Chinese characters before commands or live values.
Load detailed state only after the user explicitly selects the relevant topic.
