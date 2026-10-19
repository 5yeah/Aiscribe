"""新数据源连接器模板：复制到 ai_report/connectors/<name>.py，按需修改即可。

两步接入（详见 docs/数据源适配.md）：
1) 在 ai_report/config.py 的 default_paths() 里补一行你这款工具的默认路径（环境变量可覆盖）
2) 在 ai_report/connectors/registry.py 的 REGISTRY 里追加 YourToolConnector()

接口约定：
- available() 只检查本地文件是否存在，绝不联网
- read() 把本地记录归一化成 UnifiedRecord，失败时打印日志并返回 []（不要抛异常）
- 想把“正文级”内容交给上层 AI 归纳，就放进 raw["messages"] = [{"ts": epoch秒, "text": 原文}]
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.models import UnifiedRecord
from ai_report.utils import log, parse_ts, safe_str

#: 环境变量名（用户可用它覆盖自动发现的路径）
ENV_KEY = "YOURTOOL_DB"


class YourToolConnector(BaseConnector):
    name = "YourTool"          # 报告“数据来源”里显示的名字

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or get_db_path(ENV_KEY)

    def available(self) -> bool:
        return Path(self.db_path).is_file()

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 数据源不存在，跳过：{self.db_path}")
            return []

        records: List[UnifiedRecord] = []
        for row in self._query():          # TODO: 换成你自己的读取逻辑
            records.append(
                UnifiedRecord(
                    source=self.name,
                    session_id=safe_str(row["id"]),
                    title=safe_str(row["title"]) or "(无标题会话)",
                    project="",        # 留空，交给 L2 统一分类（关键词 + cwd 末段兜底）
                    status=safe_str(row["status"]),   # completed/working → L2 归一成中文
                    cwd=safe_str(row["cwd"]),         # 有项目目录就填，分类更准
                    start_ts=parse_ts(row["start"]),  # epoch 秒/毫秒都行，parse_ts 会识别
                    end_ts=parse_ts(row["end"]) or parse_ts(row["start"]),
                    summary=safe_str(row["summary"]),  # 一句话时间线，用于 summary 模式
                    raw={              # 逐条原文放这里，get_conversations(detail=full) 会用到
                        "messages": [{"ts": t, "text": s} for t, s in row["messages"]],
                    },
                )
            )
        log(f"[{self.name}] 读取到 {len(records)} 条会话")
        return records

    def _query(self):
        """返回可迭代的记录字典列表。这里只是示意，改成你自己的实现。"""
        raise NotImplementedError("请实现 _query()：从你的数据源读取原始记录")
