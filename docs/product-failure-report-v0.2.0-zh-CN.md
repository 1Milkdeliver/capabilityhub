# CapSift v0.2.0 产品失败复盘

> 状态：最终结论  
> 日期：2026-08-14  
> 范围：CapSift 作为“面向 Codex 初阶用户的任务级能力查看与开关插件”这一产品方向

## 摘要

CapSift v0.2.0 在工程上交付了一个可运行、可测试、可发布的渐进式能力控制内核，但没有充分解决目标用户的核心问题。因此，本报告将当前产品方向判定为**产品力不足**，而不是工程实现失败。

最初希望用户可以查看每个 Codex 任务加载了哪些 Skills、MCP、CLI、API 和 RAG，随时开关它们，检查连接与授权状态，并减少不必要的上下文和 Token 消耗。实际交付只能完整控制经过 CapSift 路由的能力，不能权威读取、卸载或阻止 Codex 原生注入及绕过 CapSift 的能力。用户仍需理解额外的 CLI、Provider、路由和控制概念，产品增加的操作与认知成本可能高于它节省的成本。

**English summary:** CapSift v0.2.0 delivered a substantial progressive-disclosure control-plane implementation, but it did not establish a strong end-user product. It cannot authoritatively observe or unload capabilities already injected by Codex, and it cannot govern native capability paths that bypass CapSift. The project should be treated as a research/reference implementation unless the host exposes task-level capability and context-control APIs.

## 1. 最初要解决的问题

目标用户是 Codex 初阶使用者。预期的核心体验是：

1. 自动识别本机和当前任务可用的能力；
2. 明确显示当前任务已经加载、披露和调用的能力；
3. 用户通过简单开关决定以后允许使用什么；
4. 检查 CLI、MCP、API 的安装、连接、登录和权限状态；
5. 未选择的能力不进入任务上下文；
6. 显示实际或可验证的 Token 变化；
7. 无需理解 Provider、Revision、Routing、Approval 等底层概念。

这个目标隐含了一个关键前提：插件能够进入 Codex 的权威能力加载路径，并获得任务级能力和上下文事件。该前提在开发开始时没有被优先验证。

## 2. 实际交付了什么

v0.2.0 的工程实现并非空壳，主要包括：

- Skill、MCP、CLI、API、RAG 的统一 manifest 和不可变 revision 模型；
- `search → load → execute` 渐进披露流程；
- 结果 Token/字节上限、权限过滤、预算、审批、审计和生命周期状态；
- CLI、Python、MCP、loopback HTTP、Dashboard 和可选 mTLS 参考接口；
- 受限 Provider worker、CLI/HTTP/MCP/RAG 适配器和本地持久状态；
- Codex 任务索引与工具调用 envelope 的隐私有界扫描；
- 跨平台测试、浏览器测试、百万条 RAG 基准和签名发布认证。

这些能力证明了“按需披露能力定义”在技术上可行，也为未来的宿主集成保留了可复用内核。

## 3. 没有解决的核心用户问题

### 3.1 无法权威知道当前任务加载了什么

任务扫描器只观察 Codex 任务文件中的明确工具调用 envelope。它不读取消息、回复和推理正文，也无法证明系统提示或宿主内部静默注入了什么。当前实现因此只能报告“观察到的调用”，不能报告“当前模型上下文的完整组成”。

证据：[Codex task scanner](../src/capabilityhub/codex_sessions.py) 明确返回 `observed_calls_only`，并只解析 `custom_tool_call` 和 `function_call`。

### 3.2 开关不能从当前任务卸载内容

能力状态可以阻止 CapSift 以后再选择或执行某项能力，但不能删除已经进入模型上下文的 Skill 内容，也不能让模型忘记已读取的信息。彻底应用新配置通常需要创建一个干净的新任务。

证据：[local lifecycle state](../src/capabilityhub/state.py) 保存的是以后使用的 `enabled`、`disabled`、`quarantined` 偏好；它不是模型上下文删除接口。

### 3.3 无法控制绕过 CapSift 的原生路径

只有客户端把发现和执行路由到 CapSift 时，预算、权限和开关才是强制控制。如果 Codex 仍直接暴露同一个 Skill 或 MCP，CapSift 无法阻止该旁路。

证据：[product scope](scope.md) 明确说明，要求强制控制的部署必须移除直接访问，或由上游网关执行相同策略。

### 3.4 轻量插件不等于完整本地控制面

插件自带 MCP 可以独立启动，但其轻量目录只包含插件包内的 `helpme` 和 `myskills`。完整的本机 Inventory、Provider 执行和管理动作仍依赖单独安装的 Python 核心。用户看到统一入口，不代表插件已经接管本机五类能力。

证据：[bundled plugin runtime](../plugins/capsift/runtime/capsift_mcp.cjs) 只扫描插件自身的 `skills` 目录；[menu map](../plugins/capsift/menu-map.json) 仍将多项能力映射到外部 CLI 或明确标记为 unavailable。

### 3.5 Token 证据不能直接代表日常节省

发布认证中的真实 Codex 基准证明了固定能力路由任务下的输入差异，但它不是普通用户一周真实工作的随机样本。v0.2.0 发布实验中 lazy 输入显著少于 eager，但 eager 为 0/30 正确、lazy 为 30/30 正确。这个极端结果可能反映完整目录造成严重干扰，也可能反映 fixture、提示格式或评分设计更适合 lazy。

