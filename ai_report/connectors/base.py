"""L1 连接器抽象接口：每个 AI 工具实现一个 reader。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from ai_report.models import UnifiedRecord


class BaseConnector(ABC):
    """连接器基类。

    约定：
    - available() 只检查本地数据文件是否存在（不联网）
    - read() 把原始数据归一化为 UnifiedRecord 列表；失败时打印清晰错误并返回 []，不崩溃
    - 绝不尝试联网获取内容，正文能本地取到就取，取不到只用元数据
    """

    #: 连接器显示名（如 "CodeBuddy CN"）
    name: str = "base"

    #: 本地能拿到的内容等级（引导页与文档展示用）：
    #: 逐条原文 / 首条正文 / 仅标题 / 元数据
    content_level: str = "元数据"

    @abstractmethod
    def available(self) -> bool:
        """本地数据源是否可用（数据库文件是否存在）。"""

    @abstractmethod
    def read(self) -> List[UnifiedRecord]:
        """读取并归一化为统一记录列表。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r} available={self.available()}>"

    def location(self) -> str:
        """数据源的展示路径（`aireport sources` 用）；多目录型数据源请覆盖。"""
        for attr in ("db_path", "root", "memory_dir"):
            value = getattr(self, attr, "")
            text = str(value) if value else ""
            if text:
                return text
        return ""
