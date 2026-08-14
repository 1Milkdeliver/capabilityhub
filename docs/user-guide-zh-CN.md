# CapSift 中文使用说明书

> 介绍 CapSift 的安装、菜单、完整 CLI 和日常使用方式。你不需要预先理解 API、MCP 或 RAG，可以直接按照“3 分钟开始”操作。

> 以前安装过 CapabilityHub？请先看[改名迁移说明](migration-capabilityhub-to-capsift.md)。旧命令和项目状态仍然兼容。

## 1. CapSift 是做什么的

Codex 可以通过 Skill、MCP、CLI、API 和 RAG 获得额外能力。能力越来越多时，如果每次对话都把全部说明加载进上下文，会浪费 Token，也更容易选错工具。

CapSift 的作用是：

1. 先保存一份简短的能力清单；
2. 根据你当前想做的事搜索合适能力；
3. 只加载真正需要的那一项；
4. 对执行、权限、预算和审计进行统一控制。

你可以把它理解成 Codex 的“能力菜单和工具管理员”。

### CapSift 和 Codex 原生对话控制有什么区别

CapSift 是 Codex 外围的能力管理层，不是另一套对话系统，也不会接管当前任务。

| 范围 | Codex 原生对话控制 | CapSift |
|---|---|---|
| 对话和任务 | 管理消息历史、系统指令、当前任务和回答 | 不改写、不删除对话消息 |
| 模型和上下文窗口 | 决定使用的模型，并维护实际对话上下文 | 只减少 Skill、MCP、CLI、API 和 RAG 的说明加载量 |
| 原生命令 | 提供 `/help`、`/skills`、`/status`、`/mcp` 等功能 | 新增 `/helpme` 和 `/myskills`，不覆盖原生命令 |
| 能力管理 | 提供 Codex 自带的 Skill、MCP 和工具使用机制 | 统一清点、搜索、选择和按需加载五类能力 |
| 执行安全 | 提供原生审批、权限和沙箱 | 在 Provider 层再加权限、预算、审批和审计限制 |
| 上下文清理 | 由 Codex 客户端和模型上下文机制负责 | 只能移除或压缩 CapSift 自己管理的能力说明，不能删除聊天记录 |

正常使用时，你仍然直接和 Codex 对话。只有在任务需要外部能力时，CapSift 才先搜索简短目录，加载选中的能力说明，再交回 Codex 继续完成任务。

Codex 和 CapSift 的安全限制会同时生效。任何一层拒绝执行，操作都会停止；CapSift 不会绕过 Codex 的确认、权限或沙箱，也不会自动改变 Codex 使用的模型或推理强度。

## 2. 先选择使用方式

| 方式 | 适合谁 | 可以做什么 |
|---|---|---|
| Codex 插件（推荐） | 只想快速查找 Skill、使用中文菜单 | `/helpme`、`/myskills`、轻量搜索和查看 Skill 说明 |
| 完整 Python 核心 | 需要 Dashboard、五类 Inventory、Provider、预算、审计或执行控制 | 使用全部 `capsift` 命令和本地管理面板 |

初次使用时先装插件即可。菜单中如果某项标为“需要完整 CLI”，再决定是否安装 Python 核心。

## 3. 3 分钟开始

### 3.1 已经安装插件

本机已安装 CapSift 时：

1. 在 Codex 中新建一个任务；
2. 输入 `/helpme`；
3. 如果不是中文，输入 `/helpme language set zh-CN`；
4. 输入 `/myskills` 查看 Skill 菜单；
5. 直接输入你想做的事，例如：

```text
/myskills find 帮我读取并整理 PDF
```

### 3.2 安装插件

在终端中运行：

```powershell
codex plugin marketplace add 1Milkdeliver/capsift --ref main
codex plugin add capsift@capsift
```

然后关闭当前 Codex 任务并新建任务，再输入：

```text
/helpme
```

检查安装状态：

```powershell
codex plugin list
```

列表中应看到 `capsift@capsift`，状态为 `installed, enabled`。

插件自带一个只读 MCP 运行时，不要求全局安装 `capsift` 命令，也不需要另外执行 `codex mcp add`。

## 4. 最常用的操作

### 4.1 打开主菜单

```text
/helpme
```

主菜单只显示简短说明，不会为了显示菜单而加载全部 Skill 内容。

### 4.2 打开 Skill 菜单

```text
/myskills
```

你可以回复菜单数字、输入完整命令，或直接用自然语言描述任务。

### 4.3 按任务查找 Skill

```text
/myskills find 帮我分析 Excel 销售数据
```

也可以说：

```text
帮我找一个适合分析 Excel 销售数据的 Skill
```

CapSift 先返回简短候选项；选择后才读取对应 Skill 的详细说明。

### 4.4 查看当前清单

```text
/helpme inventory
```

