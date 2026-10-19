# 接入 MCP 客户端（TRAE / Claude Code / Cursor / dsh …）

`aireport` 用标准 **MCP stdio transport**（JSON-RPC 走 stdin/stdout，日志走 stderr），
任何支持 MCP 的 AI 工具加一段配置就能调用。协议与取数逻辑全部在本地，数据不出本机。

## 一、暴露的工具

| 工具 | 关键参数 | 说明 |
|---|---|---|
| `list_sources` | — | 列出本机已发现的会话数据源、可用状态与会话数 |
| `get_conversations` | `mode`(daily/weekly/range)、`date`、`start`、`end`、`project`、`detail`(full/summary)、`ids` | **推荐入口**：按项目返回原始对话（`full` 含逐条用户输入全文），供 AI 阅读并归纳；`ids` 用于只取本地引导页勾选的那几条记录。被引导页剪辑过的记录会带 `trim` 字段，窗口外的片段**已由工具丢弃** |
| `save_report` | `markdown`、`filename`、`format`(md/docx/html) | 把 AI 撰写好的报告正文落盘到 `reports/`；`html` 是自包含单文件（样式内嵌、无脚本、无外部资源），适合分享 |
| `list_templates` | — | 列出用户已学到的格式模板（引导页「格式克隆」上传的旧日报样例） |
| `get_format_spec` | `name`（可选） | 取模板的「输出格式规范」文本；**写报告前先取它，再照此排版** |
| `save_template` | `name`、`features`、`samples` | 保存/回填格式模板（读图后把看到的排版结构写回，只写结构不写内容） |
| `generate_report` | `mode`、`date`、`start`、`end`、`polish`、`save` | 不经过 AI 理解的模板直出（快速查看/兜底用） |

分工说明：`get_conversations` 负责“一条不漏地取回真实对话”，**归纳由你（客户端 AI）完成**。
这套分工不需要任何 API Key，也不需要把对话上传到云端。

## 二、启动命令

把下面路径里的 `<仓库>` 换成本机 Aiscribe 仓库的绝对路径：

```
python <仓库>/run_mcp_stdio.py
```

> `run_mcp_stdio.py` 会自动把仓库根目录加入 `sys.path`，
> 因此不管你从哪个工作目录启动、有没有设置 `PYTHONPATH`，都能找到 `ai_report` 包。

## 三、各客户端配置

### Claude Code

```powershell
claude mcp add aireport -- python <仓库>/run_mcp_stdio.py
```

### TRAE / Cursor / dsh / 其他通用 JSON 配置

```json
{
  "mcpServers": {
    "aireport": {
      "command": "python",
      "args": ["<仓库>/run_mcp_stdio.py"]
    }
  }
}
```

Cursor 一般写在 `~/.cursor/mcp.json`（全局）或项目内 `.cursor/mcp.json`。

### 已知坑

- `command: "python"` 解析失败时，换成本机 Python 的**绝对路径**（`where python` 查询），`args` 不变。
- **TRAE 会把 `command` 按空格拆分后再启动进程**，所以 `command` 必须是不含空格的路径；
  安装目录带空格时改用 8.3 短路径（如 `C:\Users\ADMINI~1\miniconda3\python.exe`，
  用 `cmd /c dir /x` 查看短名）。这类问题只影响 `command`，不影响的 `args`。
- 启动失败排查日志：
  `%APPDATA%\TRAE SOLO CN\logs\*\window1\exthost\mcp-servers-host.log`。

### 本地 HTTP（可选，给网页/脚本用）

```powershell
python -m ai_report mcp --port 5070   # POST /mcp + GET /health，只监听 127.0.0.1
```

## 四、配合 Skill 使用

把 [skills/ai-report/SKILL.md](../skills/ai-report/SKILL.md) 放进你客户端能读到的 skills 目录
（TRAE：`.trae/skills/` 或用户级 skills 目录；Claude Code：`~/.claude/skills/`），
之后直接说“生成今天的日报”即可，客户端 AI 会自己按 Skill 里的方法论走完
**取数 → 归纳 → 落盘** 全流程。

## 五、不用 AI 也能用：本地引导页

想先“看一眼素材再决定交给 AI 什么”，可以另开一个终端跑：

```powershell
python -m ai_report ui          # 默认 5071，自动打开浏览器
```

页面依次是**数据源扫描 → 时间窗（日报 / 周报 / 自定义区间）→ 按项目勾选素材 → 模板与输出格式**，
到等待页有两条路：

- **生成报告**：本机按勾选与剪辑过滤素材、渲染成草稿，再交给归纳引擎（`llm.json` 配
  DeepSeek，或指向本机 ollama）整理成内容级摘要，写进 `reports/`；页面标注是否真的归纳过，
  没配引擎就如实退回草稿并说明原因
- **复制「交给 AI 的指令」**：那段文本里已经带你勾选的记录 `ids`，
  直接粘到任意接了 aireport MCP 的 AI 对话里即可（AI 会按第 1 节的
  `get_conversations(ids=[...])` 只取这些记录），归纳质量最高

素材 chip 右侧的 **✂** 可给单条会话设起止时间（「清空全部剪辑」一键撤销）：
窗口外的片段在工具内部就被丢弃，`get_conversations` 不会返回，
因此不需要在提示词里额外叮嘱 AI「别用被剪掉的内容」。
