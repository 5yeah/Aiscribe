# AI 对话聚合 · 自动日报/周报插件 —— 完整 AI 构建提示词

> 用途：把下面这些提示词**依次（或整体）**发给任意 AI 编码助手（CodeBuddy CN / WorkBuddy / Claude / Cursor / ChatGPT），它就能从零实现这个「本地聚合所有 AI 工具对话 → 自动生成日报周报 → 钉钉提交（带人工审核）」的插件。
> 适用环境：Windows；AI 运行在**你本机**。路径一律用 `%APPDATA%` / `%LOCALAPPDATA%` / 用户主目录占位，换机不用改。
> 设计原则：**本地优先 + 隐私优先 + 零三方依赖（仅 Python 标准库）**。

> **历史文档说明**：本文是项目最初的构建提示词，保留原貌以便追溯。
> 其中 Prompt 1.4 的 Cass 读取器**已被移除**——现在 Codex / Copilot 等都由
> `connectors/codex.py`、`connectors/vscode_chat.py` 等原生实现直接读取，
> 不再需要安装 coding-agent-search。当前支持的数据源见
> [`README.zh-CN.md`](../README.zh-CN.md#支持的数据源) 与 [`数据源适配.md`](数据源适配.md)。

---

## 〇、总纲提示词（第一条发送，建立全局上下文）

把下面的「项目说明书」整段发给 AI：

```
你将要帮我从零开发一个命令行插件，名为「AI 活动日报聚合器」（命令行入口 aireport）。
目标：在用户本机，本地优先、隐私优先地聚合多个 AI 编码/对话工具的历史会话，
自动按项目分类、生成日报/周报/区间报告，并支持把报告提交到钉钉工作日志（带人工审核闸门）。

【四层架构】
- L1 连接器层：每个 AI 工具一个读取器（reader），把原始数据归一化成统一记录（UnifiedRecord）。
- L2 统一索引与分类：合并所有记录、按项目/日期分类、去重、排序。
- L3 报告生成器：把记录渲染成 Markdown 日报/周报；可选接 LLM 做“内容级”润色（无 key 时退回模板）。
- L4 分发层：人工审核后提交到钉钉（绝不在无确认时自动发送）。

【已探明的真实数据源】（运行在本机，路径真实）
1) CodeBuddy CN（腾讯 IDE）
   - 数据库：%APPDATA%/CodeBuddy CN/codebuddy-sessions.vscdb
   - 格式：SQLite，表 ItemTable(key TEXT, value TEXT)
   - key 形如 session:<uuid>，value 是 JSON：{conversationId, cwd, title, status, createdAt, updatedAt}
   - 时间戳为 epoch 毫秒；cwd 是项目目录，title 是会话标题，status 如 completed / working
   - 重要限制：本地只存元数据，消息正文在腾讯云端，本地拿不到 → 报告只能到“任务列表级”
   - 约 40 个会话
2) WorkBuddy（本机 AI 助手）
   - 数据库：<用户主目录>/.workbuddy/workbuddy.db
   - 表 sessions 存元数据；注意 cwd 是 WorkBuddy 自身的工作区目录（不是项目目录），
     因此分类必须靠 title 关键词，不能靠 cwd
   - 约 35 个会话
3) Cass（coding-agent-search，索引 codex / copilot）
   - 数据库：%APPDATA%/coding-agent-search/coding-agent-search/data/agent_search.db
   - 表：conversations, messages, workspaces
   - 16 个会话、314 条消息；messages 含正文；时间戳为 epoch 毫秒
   - 注意过滤标题以 “<” 开头的系统事件（如 <external_codex_apps_open_page>）

【硬约束】
- 仅用 Python 标准库（sqlite3, json, argparse, datetime, urllib.request, os, pathlib），禁止 pip 安装三方包
- 所有对话正文/元数据绝不离开本机；只有“最终要合成的文本”才可能被发往 LLM
- 文件 IO、日志统一 UTF-8；时间显示统一北京时间（UTC+8）
- Windows 路径用正斜杠或原始字符串；不要依赖 Unix 特有命令
- 每个模块可独立运行、可单测；错误要清晰，不要静默失败

【交付】
- 一个包结构（运行时仅标准库），命令 aireport
- 一份 README 说明安装、配置、用法、隐私声明
- 所有代码带中文注释，面向中文用户

请先输出整体目录结构与各模块职责，等我说“开始”再逐层实现。
```

---

## 一、L1 连接器层（数据源读取）

### Prompt 1.1 — 项目脚手架 + 统一记录模型

```
接总纲。先搭脚手架，不要写业务逻辑，只搭结构 + 定义数据模型。

1) 建目录（用 pathlib 创建，不要手敲 mkdir）：
   ai_report/
     __init__.py
     models.py          # UnifiedRecord 数据类
     connectors/
       __init__.py
       base.py          # BaseConnector 抽象接口
       codebuddy_cn.py
       workbuddy.py
       cass.py
       registry.py      # 连接器注册表
     index.py           # L2
     generator.py       # L3
     llm.py             # L4-LLM 合成（DeepSeek 可插拔）
     dingtalk.py        # L4-钉钉
     cli.py             # 命令行入口
     config.py          # 配置加载
     utils.py           # 时间/路径/日志工具
   tests/

2) models.py 定义 UnifiedRecord（用 @dataclass）：
   - source: str            # "CodeBuddy CN" / "WorkBuddy" / "Cass"
   - session_id: str
   - title: str
   - project: str           # 分类后的项目名（空串表示未分类）
   - status: str            # 中文：已完成 / 进行中 / 原始值
   - cwd: str               # 项目路径（可能为空）
   - start_ts: float        # epoch 秒，北京时间展示用
   - end_ts: float          # epoch 秒
   - summary: str           # 一句话摘要（来自 title/status，不取正文）
   - raw: dict              # 原始字段备份

3) connectors/base.py 定义抽象基类：
   - 属性 name: str
   - 方法 read() -> List[UnifiedRecord]
   - 方法 available() -> bool   # 数据库文件是否存在
   - 统一在 utils.py 提供 parse_ts(ms) 处理毫秒/秒混用（f>1e11 则 /1000），统一 datetime.UTC

只输出文件结构和 models.py / base.py 代码，等我说“继续”。
```

### Prompt 1.2 — CodeBuddy CN 读取器

```
接总纲 + Prompt 1.1。实现 connectors/codebuddy_cn.py。

事实（已探明）：
- DB 路径：%APPDATA%/CodeBuddy CN/codebuddy-sessions.vscdb
- SQLite，表 ItemTable(key TEXT, value TEXT)；key 形如 session:<uuid>
- value JSON 字段：conversationId, cwd, title, status, createdAt, updatedAt（epoch 毫秒）
- 本地只有元数据，拿不到消息正文 → summary 用 title + status 拼“任务列表级”描述

要求：
- available() 检查该 vscdb 是否存在
- read() 遍历 ItemTable 中 key LIKE 'session:%' 的行，解析 JSON
- 用 utils.parse_ts 解析 createdAt/updatedAt
- project 先留空（L2 统一分类）；status 用 STATUS_MAP 归一（见 Prompt 3）
- 只把元数据写进 UnifiedRecord，禁止尝试联网取正文
- 异常：文件损坏/打不开时打印清晰错误并返回空列表，不崩溃
- 代码顶部把 DB 路径做成常量，并允许通过环境变量 CODEBUDDY_CN_DB 覆盖

输出完整 codebuddy_cn.py 代码。
```

### Prompt 1.3 — WorkBuddy 读取器

```
接总纲 + Prompt 1.1。实现 connectors/workbuddy.py。

事实（已探明）：
- DB 路径：<用户主目录>/.workbuddy/workbuddy.db
- 表 sessions 存元数据；cwd 是 WorkBuddy 自身工作区目录（如 2026-10-09-09-02-18），不是项目目录
- 因此分类必须靠 title 关键词，不能靠 cwd（L2 用 TITLE_HINTS 处理）

要求：
- available() 检查 workbuddy.db 存在
- read() 读取 sessions 表所有行，字段含 title/createdAt/updatedAt/status 等
- 时间戳按实际单位用 utils.parse_ts 处理
- project 留空（L2 用 title 分类），status 归一
- 同样只取元数据、不联网
- DB 路径常量 + 环境变量 WORKBUDDY_DB 覆盖

输出完整 workbuddy.py 代码。
```

### Prompt 1.4 — Cass 读取器 + 连接器注册表

```
接总纲 + Prompt 1.1。实现 connectors/cass.py 与 connectors/registry.py。

Cass 事实（已探明）：
- DB 路径：%APPDATA%/coding-agent-search/coding-agent-search/data/agent_search.db
- 表：conversations(id, title, created_at, updated_at, workspace_id, ...)，messages(id, conversation_id, role, content, created_at, ...)，workspaces(id, name, path)
- 16 会话 / 314 消息；messages 含正文；时间戳 epoch 毫秒
- 重要：过滤标题以 “<” 开头的系统事件（如 <external_codex_apps_open_page>），它们不是真实任务
- Cass 是少数能拿到消息正文的源 → 它的 summary 可更丰富（取首条 user 消息前 80 字）

要求：
- available() 检查 cass DB 存在
- read() 联表取 conversations + 其首条消息，构造 UnifiedRecord
- 用 workspaces.path 做 cwd，便于 L2 按项目分类
- 过滤 < 开头的 title
- 路径常量 + 环境变量 CASS_DB 覆盖

registry.py：
- REGISTRY: List[BaseConnector] = [CodeBuddyCNConnector(), WorkBuddyConnector(), CassConnector()]
- 提供 iter_available() 只 yield available() 为真的连接器
- 预留扩展：未来加 CursorConnector / ClaudeConnector 只需往 REGISTRY 追加

输出 cass.py 与 registry.py 完整代码。
```

---

## 二、L2 统一索引与分类（Prompt 2）

```
接总纲 + L1。实现 index.py。

职责：
1) collect() -> List[UnifiedRecord]：遍历 registry.iter_available()，合并所有记录，按 session_id 去重
2) 分类（核心，把 project 填上）：
   - PROJECT_MAP（cwd/title 子串匹配，顺序匹配，命中即停），示例：
       "project-a"/"项目甲"  -> 项目甲
       "project-b"/"项目乙"  -> 项目乙
   - 对 WorkBuddy 这类 cwd 不是项目目录的来源，用 TITLE_HINTS（标题关键词）补分类
   - 兜底：cwd 路径末段目录名作为项目名，新项目不改词表也能归类
   - 未命中 -> project = "其他"
3) STATUS_MAP（大小写不敏感）：completed->已完成, working->进行中, 其余保留原文
4) filter_by_date(records, date|start,end) -> 按北京时间的 start_ts 落在范围内筛选
5) 导出 to_json / from_json 便于缓存与调试

分类函数 classify(project_hint, cwd, title) 单独抽出，便于单测。
输出完整 index.py 代码 + 一个 tests/test_index.py（覆盖 PROJECT_MAP 各分支与 < 过滤）。
```

---

## 三、L3 报告生成器（Prompt 3）

```
接总纲 + L2。实现 generator.py，纯模板渲染（不依赖 LLM）。

功能：
- render_daily(date) -> Markdown 字符串
- render_weekly(week_start) -> Markdown 字符串
- render_range(start, end) -> Markdown 字符串

报告结构（中文）：
  # AI 日报 / AI 周报（日期区间）
  > 数据来源：CodeBuddy CN / WorkBuddy / Cass（本地聚合，隐私优先）
  ## 按项目汇总
  ### {项目名}
  - [{状态}] {title}（{source}，{起止时间}）
  ...
  ## 未分类
  ...
  ---
  说明：本地仅存会话元数据，正文在云端，本报告为“任务列表级”汇总。

要求：
- 时间用北京时间（UTC+8）格式化 YYYY-MM-DD HH:MM
- 空项目/空区间给出友好提示而非报错
- 所有 IO UTF-8
- 提供 main 入口可被 cli 调用，也可独立跑：python generator.py --mode daily --date 2026-10-10

输出完整 generator.py + tests/test_generator.py（断言含项目分组、状态映射、空数据处理）。
```

---

## 四、L4-LLM 合成层：DeepSeek 可插拔（Prompt 4）

```
接总纲 + L3。实现 llm.py，把 L3 的“任务列表级”文本升级为“内容级”润色摘要。

接口：
- class ReportSynthesizer:
    def __init__(self, provider="deepseek", api_key=None)
    def synthesize(self, draft_markdown: str) -> str   # 返回润色后的 markdown
- provider 通过环境变量 LLM_PROVIDER 选择，默认 "deepseek"；预留 "openai" / "ollama"

DeepSeek 事实：
- 端点（OpenAI 兼容）：https://api.deepseek.com/chat/completions
- 认证：Header Authorization: Bearer <DEEPSEEK_API_KEY>（环境变量，缺失则不可用）
- 模型：deepseek-chat
- 请求体：{"model":"deepseek-chat","messages":[{"role":"user","content":<prompt>}],"temperature":0.3}
- 用标准库 urllib.request 发 POST + json，禁止 requests
- 隐私：只把 draft_markdown（已是本地汇总，不含原始正文）发往 LLM；不传任何原始对话

降级策略（关键）：
- 若 DEEPSEEK_API_KEY 缺失或请求失败 → 直接返回原始 draft_markdown，并打印“已退回模板模式”
- 绝不因 LLM 失败而中断整个流程

Prompt 设计：要求 LLM“把以下 AI 工作记录整理成专业、简洁的中文日报，按项目分组，突出进展与产出，不改事实、不编造”。

提供 synthesize 的单元桩（mock urllib）测试：无 key 时返回原文本；有 key 时构造假响应验证解析。
输出完整 llm.py + tests/test_llm.py。
```

---

## 五、L4-钉钉提交器 + 人工审核闸门（Prompt 5）

```
接总纲。实现 dingtalk.py，把报告提交到钉钉工作日志。

钉钉事实（已探明）：
- 获取 token：GET https://oapi.dingtalk.com/gettoken?appkey=<APPKEY>&appsecret=<APPSECRET>
  返回 {"access_token": "...", "errcode":0}
- 创建日志：POST https://oapi.dingtalk.com/topapi/report/create?access_token=<token>
  请求体 JSON：
    {
      "template_id": "<模板ID>",
      "userid": "<员工userid>",
      "contents": [ {"key":"今日完成工作","value":"<markdown，≤1000字>"}, ... ],
      "dd_from": "ai-report",
      "to_chat": "", "to_userids": "", "to_cids": ""
    }
- 每个 content 的 value 为 markdown，单字段硬上限 1000 字符，超出必须截断（CONTENT_LIMIT=950 保险）
- 权限：应用需有 qyapi_report_manage（员工日志数据管理）

硬规则（用户强需求：人工审核闸门）：
- 仅在用户显式调用“提交钉钉”时才发送，绝不自动发送
- 提交前必须 --dry-run 打印预览（分段、截断后内容、目标 template_id/userid）
- 凭证从环境变量读取：DINGTALK_APPKEY / DINGTALK_APPSECRET；缺失则拒绝运行并提示配置
- template_id / userid / 字段映射写在 dingtalk_config.json（提供 .example 模板），不写死在代码
- 提供 load_config() 读 json；resolve_source() 决定内容映射到哪个字段（ALL/PROJECTS/section:）
- 网络异常、errcode!=0 时清晰报错，不静默

CLI 设计：
  aireport dingtalk --file report.md           # 先预览（dry-run）
  aireport dingtalk --file report.md --confirm # 真正提交（需用户二次确认）

输出完整 dingtalk.py + dingtalk_config.example.json + tests/test_dingtalk.py（mock urllib 验证分段/截断/拒绝逻辑）。
```

---

## 六、CLI 入口 + 一键脚本 + 定时（Prompt 6）

```
接总纲 + L1~L5。实现 cli.py 与一键启动脚本。

cli.py（argparse，子命令）：
  aireport daily  [--date YYYY-MM-DD] [--llm] [--out FILE]
  aireport weekly [--date YYYY-MM-DD] [--llm] [--out FILE]
  aireport range  --start X --end Y [--llm] [--out FILE]
  aireport dingtalk --file FILE [--confirm]
  aireport sources                       # 列出已发现的数据源（调试用）
  aireport config                        # 显示当前配置（隐藏敏感字段）
行为：
- --llm 开关决定是否走 llm.synthesize；不带则纯模板
- 默认把报告写到 reports/ 目录，文件名 AI日报_日期.md / AI周报_起_止.md
- 全程 UTF-8，时间北京时间

一键脚本（Windows .bat，放在包根）：
- run_daily.bat：调用 python cli.py daily --out reports/...；PY 优先用本机 Python 的绝对路径，
  不存在则退回 PATH 里的 python
- run_weekly.bat：同理 weekly
- submit_dingtalk.bat：先检查 DINGTALK_APPKEY 环境变量，存在才跑 dingtalk（--confirm 由用户手敲决定）
- 用 %~dp0 定位脚本目录，用 start "" reports 打开结果文件夹

定时（说明即可，给出 WorkBuddy 自动化配置示例 JSON）：
- 每周一至周五 18:00 自动生成日报并展示，但“禁止自动提交钉钉”，只有用户说“提交钉钉”才推送

输出 cli.py + run_daily.bat + run_weekly.bat + submit_dingtalk.bat + 一段自动化 JSON 示例（注释说明）。
```

---

## 七、README / 开源打包 / MCP Server（Prompt 7，可选进阶）

```
接总纲 + 全部。补两件事：

A) README.md（中文，开源友好）：
   - 项目定位：Local-first AI 活动日报聚合器（隐私优先，对话不出机）
   - 特性列表：多工具聚合（CodeBuddy CN / WorkBuddy / Cass，可扩展 Cursor/Claude）、
     按项目分类、日报/周报/区间、可选 DeepSeek 润色、钉钉提交（人工审核）
   - 快速开始：放路径常量说明、配置 DINGTALK/DEEPSEEK 环境变量、命令示例
   - 隐私声明：明确本地只取元数据、正文在云端、仅最终摘要可能进 LLM
   - 架构图（用文本/mermaid 画四层）
   - 贡献指南：如何加新连接器（实现 BaseConnector 并注册）

B) MCP Server（可选，让任意 AI 工具能查你本地聚合的会话）：
   - 用标准库实现一个最小 MCP-ish HTTP 服务（或 stdin/stdout JSON-RPC），暴露工具：
       list_sources() -> 已发现数据源
       generate_report(mode, date) -> markdown
   - 说明：MCP 是“让别的 AI 来查本聚合器”的接口，不是把数据传出去
   - 给出启动命令 aireport mcp --port 5070

输出 README.md + mcp_server.py（最小可用版）+ 在 README 补 MCP 章节。
```

---

## 使用建议

1. **稳健路线（推荐）**：把「〇 总纲」+「Prompt 1.1 → 1.4 → 2 → 3 → 4 → 5 → 6」依次发给 AI，每发一段等它产出、你确认后再发下一段。这样每一步都可审、可改。
2. **一键路线**：若你用的 AI 支持长上下文且你信任它，可把「〇 总纲」+ 全部 Prompt 1~7 拼成一个超长提示词一次发出，要求它分阶段提交代码。
3. **关键约束已固化**：表结构、时间戳单位、钉钉接口、隐私闸门、Stdlib-only 全部写死在提示词里，接手 AI 不需要再问你环境问题。
4. **验证清单**（交给 AI 自测或你手测）：
   - `aireport sources` 能列出本机发现的源且标注 available
   - `aireport daily --date <某天>` 产出按项目分组的 md，项目名与你本机目录名对得上
   - 不设 DEEPSEEK_API_KEY 时 `--llm` 自动退回模板且不报错
   - 不设 DINGTALK_APPKEY 时 `dingtalk` 拒绝运行并提示
   - `dingtalk --file x.md` 默认只预览不发送