Inventory 会按 Skill、MCP、CLI、API、RAG 五类显示数量。插件自带的轻量 Inventory 只统计插件包内能力；安装完整 Python 核心后，才能查看项目和本机配置的完整清单。

### 4.5 搜索所有能力

```text
/helpme search 读取网页并整理重点
```

搜索只返回简短卡片，不会立即执行能力。

### 4.6 返回和切换语言

```text
/helpme back
/helpme home
/myskills back
/helpme language
/helpme language set zh-CN
```

- `back`：返回上一层；
- `home`：返回 CapSift 主菜单；
- `language`：查看或修改语言。

这些命令不会替代 Codex 原生的 `/help`、`/skills`、`/status` 或 `/mcp`。

## 5. 菜单里的专业词是什么意思

| 专业词 | 小白解释 |
|---|---|
| Skill | 一份告诉 Codex“怎样完成某类任务”的说明书 |
| MCP | 让 Codex 按标准协议连接外部工具或服务的方式 |
| CLI | 在终端中运行的本地命令行工具 |
| API | 通过固定接口访问的网络或本地服务 |
| RAG | 从指定资料中搜索相关片段，再交给模型使用 |
| Inventory | 当前发现了哪些能力、各有多少、状态如何 |
| Provider | 某项能力实际来自哪里，例如插件、本地程序或服务 |
| Routing | 为什么选择或排除某项能力 |
| Loaded | 最近成功加载过哪些能力说明 |
| Lifecycle | 启用、停用或隔离能力；不会删除源文件 |
| Budget | Token、字节、加载次数和执行次数上限 |
| Approval | 高风险或不可逆操作执行前的明确批准 |
| Audit | 搜索、加载和执行的最小化安全记录 |

## 6. 安装完整 Python 核心

只有需要 Dashboard、完整 Inventory、Provider 配置、预算、审计或受控执行时才需要本节。

### 6.1 Windows 安装

需要 Python 3.11 或更高版本。在 PowerShell 中进入你准备保存项目的目录，然后运行：

```powershell
git clone https://github.com/1Milkdeliver/capsift.git
cd capsift
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install ".[mcp]"
```

如果 PowerShell 阻止激活虚拟环境，可以不激活，直接使用：

```powershell
.\.venv\Scripts\python.exe -m pip install ".[mcp]"
.\.venv\Scripts\capsift.exe health --pretty
```

### 6.2 macOS 或 Linux 安装

```bash
git clone https://github.com/1Milkdeliver/capsift.git
cd capsift
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[mcp]'
capsift health --pretty
```

### 6.3 安装后检查

```powershell
capsift health --pretty
capsift inventory --pretty
capsift providers --pretty
```

- `health`：检查项目路径、Dashboard 文件、配置解析和版本；
- `inventory`：扫描并统计五类能力；
- `providers`：按真实来源分组显示能力。

`health` 不扫描完整能力目录，因此适合先检查安装是否正常。

## 7. 打开 Dashboard 管理面板

在项目目录中运行：

```powershell
capsift dashboard --project-root .
```

终端会显示本机地址。把地址复制到浏览器打开。

Dashboard 可以：

- 通过“对话、能力库、管理、系统详情、本地使用说明书”五个页面快速定位功能；
- 在首页刷新并筛选当前或已归档的本机 Codex 对话；
- 查看五类能力数量和 Inventory 状态；
- 选择类型、自动分类、启用状态或 Provider 后立即筛选能力，不需要再点搜索；
- 只在查找特定名称时使用模糊搜索，并按预计 Token 或字母排序；
- 在彩色分类卡片中查看介绍、Provider、状态和醒目的预计 Token，点击“查看详情”打开完整元数据；
- 使用开关允许或阻止以后通过 CapSift 完整加载能力；
- 在右上角即时切换中文、英文或跟随系统；
- 查看 Provider、Routing、Loaded 和连接配置状态；
- 启用、停用或隔离能力，并在失败时自动恢复开关；
- 处理已经创建的审批请求；
- 查看脱敏后的审计、上下文和推理状态；
- 无需联网直接阅读面板内置的本地使用说明书。

Dashboard 默认只监听 `127.0.0.1`，也就是本机。刷新对话时，它会合并轻量的 `session_index.jsonl`，并在 `sessions` 与 `archived_sessions` 目录中进行有界文件发现，因此索引中遗漏的旧对话和归档对话也可以出现在列表中。只有选择某个对话后，才会以流式方式检查该对话最多 128 MiB 的工具调用包络；它不会读取或显示消息、回答和推理正文。界面会显示扫描字节数、工具包络数以及覆盖是否完整。

Dashboard 不会自动探测外部服务，也不会显示凭据、完整命令、Skill 正文或 Provider 输出。启用能力本身消耗 `0 Token`；卡片上的 Token 是以后完整加载说明时的估算值。停用能力只能阻止后续通过 CapSift 再次加载，不能删除已经进入 Codex 对话历史的内容。CapSift 现在能发现更多旧对话，但 Codex 在没有工具调用包络时静默注入的原生内容仍无法被可靠证明；对话视图会明确显示可验证证据和覆盖范围，不会猜测遗漏项。

