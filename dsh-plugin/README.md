# Aiscribe — 把 AI 会话变成日报 / 周报

> 你在 CodeBuddy、TRAE、Codex、dsh、Claude Code、Cursor……之间来回切换，
> 写周报时却想不起这周到底做了什么。Aiscribe 读取这些工具留在本机的会话记录，
> 按项目归类，用一句话生成一份能直接交的日报 / 周报。

- **16 种数据源**：覆盖主流 AI CLI、IDE 与桌面端
- **本机优先**：读取、筛选、排版都在本机完成；不读取云端会话，也没有遥测
- **不装 Python**：包内自带 Windows x64 sidecar（PyInstaller 打包），装完即用
- **md / docx / html**：可直接提交、流转或分享
- **格式克隆**：上传你以前的日报样例，让报告沿用你的排版习惯

> **English at a glance** — Aiscribe is a local-first session aggregator for DSH.
> It reads AI-tool session history already stored on your machine, groups it by
> project, and generates daily/weekly reports. A bundled Windows x64 sidecar is
> included, so no Python installation is required. The MCP server is local and
> read-only unless you explicitly ask it to write a report.

---

## 安装

**要求**：DSH 0.2.x，Windows x64。当前版本 `0.1.0` 只随包提供 Windows x64
运行时；macOS / Linux 版本会以独立平台包发布。

| 项目 | 状态 |
|---|---|
| Harness | DSH `>=0.2.0-0 <0.3.0`（`engines.dsh`） |
| Profile | 任意能加载 bundle 的 profile；`web` 已作为主要目标验证 |
| 操作系统 | Windows x64（`0.1.0`） |
| Python | 不需要（sidecar 已内置） |
| 网络 | 默认不联网；只有你显式开启 AI 归纳时才访问所配 LLM 端点 |
| 外部服务 | 无内置账号、无遥测、无云端同步 |

### 方式一：插件市场（推荐）

在 DSH 的插件市场搜索 **Aiscribe**（包名 `dsh-aireport`），点击安装，然后**新开一个会话**。

### 方式二：命令行

```bash
dsh plugin --profile web add dsh-aireport
```

`web` 是常见的 profile 名；如果你的 profile 不叫 `web`，换成 `dsh --profile` 实际使用的名字。
安装完成后新开会话。旧会话的工具列表在启动时已经固定，不会热更新。

### 验证

对新会话说：

> 用 aireport 列一下本机有哪些 AI 数据源。

Agent 应调用 `mcp__aireport__list_sources`，返回每个数据源的名称、是否可用、会话数。

---

## 一句话能做什么

| 你说 | Agent 会做 |
|---|---|
| 「看看今天有哪些 AI 会话」 | `get_conversations`，默认只取摘要，先摸底 |
| 「生成今天的日报，按项目分组」 | `generate_report(mode="daily")`，本机排版 |
| 「生成本周周报，存成 docx」 | `generate_report(mode="weekly", format="docx", save=true)` |
| 「把这份正文存成 html 发我」 | `save_report(format="html")`，生成自包含单文件 |
| 「照我以前日报的格式写」 | `list_templates` → `get_format_spec` → 按模板排版 |

示例输出（节选）：

```markdown
# AI 日报 2026-10-16

## Aiscribe

**1、Aiscribe：**

①、修复引导页时间剪辑只裁摘要、不裁原始消息的问题，窗口外的 AI 正文不再进入报告。

②、补充 MCP 工具的只读/写入权限标注，并让单个连接器异常不再拖垮整次调用。

## 其他

**1、环境配置：**

①、核对本地依赖与打包脚本，确认 Windows sidecar 可在无 Python 环境启动。
```

---

## 工具与权限

所有工具都通过本地 stdio 的 MCP 服务提供，不监听网络端口。

| 工具 | 作用 | 权限 |
|---|---|---|
| `list_sources` | 列出 16 个数据源与可用状态 | 只读 |
| `get_conversations` | 按项目/日期取会话素材（`summary` 或 `full`） | 只读 |
| `generate_report` | 本机生成 md / docx / html 报告 | 本机写入 `reports/` |
| `save_report` | 把 AI 写好的正文落盘 | 本机写入 `reports/` |
| `list_templates` | 列出已学到的格式模板 | 只读 |
| `get_format_spec` | 取模板的排版规范 | 只读 |
| `save_template` | 保存/回填格式模板 | 本机写入模板目录 |

