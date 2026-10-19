"""统一记录模型：所有 AI 工具的会话都归一化成 UnifiedRecord。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict


@dataclass
class UnifiedRecord:
    """L1 连接器输出 / L2 输入的统一会话记录。

    时间戳统一为 epoch 秒（float），展示时转北京时间（UTC+8）。
    """

    source: str            # "CodeBuddy CN" / "WorkBuddy" / "Cass"
    session_id: str
    title: str
    project: str = ""      # 分类后的项目名，空串表示未分类（由 L2 填充）
    status: str = ""       # 中文：已完成 / 进行中 / 原始值
    cwd: str = ""          # 项目路径（可能为空）
    start_ts: float = 0.0  # epoch 秒
    end_ts: float = 0.0    # epoch 秒
    summary: str = ""      # 一句话摘要（来自 title/首条消息，不含云端正文）
    raw: Dict[str, Any] = field(default_factory=dict)  # 原始字段备份

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UnifiedRecord":
        known = {f: d.get(f) for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)
