# 从 CapabilityHub 升级到 CapSift

项目从 `0.2.0` 起使用名称 **CapSift**。改名的目的是让初次使用 Codex
的人更容易记住它：CapSift 会先筛选能力，只在需要时加载详细内容。

这是一段渐进迁移期。改名不会删除项目状态，也不会强迫现有 Python
集成立即修改代码。

## 新名称

| 项目 | 旧名称 | 新名称 |
|---|---|---|
| 产品 | CapabilityHub | CapSift |
| GitHub 仓库 | `1Milkdeliver/capabilityhub` | `1Milkdeliver/capsift` |
| Codex 插件 | `capabilityhub@capabilityhub` | `capsift@capsift` |
| 首选命令 | `capabilityhub` | `capsift` |
| Python 分发包 | `capabilityhub` | `capsift` |

## 仍然兼容的内容

- 旧的 `capabilityhub` 命令仍指向同一个 CLI；
- 旧的 `import capabilityhub` 仍然可用；
- `.capabilityhub/` 项目状态目录不改名，避免丢失设置、预算和审计状态；
- `capabilityhub.io/v1alpha1` 协议标识不改名，避免现有 manifest 失效；
- `CAPABILITYHUB_*` 环境变量暂时保留。

旧名称处于建议迁移状态，目前没有强制删除日期。新文档和示例只使用
`capsift`。

## 更新 Codex 插件

先移除旧插件，再安装新插件：

```powershell
codex plugin remove capabilityhub@capabilityhub
codex plugin marketplace remove capabilityhub
codex plugin marketplace add 1Milkdeliver/capsift --ref main
codex plugin add capsift@capsift
```

然后新建一个 Codex 任务，输入：

```text
/helpme
```

移除旧插件不会删除你的 Skill 或项目文件。

## 更新完整 Python 版本

在新的仓库目录中安装：

```powershell
git clone https://github.com/1Milkdeliver/capsift.git
cd capsift
python -m pip install ".[mcp]"
capsift health --pretty
```

已有脚本可以暂时继续使用 `capabilityhub`。修改脚本时，把命令逐步替换为
`capsift` 即可，无需一次改完。

## 回退

如果新插件在当前 Codex 版本中无法启动，可以移除 `capsift@capsift`，继续
使用已缓存的旧插件版本。Python 侧的旧命令和导入路径保持兼容，因此不需要
回滚 `.capabilityhub/` 状态目录。
