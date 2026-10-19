"""豆包（字节跳动）会话读取器。

已探明事实：
- 会话列表存在 Chromium IndexedDB 的 LevelDB 目录，主力库是 chrome_doubao-chat
- value 为 V8 序列化 blob：字段名用 0x22 + 长度 + ASCII，标题用 0x63 + 长度 + UTF-16LE
- 时间戳是 ASCII 的 epoch 秒，字段名尾部固定为 "_time"；新写入的记录字段名前半段
  会被噪声字节覆盖，只能按稳定尾部 `e_time"<len> + 10 位数字` 匹配
- .log 是未压缩 write batch，.ldb 是 Snappy 压缩 SSTable，两者都要扫
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.connectors.leveldb_scan import iter_log_records
from ai_report.connectors.leveldb_sst import iter_table_file
from ai_report.models import UnifiedRecord
from ai_report.utils import log, parse_ts

ENV_KEY = "DOUBAO_IDB_DIR"

_TIME_RE = re.compile(rb'e_time"\n(\d{10})')
_NAME_RE = re.compile(rb"name")
_TS_MIN = 1577836800       # 2020-01-01
_TS_MAX = 4102444800       # 2100-01-01（防未来脏值）
_SPAN_LIMIT = 86400        # 单会话有效跨度上限（1 天），超过视为快照噪声


def _read_varint(buf: bytes, pos: int):
    shift = result = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            return None, pos
    return None, pos


def _next_u16(v: bytes, pos: int, window: int = 16):
    """取 pos 之后第一个 0x63(UTF-16 串) 的可读文本；没有则返回 None。"""
    p = pos
    end = min(len(v), pos + window)
    while p < end:
        if v[p] == 0x63:
            length, q = _read_varint(v, p + 1)
            if length and length % 2 == 0 and q + length <= len(v):
                try:
                    s = v[q:q + length].decode("utf-16-le")
                except UnicodeDecodeError:
                    return None
                if s.strip() and all(c.isprintable() for c in s) and len(s) <= 200:
                    return s.strip()
                return None
        p += 1
    return None


def _extract_title(v: bytes):
    for m in _NAME_RE.finditer(v):
        s = _next_u16(v, m.end())
        if s:
            return s
    return None


def _iter_values(root: Path) -> Iterator[bytes]:
    for f in sorted(root.glob("*.log")):
        if f.stat().st_size:
            for _k, v in iter_log_records(f):
                yield v
    for f in sorted(root.glob("*.ldb")):
        if f.stat().st_size:
            for _k, v in iter_table_file(f):
                yield v


class DoubaoConnector(BaseConnector):
    name = "豆包"
    content_level = "仅标题"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_db_path(ENV_KEY))

    def available(self) -> bool:
        return self.root.is_dir()

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 数据目录不存在，跳过：{self.root}")
            return []

        # 同一会话会在多个快照里重复出现，按 (标题, 创建时间) 合并，时间取并集
        merged: Dict[Tuple[str, int], List[int]] = {}
        errors = 0
        for sub in sorted(p for p in self.root.glob("*") if p.is_dir()):
            try:
                for v in _iter_values(sub):
                    if b"conversation" not in v:
                        continue
                    times = [int(t) for t in _TIME_RE.findall(v)]
                    times = [t for t in times if _TS_MIN <= t <= _TS_MAX]
                    if not times:
                        continue
                    title = _extract_title(v) or "(无标题会话)"
                    key = (title, min(times))
                    bucket = merged.setdefault(key, [])
                    bucket.extend(times)
            except OSError as exc:
                errors += 1
                log(f"[{self.name}] 读取 {sub.name} 失败：{exc}")

        if errors:
            log(f"[{self.name}] 有 {errors} 个目录读取失败")

        records: List[UnifiedRecord] = []
        for (title, created), times in merged.items():
            start = float(min(times))
            # 同一会话会被多个 LevelDB 快照重复写入，时间戳直接并集会虚增跨度
            # （曾出现 7 天以上的跨度假象，把陈年会话拉进当天日报）；
            # 只保留起始后 1 天内的活跃时间，与日报的跨夜会话口径一致。
            active = [t for t in times if t - start <= _SPAN_LIMIT]
            end = float(max(active)) if active else start
            digest = hashlib.md5(f"{title}|{created}".encode("utf-8")).hexdigest()[:8]
            records.append(
                UnifiedRecord(
                    source=self.name,
                    session_id=f"db{created}-{digest}",
                    title=title,
                    status="",
                    cwd="",
                    start_ts=start,
                    end_ts=end,
                    summary=title,
                    raw={"create": created, "update": int(end)},
                )
            )
        records.sort(key=lambda r: r.start_ts)
        return records
