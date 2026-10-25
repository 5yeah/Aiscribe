"""连接器注册表：新增数据源只需往 REGISTRY 追加实例。

全部连接器都是**原生实现**：直接读各家工具自己的本地文件，不依赖 Cass 之类的
第三方索引器，也不需要额外装任何包（zstd 压缩的会话需要 `zstandard` 或 zstd 命令，
没有时自动退化为只读元数据，见 connectors/common.py）。

顺序即报告「数据来源」的展示顺序：正文最全、最常用的排前面。
"""

from __future__ import annotations

from typing import Iterator, List

from ai_report.connectors.base import BaseConnector
from ai_report.connectors.claude_code import ClaudeCodeConnector
from ai_report.connectors.cline import ClineConnector, KiloCodeConnector, RooCodeConnector
from ai_report.connectors.codebuddy_cn import CodeBuddyCNConnector
from ai_report.connectors.codex import CodexConnector
from ai_report.connectors.cursor import CursorConnector
from ai_report.connectors.doubao import DoubaoConnector
from ai_report.connectors.dsh import DshConnector
from ai_report.connectors.gemini_cli import (
    GeminiCliConnector,
    IFlowConnector,
    QwenCodeConnector,
)
from ai_report.connectors.kimi import KimiConnector
from ai_report.connectors.trae import TraeConnector
from ai_report.connectors.vscode_chat import VSCodeChatConnector
from ai_report.connectors.workbuddy import WorkBuddyConnector

# 所有已注册的数据源
REGISTRY: List[BaseConnector] = [
    # —— 原生 CLI / 桌面端（有逐条正文）——
    CodeBuddyCNConnector(),
    TraeConnector(),
    CodexConnector(),
    DshConnector(),
    ClaudeCodeConnector(),
    GeminiCliConnector(),
    QwenCodeConnector(),
    IFlowConnector(),
    # —— 编辑器内建聊天与插件代理 ——
    VSCodeChatConnector(),
    ClineConnector(),
    RooCodeConnector(),
    KiloCodeConnector(),
    CursorConnector(),
    # —— 只有标题 / 元数据的源 ——
    DoubaoConnector(),
    WorkBuddyConnector(),
    KimiConnector(),
]


def iter_available() -> Iterator[BaseConnector]:
    """只 yield 本地数据文件存在的连接器。"""
    for conn in REGISTRY:
        if conn.available():
            yield conn