报告与配置写入 `%USERPROFILE%\.aireport\`（可用 `AIREREPORT_HOME` 覆盖）：

```
~/.aireport/
├── reports/      # 生成的报告
├── templates/    # 格式克隆学到的模板
├── trims.json    # 引导页里的时间剪辑
└── llm.json      # 可选的归纳引擎配置（含 API Key）
```

---

## 数据从哪来

Aiscribe **只读**以下工具已经写在本机的会话文件。默认只扫描当前 Windows 用户；
多人共用一台机器时，不会去读其他用户的目录（除非显式设置
`AIREREPORT_SCAN_ALL_USERS=1`）。

| 类别 | 数据源 | 内容 |
|---|---|---|
| CLI / 桌面端 | CodeBuddy CN | 会话元数据 + 本地日志里的真实输入 |
| | TRAE | 会话记忆 JSONL + 工作区元数据 |
| | Codex | `~/.codex` 会话 |
| | dsh | `~/.dsh/sessions` |
| | Claude Code | `~/.claude/projects` |
| | Gemini CLI | `~/.gemini` |
| | Qwen Code | `~/.qwen` |
| | iFlow | `~/.iflow` |
| 编辑器内建聊天 / 代理 | GitHub Copilot Chat | VS Code 系 `chatSessions` |
| | Cline / Roo Code / Kilo Code | 插件 `globalStorage` |
| | Cursor | `state.vscdb` 会话键 |
| 仅元数据 | 豆包 | 本地 IndexedDB 会话标题与时间 |
| | WorkBuddy | SQLite 会话元数据 |
| | Kimi | 本地状态与日志（正文在云端，不读取） |

想确认本机实际读到了什么，运行 `aireport sources`，或直接使用 `list_sources` 工具。
各数据源路径都可以用环境变量覆盖（例如 `DSH_SESSIONS_DIR`、`CLAUDE_CODE_DIR`）。

---

## 隐私与数据流

这是本插件最重要的部分，请按实际使用方式判断：

1. **读取**：sidecar 以当前用户权限读取上表中的本地文件。不联网、不读取云端会话、
   不上传完整会话文件，也没有遥测。
2. **进入模型上下文**：MCP 工具返回的内容会进入你当前会话的模型上下文。
   - 若使用**云端模型**，`get_conversations(detail="full")` 返回的对话原文
     等同于发送给该模型提供商。
   - 服务端在 `initialize` 中已提示模型：**先用 `detail="summary"` 摸底**，
     只有你明确要求生成报告时才读取相关项目的全文，且不要读取无关日期/项目。
   - 你可以先用时间剪辑或指定 `ids` 缩小范围，再取全文。
3. **AI 归纳（可选）**：`generate_report(polish=true)` 会把本机排版前的
   **骨架文本**（当日摘要与最多 120 字的 AI 结论片段，不含完整原始对话）
   发往你配置的 LLM。未配置 Key、请求失败或返回格式不合规时，自动退回纯模板报告。
   默认 `polish=false`，引导页也默认不勾选 AI 归纳。
4. **写入**：只写 `~/.aireport/`。删除该目录即可清除全部报告、模板和剪辑记录。
5. **不要提交**：`llm.json`、`projects.json`、`reports/`、`templates/` 已被
   `.gitignore` 排除；分享日志前请自行检查是否含个人信息。

---

## 常见问题

**装完没有工具？**
新开一个会话；旧会话不会重新加载工具列表。

**提示找不到 sidecar？**
当前版本仅支持 Windows x64。开发态可设置 `AIREREPORT_REPO` 指向源码仓库，
或设置 `AIREREPORT_BIN` 指向已有的 `aireport.exe`。

**某个数据源会话数是 0？**
该工具可能没有在本机留下会话，或数据目录不在默认位置。用对应环境变量覆盖路径后重试。

**报告里没有逐条正文？**
部分数据源（豆包 / WorkBuddy / Kimi）本地只有元数据，正文在云端；Aiscribe 不会联网补全。

**Windows 提示未知发布者？**
sidecar 是未签名的本地可执行文件，可能触发 SmartScreen。源码与构建脚本均在本仓库，
可自行复核后构建。

---

## 从源码构建

```bash
# 1. 建一个干净的构建环境（避免系统里的旧包与 PyInstaller 冲突）
python -m venv build/venv
build/venv/Scripts/python -m pip install pyinstaller zstandard

# 2. 生成 Windows x64 sidecar 到 dsh-plugin/runtime/win32-x64/
build/venv/Scripts/python scripts/build_sidecar.py

# 3. 打包 npm 包（会带上 runtime/）
cd dsh-plugin && npm pack
```

开发态也可以不打包 sidecar：设置 `AIREREPORT_REPO` 指向仓库根，插件会退回
`python -m ai_report mcp --stdio`。

运行测试：

```bash
python -m unittest discover -s tests -t .
```

---

## 支持与许可

- 问题与建议：请在插件市场页面或仓库 Issue 中反馈。
- 许可证：本插件为 MIT。sidecar 内嵌 CPython、PyInstaller bootloader、zstandard 等组件，
  各自的许可全文见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与 `third-party/`。
- **权限提示**：DSH 插件在宿主进程中运行，拥有与宿主相同的本机权限，没有额外沙箱。
  本插件只调用自带 sidecar 的 stdio 接口，不监听网络端口；请从本仓库或官方市场安装，
  不要使用来路不明的二次打包版本。
- 本项目不会读取与报告无关的文件；若你发现越界行为，请立即停止使用并反馈。
