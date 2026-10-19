"""连接器注册表：新增数据源只需往 REGISTRY 追加实例（如未来加 Cursor / Claude）。"""

from __future__ import annotations

from typing import Iterator, List

from ai_report.connectors.base import BaseConnector
from ai_report.connectors.cass import CassConnector
from ai_report.connectors.codebuddy_cn import CodeBuddyCNConnector
from ai_report.connectors.doubao import DoubaoConnector
from ai_report.connectors.kimi import KimiConnector
from ai_report.connectors.trae import TraeConnector
from ai_report.connectors.workbuddy import WorkBuddyConnector

# 所有已注册的数据源（顺序即报告“数据来源”展示顺序）
REGISTRY: List[BaseConnector] = [
    CodeBuddyCNConnector(),
    WorkBuddyConnector(),
    CassConnector(),
    DoubaoConnector(),
    KimiConnector(),
    TraeConnector(),
]


def iter_available() -> Iterator[BaseConnector]:
    """只 yield 本地数据文件存在的连接器。"""
    for conn in REGISTRY:
        if conn.available():
            yield conn