## 8. 常用的 10 个命令

| 命令 | 用途 |
|---|---|
| `capsift health --pretty` | 检查安装和本地接线 |
| `capsift inventory --pretty` | 查看五类能力清单 |
| `capsift search "任务" --pretty` | 按任务搜索能力 |
| `capsift loaded --pretty` | 查看最近加载记录 |
| `capsift providers --pretty` | 查看能力来源 |
| `capsift routing "任务" --pretty` | 查看选择原因 |
| `capsift connections --pretty` | 查看配置状态，不执行联网探测 |
| `capsift budget-report --pretty` | 查看预算与已用额度 |
| `capsift audit --pretty` | 查看脱敏审计记录 |
| `capsift dashboard --project-root .` | 打开本地管理面板 |

查看某个命令的参数：

```powershell
capsift --help
capsift search --help
```

## 9. 一次完整的安全使用流程

假设你想让 Codex 使用一项能力：

1. 搜索：只看简短候选，不执行。

   ```powershell
   capsift search "整理项目文档" --pretty
   ```

2. 查看 Routing：确认为什么选中它。

   ```powershell
   capsift routing "整理项目文档" --pretty
   ```

3. 加载：使用搜索结果中的精确 revision，只读取所需说明。

   ```powershell
   capsift load REVISION --pretty
   ```

4. 执行：只有已配置的 CLI、API、RAG 或 MCP Provider 才能执行；普通 Skill 是只读说明，不会运行 Skill 旁边的脚本。

5. 审计：完成后查看脱敏记录。

   ```powershell
   capsift audit --pretty
   ```

不要把示例中的 `REVISION` 原样复制执行；请换成搜索结果返回的完整 revision。

## 10. 常见状态怎么理解

### `complete`

本次 Inventory 刷新完整成功。

### `partial`

部分能力因为重复、冲突、格式或路径问题被排除。查看非零的 `excluded_by_reason`，不要把排除项算作可用能力。

### `stale`

最新刷新失败，正在使用上一次完整快照。先运行：

```powershell
capsift health --pretty
capsift inventory --pretty
```

### `configured_not_probed`

表示已经配置，但尚未做连接测试，不代表服务已连通。

### `unavailable`

当前插件或运行时没有连接该功能。常见原因是只安装了轻量插件，没有安装完整 Python 核心；这不是 Codex 对话故障。

## 11. 常见问题

### 输入 `/helpme` 没反应

1. 确认 `codex plugin list` 中插件为 `installed, enabled`；
2. 安装或更新插件后新建 Codex 任务；
3. 确认输入的是 `/helpme`，不是原生 `/help`。

### `/helpme` 能用，但 Dashboard 或 Provider 不可用

这是正常的轻量模式。安装完整 Python 核心，并确认 `capsift health --pretty` 成功。

### `capsift` 命令不存在

你可能没有激活虚拟环境。Windows 可以直接运行：

```powershell
.\.venv\Scripts\capsift.exe health --pretty
```

### MCP 显示已配置，为什么不能用

“已配置”不等于“已连接”或“已认证”。先运行：

```powershell
capsift connections --pretty
```

只有你明确需要时才运行受限探测：

```powershell
capsift connections --probe --pretty
```

探测成功只证明 DNS、TCP 或 TLS 可达，不代表 MCP 工具调用一定健康。

### 会不会自动执行 Skill 里的脚本

不会。被发现的 Skill 只作为说明内容加载。只有项目显式配置并通过权限、预算和审批检查的 Provider 才能执行操作。

### 会不会把密码显示在对话或 Dashboard 中

设计上不会显示凭据内容。请仍然遵循一个原则：不要把密码、Token 或 API Key 直接粘贴到对话命令参数里。

## 12. 更新和卸载插件

更新 marketplace 和插件：

```powershell
codex plugin marketplace upgrade capsift
codex plugin add capsift@capsift
```

更新后新建 Codex 任务。

卸载插件：

```powershell
codex plugin remove capsift@capsift
codex plugin marketplace remove capsift
```

卸载插件不会删除你的 Skill、项目文件或 CapSift 本地状态。删除这些文件属于单独操作，请先确认备份和目标路径。

## 13. 下一步阅读

- [插件和 Dashboard 技术边界](ui-plugin.md)
- [Provider 项目配置](provider-configuration.md)
- [发布范围与安全边界](release-readiness.md)
- [GitHub 项目主页](https://github.com/1Milkdeliver/capsift)
- [正式发布版本](https://github.com/1Milkdeliver/capsift/releases)

如果你只记住三条命令，就记住：

```text
/helpme
/myskills
/helpme home
```
