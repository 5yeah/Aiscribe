"""LevelDB SSTable (.ldb) 读取器：纯标准库，含 Snappy 解压。

Chrome 的 LevelDB 表块默认 Snappy 压缩，故 .ldb 无法直接 grep；
本模块解析 footer -> index -> data block，产出 (key, value) 原始字节对。
"""

from __future__ import annotations

from typing import Iterator, Optional, Tuple

_FOOTER_MAGIC = bytes.fromhex("57fb808b247547db")
_KNOBLOCK = 0
_KSNAPPY = 1


def _read_varint(buf: bytes, pos: int) -> Optional[Tuple[int, int]]:
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            return None
    return None


def snappy_decompress(src: bytes) -> Optional[bytes]:
    """Snappy 块格式解压；任何越界/异常返回 None。"""
    parsed = _read_varint(src, 0)
    if parsed is None:
        return None
    out_len, pos = parsed
    if out_len > 64 * 1024 * 1024:
        return None
    out = bytearray()

    while pos < len(src):
        tag = src[pos]
        pos += 1
        typ = tag & 0x03
        if typ == _KNOBLOCK:  # literal
            length = tag >> 2
            if length >= 60:
                n = length - 59
                if pos + n > len(src):
                    return None
                length = int.from_bytes(src[pos:pos + n], "little") + 1
                pos += n
            else:
                length += 1
            if pos + length > len(src):
                return None
            out += src[pos:pos + length]
            pos += length
        else:
            if typ == 1:
                length = 4 + ((tag >> 2) & 0x07)
                if pos >= len(src):
                    return None
                offset = ((tag & 0xE0) << 3) | src[pos]
                pos += 1
            elif typ == 2:
                if pos + 2 > len(src):
                    return None
                length = (tag >> 2) + 1
                offset = int.from_bytes(src[pos:pos + 2], "little")
                pos += 2
            else:
                if pos + 4 > len(src):
                    return None
                length = (tag >> 2) + 1
                offset = int.from_bytes(src[pos:pos + 4], "little")
                pos += 4
            if offset == 0 or offset > len(out):
                return None
            start = len(out) - offset
            for i in range(length):  # 逐字节，允许重叠复制
                out.append(out[start + i])
        if len(out) > out_len + 16:
            return None

    if len(out) < out_len:
        return None
    return bytes(out[:out_len])


def _read_block(data: bytes, offset: int, size: int) -> Optional[bytes]:
    """读一个表块：block 内容 + 1 字节压缩类型尾标。"""
    if offset < 0 or size < 0 or offset + size + 1 > len(data):
        return None
    block = data[offset:offset + size]
    ctype = data[offset + size]
    if ctype == _KNOBLOCK:
        return block
    if ctype == _KSNAPPY:
        return snappy_decompress(block)
    return None


def _iter_block_entries(block: bytes) -> Iterator[Tuple[bytes, bytes]]:
    if len(block) < 4:
        return
    num_restarts = int.from_bytes(block[-4:], "little")
    if num_restarts > len(block) // 4:
        return
    limit = len(block) - 4 * (num_restarts + 1)
    if limit < 0:
        return

    pos = 0
    key = b""
    while pos < limit:
        r1 = _read_varint(block, pos)
        if r1 is None:
            return
        shared, pos = r1
        r2 = _read_varint(block, pos)
        if r2 is None:
            return
        non_shared, pos = r2
        r3 = _read_varint(block, pos)
        if r3 is None:
            return
        value_len, pos = r3
        if pos + non_shared + value_len > limit:
            return
        key = key[:shared] + block[pos:pos + non_shared]
        pos += non_shared
        value = block[pos:pos + value_len]
        pos += value_len
        yield key, value


def iter_table(data: bytes) -> Iterator[Tuple[bytes, bytes]]:
    """遍历 SSTable 全部 (key, value)。数据损坏时提前结束而非抛错。"""
    if len(data) < 48 or data[-8:] != _FOOTER_MAGIC:
        return
    footer = data[-48:-8]
    pos = 0
    handles = []
    for _ in range(4):  # metaindex(offset,size) + index(offset,size)
        parsed = _read_varint(footer, pos)
        if parsed is None:
            return
        handles.append(parsed[0])
        pos = parsed[1]
    idx_off, idx_size = handles[2], handles[3]

    index = _read_block(data, idx_off, idx_size)
    if not index:
        return
    for _, handle_val in _iter_block_entries(index):
        h = _read_varint(handle_val, 0)
        if h is None:
            continue
        off, p = h
        h2 = _read_varint(handle_val, p)
        if h2 is None:
            continue
        size, _ = h2
        block = _read_block(data, off, size)
        if not block:
            continue
        yield from _iter_block_entries(block)


def iter_table_file(path) -> Iterator[Tuple[bytes, bytes]]:
    try:
        data = open(path, "rb").read()
    except OSError:
        return
    yield from iter_table(data)
