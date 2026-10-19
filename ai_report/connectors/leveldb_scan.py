"""LevelDB 轻量扫描（零依赖，供豆包/Kimi 等 Chromium 系连接器使用）。

- .log 文件：完整的 LevelDB log record 格式解析（log 块不做压缩，可直接取出
  write batch 中的 key/value）
- .ldb 文件：SSTable block 可能 Snappy 压缩，不逐条解；退化为可打印字符串启发式扫描
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Iterator, List, Tuple

_BLOCK_HEADER = 7  # crc32c(4) + length(2) + type(1)
_KIND_FULL, _KIND_FIRST, _KIND_MIDDLE, _KIND_LAST = 1, 2, 3, 4

_UUID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_STR_RE = re.compile(rb"[\x20-\x7e\xe4-\xe9][\x20-\x7e\x80-\xbf]{11,}")


def iter_log_records(path: Path) -> Iterator[Tuple[bytes, bytes]]:
    """解析 LevelDB .log 的 write batch，产出 (key, value)。损坏处安全跳过。"""
    try:
        data = path.read_bytes()
    except OSError:
        return
    pos, chunks = 0, []
    while pos + _BLOCK_HEADER <= len(data):
        length = struct.unpack_from("<H", data, pos + 4)[0]
        kind = data[pos + 6]
        start = pos + _BLOCK_HEADER
        end = start + length
        if length == 0 or end > len(data):
            break  # 尾部填充区或截断
        payload = data[start:end]
        pos = end
        if kind in (_KIND_FIRST, _KIND_MIDDLE, _KIND_LAST):
            chunks.append(payload)
            if kind != _KIND_LAST:
                continue
        elif kind == _KIND_FULL:
            chunks = [payload] if not chunks else chunks + [payload]
        else:
            chunks = []
            continue
        yield from _parse_batch(b"".join(chunks))
        chunks = []


def _parse_batch(batch: bytes) -> Iterator[Tuple[bytes, bytes]]:
    """write batch: sequence(8) + count(4) + entries(type+varint key+varint value)。"""
    if len(batch) < 12:
        return
    pos = 12  # 跳过 sequence + count
    while pos < len(batch):
        if pos + 1 > len(batch):
            return
        pos += 1  # entry type（1=put 0=delete）
        key, pos = _read_varint_bytes(batch, pos)
        val, pos = _read_varint_bytes(batch, pos)
        if key is None or val is None:
            return
        yield key, val


def _read_varint_bytes(buf: bytes, pos: int):
    """读 LevelDB varint 长度 + 数据。失败返回 (None, pos)。"""
    shift = result = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            if pos + result > len(buf):
                return None, pos
            return buf[pos:pos + result], pos + result
        shift += 7
        if shift > 63:
            return None, pos
    return None, pos


def scan_strings(path: Path, min_len: int = 12) -> List[str]:
    """对（可能压缩的）.ldb 文件做可打印字符串启发式提取。"""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    out, seen = [], set()
    pat = _STR_RE if min_len <= 12 else re.compile(
        rb"[\x20-\x7e\xe4-\xe9][\x20-\x7e\x80-\xbf]{" + str(min_len).encode() + rb",}")
    for m in pat.finditer(data):
        try:
            s = m.group().decode("utf-8")
        except UnicodeDecodeError:
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def find_uuids_in_file(path: Path) -> List[str]:
    """从文件里提取全部 UUID（对话类应用常用作 conversation id）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    seen, out = set(), []
    for m in _UUID_RE.finditer(data):
        u = m.group().decode("ascii").lower()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
