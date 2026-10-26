# Aiscribe 的 DSH 插件包（MCP 连接型）

这是一个 **配置型 Cordis bundle**：不含 JS 宿主代码，只把 Aiscribe 自带的 MCP server
挂进当前 DSH profile。装上之后，agent 就能像调用原生工具一样使用：

| 工具名（DSH 侧） | 作用 |
|---|---|
| `mcp__aireport__list_sources` | 列出本机 16 个数据源与各自会话数 |
| `mcp__aireport__get_conversations` | 按项目取某天/某周/某区间的逐条对话（报告素材） |
| `mcp__aireport__generate_report` | 本机直接生成 md / docx / html 报告 |
| `mcp__aireport__save_report` | 把 AI 归纳好的正文落盘 |
| `mcp__aireport__list_templates` / `get_format_spec` / `save_template` | 格式克隆（学你以前的日报排版） |

数据全程在本机，MCP 只走 stdio，不联网。

## 文件

```
package.json        bundle 清单：dsh.bundle.patch 指向下面的 patch
cordis.patch.yml    唯一有实质内容的文件：插入一条 @deepseek-ai/dsh-mcp-client 记录
locale/zh.json      插件管理页显示的中文标题与描述
locale/en.json      同上（英文）
icon.svg            插件图标（原创，1 KB）
```

## 安装

### 方式一：Web 侧边栏（推荐）

DSH 侧边栏 → **插件**页 → **安装组合包** → 选择本目录：

```
E:\DSH\Aiscribe\dsh-plugin
```

装完默认启用，重启会话后生效。

### 方式二：Creator 模式下的 `plugin_manager` 工具

`plugin_manager` 只在 **Creator** 预设里启用（其他预设默认禁用）。切到 Creator 预设后，让 agent 执行：

```
plugin_manager(action="install_bundle", target="E:\\DSH\\Aiscribe\\dsh-plugin")
```

两种方式写入的是同一个 profile 层栈，改动跨会话持久。

## 安装前要改的三处

打开 `cordis.patch.yml`，按你的机器确认：

```yaml
command: D:\Anaconda\python.exe    # 你的 Python 解释器
cwd: E:\DSH\Aiscribe                # Aiscribe 仓库根目录
```

`args` 与 `env` 一般不用动。

## 验证

装好后**新开一个会话**（旧会话的工具列表在启动时就固定了），然后：

1. 问一句「用 aireport 列一下本机有哪些数据源」——agent 应该调用 `mcp__aireport__list_sources`
2. 或直接在插件页看这条记录是否 `enabled` 且无 `error`

如果启动失败，先手工确认 MCP server 本身能跑：

```powershell
cd E:\DSH\Aiscribe
python -m ai_report mcp --stdio
# 应看到 stderr 一行「[MCP] stdio 模式已启动」，然后等 JSON-RPC 输入
```

## 为什么不用「普通脚本」或「Skill」

| 做法 | 能做什么 | 代价 |
|---|---|---|
| **MCP 连接（本包）** | 把 Aiscribe 的 7 个能力变成 agent 工具 | 只需 4 个小文件，无 JS |
| Skill | 给 agent 一段操作说明（怎么用 CLI 生成报告） | 仍要 agent 自己跑命令，没有结构化工具 |
| UI 插件 | 在侧边栏加面板、斜杠命令、自定义渲染 | 要写 Host + Client 两半、注册 slot，改一次 UI 要重装 |

Aiscribe 已经有 MCP server，所以先用最便宜的那层；等确实需要在 DSH 界面里嵌 Aiscribe 的浏览页
（`/browse`）时，再加 Client 半侧。