发布 artifact 只保存每个观察的正确/错误和分摊 Token，没有保存模型实际选择的 revision，因此无法充分解释 eager 全部失败的原因。当前证据支持“固定基准中按需披露更省输入”，但不支持“普通用户日常一定节省同等比例”。

证据：[benchmark limitations](../benchmarks/README.md) 已声明固定 fixture 不能代替更广泛的真实 Provider、模型、延迟和对抗矩阵。

## 4. 产品力不足的根因

### 4.1 先实现控制面，后验证宿主权限

项目优先建设了 revision、审批、供应链、租户隔离、远程控制、沙箱和发布认证，却没有先证明 Codex 是否提供以下必要接口：

- 当前任务的权威能力清单；
- 任务级 Skill/MCP allowlist；
- 已披露内容和实际上下文占用；
- 从当前任务卸载能力；
- 以插件方式创建应用新配置的干净任务。

核心前提未成立后，继续增加控制功能无法补上宿主能力缺口。

### 4.2 工程完成度被误当成产品完成度

[completion matrix](completion-matrix.md) 回答的是内部需求是否存在实现和验证路径。它不回答以下问题：

- 初阶用户是否理解产品；
- 用户是否愿意改变原有 Codex 工作流；
- 产品是否减少了总操作时间；
- 用户能否确认开关真正影响了下一次任务；
- 用户是否持续使用。

36/36 工程矩阵和绿色 CI 都不能替代产品需求验证。

### 4.3 目标用户与界面复杂度不匹配

初阶用户希望得到“现在是否可用、是否安全、该怎么修”的直接答案。当前产品暴露了 Inventory、Provider、Routing、Revision、Lifecycle、Approval、Audit、Supply Chain 等大量专业概念。即使这些信息正确，用户仍需要先学习一套新的控制系统。

### 4.4 产品价值依赖用户主动绕行

用户只有通过 CapSift 的搜索、加载和执行路径，才能得到完整价值。这会增加步骤并改变原生习惯。如果原生 Codex 路径更短，用户通常会绕过控制面，进一步削弱数据完整性和开关可信度。

### 4.5 缺少开发前的真实用户门槛

项目没有先用低成本原型验证：

- 至少 10 名初阶用户是否有强烈的任务级能力管理需求；
- 用户能否在三分钟内理解“可用、已披露、已调用、仍在上下文”的区别；
- 用户是否愿意安装独立本地运行时；
- 用户是否比使用原生配置更快解决问题。

因此，大量工程投入发生在产品需求强度尚未确认之前。

## 5. 哪些成果仍然有价值

本次产品方向失败不等于全部代码无价值。以下资产可以保留：

- 有界渐进披露、引用绑定和预算机制；
- 统一 capability manifest 和 Provider 接口；
- CLI/MCP/API/RAG 的配置与健康诊断基础；
- 权限、审批、审计和本地状态实现；
- Codex 任务文件的隐私有界观察器；
- 可复现的发布认证和跨平台测试框架；
- Dashboard 的能力目录、筛选和诊断组件。

它们更适合作为开发者参考实现、未来宿主 API 的集成基础，或者被拆分成更小的环境诊断工具。

## 6. 决策

从 v0.2.0 起，不再把 CapSift 描述为已经能够统一查看、卸载和控制 Codex 每个任务全部能力的成品。

建议冻结原产品方向：

- 不继续增加 Dashboard 菜单和控制模块；
- 只接受必要的安全、兼容性和文档修复；
- 将仓库保留为实验性参考实现；
- 若继续开发，优先收缩为 Skills/MCP/CLI 安装、连接、登录和权限诊断器；
- 未通过真实用户验证前，不再扩大产品承诺。

## 7. 重新启动原方向的必要条件

只有至少满足以下条件，才值得恢复“任务级能力控制插件”方向：

1. Codex 提供权威任务能力加载事件或查询 API；
2. 支持任务级 Skill/MCP allowlist，且不能被同一客户端旁路；
3. 支持创建应用新能力配置的干净任务；
4. 能获得实际输入 Token 或可信的上下文占用数据；
5. 至少 10 名目标用户无需人工指导完成“查看 → 关闭 → 新任务生效 → 验证”的闭环；
6. 一周真实任务 A/B 显示任务成功率不下降、总完成时间不增加，并且输入消耗显著下降。

如果宿主长期不提供这些接口，CapSift 应保持为开发者能力网关或环境诊断工具，而不是继续承诺管理模型客户端的完整上下文。

## 8. 经验总结

1. 在开发控制面之前，先验证是否拥有真正的控制点。
2. “能够记录”不等于“能够控制”，“阻止以后使用”不等于“从当前上下文卸载”。
3. 测试证明实现符合设计，用户测试才证明设计值得实现。
4. 安全和架构深度不能弥补核心工作流未闭环。
5. 对初阶用户，默认自动化和直接诊断比完整管理面板更重要。
6. 发布成功是工程里程碑，不是产品市场匹配证明。

## 最终结论

CapSift v0.2.0 是一次有工程成果的产品探索，但没有形成足够强、足够简单、能够独立兑现的终端用户价值。最合理的后续行动不是继续堆功能，而是冻结原方向、保留可复用技术，并等待宿主接口或新的真实用户证据。
