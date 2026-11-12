"""明文 MMKV 解析，用于读取 QQ 音乐未加密的密钥库文件。

结构：[actualSize: u32 LE][ItemSizeHolder: 4B][varint keyLen][key][varint valLen][val]
其中 val 内层还有一层 varint 长度前缀；同名条目按后写覆盖、零长值表示删除。
加密型 vault（AES-128-CFB）不在支持范围内。
"""
from __future__ import annotations

_BASE64_CHARS = set(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def looks_like_ekey(v: str) -> bool:
    """ekey 启发式（freenote `looksLikeEkey` 语义）：≥16 字符且全为 base64 字母表字符"""
    return len(v) >= 16 and all(b in _BASE64_CHARS for b in v.encode("ascii", "ignore")) and v.isascii()


def _read_varint(data: bytes, pos: int, end: int) -> tuple[int, int]:
    result = 0
    shift = 0
    p = pos
    while True:
        if p >= end:
            raise ValueError("MMKV 数据截断（varint 不完整）")
        b = data[p]
        p += 1
        result |= (b & 0x7F) << shift
        if b & 0x80 == 0:
            return result, p
        shift += 7
        if shift >= 35:
            raise ValueError("MMKV 数据损坏（varint 异常）")


def _read_len_delim(data: bytes, pos: int, end: int) -> tuple[bytes, int]:
    length, pos = _read_varint(data, pos, end)
    if pos + length > end:
        raise ValueError("MMKV 数据截断（条目不完整）")
    return data[pos : pos + length], pos + length


def _unwrap_inner(value: bytes) -> bytes | None:
    """剥值内层的 varint 长度前缀（MMKV getBytes 语义）；损坏返回 None（跳过该条目）"""
    try:
        length, p = _read_varint(value, 0, len(value))
    except ValueError:
        return None
    if p + length > len(value):
        return None
    return value[p : p + length]


def parse(data: bytes) -> list[tuple[str, str]]:
    """解析整个 MMKV 文件，返回 (条目名, ekey) 列表。

    非 ekey 值（vault 里可能混有其他键值）按启发式过滤；解析失败抛 ValueError（非 MMKV 文件）。
    """
    if len(data) < 8:
        raise ValueError("不是有效的 MMKV 文件（文件过短）")
    actual_size = int.from_bytes(data[0:4], "little")
    end = 4 + actual_size
    if actual_size == 0 or end > len(data):
        raise ValueError("不是有效的 MMKV 文件（actualSize 异常）")

    pos = 8  # 跳过 header(4) + ItemSizeHolder(4)
    # LinkedHashMap 语义：重复 key 后写胜、空值删除——用 list 保序 + dict 定位
    order: list[str] = []
    raw: dict[str, bytes] = {}
    while pos < end:
        key_bytes, pos = _read_len_delim(data, pos, end)
        value, pos = _read_len_delim(data, pos, end)
        key = key_bytes.decode("utf-8", "replace")
        if len(value) == 0:
            raw.pop(key, None)
            if key in order:
                order.remove(key)
        else:
            if key not in raw:
                order.append(key)
            raw[key] = value

    out: list[tuple[str, str]] = []
    for key in order:
        value = raw[key]
        inner = _unwrap_inner(value)
        if inner is None:
            continue
        # ekey 为 ASCII base64 文本
        text = inner.decode("utf-8", "replace")
        if looks_like_ekey(text):
            out.append((key, text))
    return out
