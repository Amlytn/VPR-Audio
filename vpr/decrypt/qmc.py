"""QQ 音乐 QMC 解密（静态表 / Map / RC4、ekey 派生、footer 解析），算法对齐 unlock-music（MIT）。

1. 数据密码：QMCv1 静态表（免密钥）/ QMCv2 Map（密钥 ≤300B）/ QMCv2 RC4（>300B），
   均为对称 XOR 流，密钥流只依赖绝对 offset；
2. ekey → 密钥：base64 →（EncV2 前缀则双 TEA）→ 8 字节头与固定序列交织；
3. footer：文件尾部 ekey / QMCv2 标识（四型），缺失时回退到 QQ 密钥源。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from . import tc_tea
from .sniff import sniff_ext

ERR_NO_KEY = "文件不含解密密钥"

# QMCv1 静态掩码表预计算周期：offset > 0x7FFF 时取模；掩码索引公式 (o*o+27)&0xFF
# 对 o ∈ [0, 0x7FFF] 有 o 与 o+0x7FFF 不同余（o² mod 0x7FFF ≠ (o+0x7FFF)² mod 0x7FFF
# 当 o mod 0x7FFF ≠ 0），故掩码周期必须按 0x7FFF 跳位语义整体预计算一张 0x8000 表。
_V1_OFFSET_BOUNDARY = 0x7FFF

# ============================================================================
# QMCv1 静态密码（免密钥，.qmc0/.qmc2/.qmc3/.qmcflac/...）
# ============================================================================

_STATIC_CIPHER_BOX = bytes.fromhex(
    "77483273DEF2C0C895EC30B251C3E1A0"
    "9EE69DCFFA7F14D1CEB8DCC34A6793D6"
    "28C29170CA8DA2A4F00861907E6FA2E0"
    "EBAE3EB667C792F491B5F66C5E8440F7"
    "F31B027FD5AB418928F425CC5211AD43"
    "68A6418B84B5FF2C924A26D8476A7C95"
    "61CCE6CBBB3F47588975C375A1D9AFCC"
    "087317DCAA9AA21641D8A206C68BFC66"
    "349FCF1823A00A74E72B277092E9AF37"
    "E68CA7BC62659CC208C988B3F343AC74"
    "2C0FD4AFA1C30164954E489FF4357895"
    "7A39D66AA06D40E84FA8EF111DF31B3F"
    "3F07DD6F5B193019FBEF0E37F00ECD16"
    "49FE5347131ABDA4F14019600EED6809"
    "065F4DCF3D1AFE2077E4D9DAF9A42B76"
    "1C71DB00BCFD0C6CA547F7F600794A11"
)
assert len(_STATIC_CIPHER_BOX) == 256


def _build_static_mask_table() -> bytes:
    """预计算 [0, 0x7FFF] 的掩码：mask(o) = BOX[(o*o + 27) & 0xFF]，
    越界后 o % 0x7FFF（0x7FFF 槽位被跳过）。整表 32768 字节一次性生成。"""
    table = bytearray(0x8000)
    for o in range(0x8000):
        eff = o if o <= _V1_OFFSET_BOUNDARY else o % _V1_OFFSET_BOUNDARY
        table[o] = _STATIC_CIPHER_BOX[(eff * eff + 27) & 0xFF]
    return bytes(table)


_STATIC_MASK_TABLE = _build_static_mask_table()


def _static_mask(offset: int) -> int:
    return _STATIC_MASK_TABLE[offset if offset <= _V1_OFFSET_BOUNDARY else offset % _V1_OFFSET_BOUNDARY]


def _decrypt_static(data: bytearray, offset: int = 0) -> None:
    """QMCv1 静态表解密。

    0..0x7FFF 为第一周期（含 o=0x7FFF 特例）；从 0x8000 起以 0x7FFF 为周期
    循环左移 1 位重复，按整周期做大整数 XOR。
    """
    n = len(data)
    period = _V1_OFFSET_BOUNDARY  # 0x7FFF
    out = bytearray(n)
    pos = 0

    if offset <= period:
        seg_end = min(offset + n, period + 1)
        m = seg_end - offset
        if m > 0:
            mask_int = int.from_bytes(_STATIC_MASK_TABLE[offset:seg_end], "big")
            out[0:m] = (int.from_bytes(data[0:m], "big") ^ mask_int).to_bytes(m, "big")
            pos = m
            n -= m
            offset = seg_end

    if n == 0:
        data[:] = out
        return

    if not hasattr(_decrypt_static, "_rot_mask"):
        _decrypt_static._rot_mask = _STATIC_MASK_TABLE[1:0x7FFF] + _STATIC_MASK_TABLE[0:1]
    rot = _decrypt_static._rot_mask
    phase = (offset - 1) % period

    while n > 0:
        take = min(n, period - phase)
        if phase + take <= period:
            mask_int = int.from_bytes(rot[phase : phase + take], "big")
        else:
            mask_int = int.from_bytes(rot[phase:] + rot[: take - (period - phase)], "big")
        out[pos : pos + take] = (int.from_bytes(data[pos : pos + take], "big") ^ mask_int).to_bytes(take, "big")
        pos += take
        n -= take
        phase = 0
    data[:] = out


MAP_INDEX_OFFSET = 71214
V1_KEY_SIZE = 128


def key_compress(long_key: bytes) -> bytes:
    """将长密钥压缩为 128 字节（非循环移位）。"""
    if not long_key:
        raise ValueError("QMC 密钥为空")
    n = len(long_key)
    result = bytearray(V1_KEY_SIZE)
    for i in range(V1_KEY_SIZE):
        idx = (i * i + MAP_INDEX_OFFSET) % n
        key = long_key[idx]
        shift = (idx + 4) % 8
        result[i] = ((key << shift) | (key >> shift)) & 0xFF
    return bytes(result)


def _qmc1_transform_byte(key: bytes, value: int, offset: int) -> int:
    eff = offset if offset <= _V1_OFFSET_BOUNDARY else offset % _V1_OFFSET_BOUNDARY
    return value ^ key[eff % V1_KEY_SIZE]


def _build_qmc1_mask_stream(key: bytes) -> bytes:
    stream = bytearray(_V1_OFFSET_BOUNDARY + 1)
    for eff in range(_V1_OFFSET_BOUNDARY + 1):
        stream[eff] = key[eff % V1_KEY_SIZE]
    return bytes(stream)


class QmcV2Map:
    def __init__(self, key: bytes) -> None:
        self.key = key_compress(key)
        self._mask_stream = _build_qmc1_mask_stream(self.key)
        self._mask_big = int.from_bytes(self._mask_stream, "big")

    def decrypt(self, data: bytearray, offset: int = 0) -> None:
        """QMCv2 Map 解密，0x7FFF 周期规则同 _decrypt_static。"""
        period = _V1_OFFSET_BOUNDARY
        n = len(data)
        out = bytearray(n)
        pos = 0

        if offset <= period:
            seg_end = min(offset + n, period + 1)
            m = seg_end - offset
            if m > 0:
                mask_int = int.from_bytes(self._mask_stream[offset:seg_end], "big")
                out[0:m] = (int.from_bytes(data[0:m], "big") ^ mask_int).to_bytes(m, "big")
                pos = m
                n -= m
                offset = seg_end

        if n == 0:
            data[:] = out
            return

        if not hasattr(self, "_rot_mask"):
            self._rot_mask = self._mask_stream[1:0x7FFF] + self._mask_stream[0:1]
        rot = self._rot_mask
        phase = (offset - 1) % period

        while n > 0:
            take = min(n, period - phase)
            if phase + take <= period:
                mask_int = int.from_bytes(rot[phase : phase + take], "big")
            else:
                mask_int = int.from_bytes(rot[phase:] + rot[: take - (period - phase)], "big")
            out[pos : pos + take] = (int.from_bytes(data[pos : pos + take], "big") ^ mask_int).to_bytes(take, "big")
            pos += take
            n -= take
            phase = 0
        data[:] = out


FIRST_SEGMENT_SIZE = 0x0080
OTHER_SEGMENT_SIZE = 0x1400
RC4_STREAM_CACHE_SIZE = OTHER_SEGMENT_SIZE + 512


def qmc_hash(key: bytes) -> float:
    h = 1
    for v in key:
        if v == 0:
            continue
        nxt = (h * v) & 0xFFFFFFFF
        if nxt == 0 or nxt <= h:
            break
        h = nxt
    return float(h)


def get_segment_key(seg_id: int, seed: int, h: float) -> int:
    if seed == 0:
        return 0
    return int(h / ((seg_id + 1) * seed) * 100.0)


def _variant_rc4_keystream(key: bytes, total: int) -> bytes:
    """变体 RC4：state 长度等于 key 长度，初值按 u8 截断。"""
    n = len(key)
    state = bytearray(i & 0xFF for i in range(n))
    j = 0
    for i in range(n):
        j = (j + state[i] + key[i % n]) % n
        state[i], state[j] = state[j], state[i]
    out = bytearray(total)
    i = 0
    jj = 0
    for k in range(total):
        i = (i + 1) % n
        jj = (jj + state[i]) % n
        state[i], state[jj] = state[jj], state[i]
        idx = (state[i] + state[jj]) % n
        out[k] = state[idx]
    return bytes(out)


class QmcV2Rc4:
    def __init__(self, key: bytes) -> None:
        self.key = key
        self.hash = qmc_hash(key)
        # 预生成密钥流缓存（skip <= 0x1FF，因此长度足够覆盖任意一段）
        self.key_stream = _variant_rc4_keystream(key, RC4_STREAM_CACHE_SIZE)

    def _process_first_segment(self, data: bytearray, start: int, end: int, offset: int) -> None:
        n = len(self.key)
        key = self.key
        h = self.hash
        for i in range(start, end):
            o = offset + (i - start)
            idx = get_segment_key(o, key[o % n], h)
            data[i] ^= key[idx % n]

    def _process_other_segment(self, data: bytearray, start: int, end: int, offset: int) -> None:
        n = len(self.key)
        seg_id = offset // OTHER_SEGMENT_SIZE
        block_offset = offset % OTHER_SEGMENT_SIZE
        seed = self.key[seg_id % n]
        skip = get_segment_key(seg_id, seed, self.hash) & 0x1FF
        start_idx = skip + block_offset
        count = end - start
        if count <= 0:
            return
        mask = self.key_stream[start_idx : start_idx + count]
        if len(mask) < count:
            for i in range(start, end):
                data[i] ^= self.key_stream[start_idx + (i - start)]
            return
        block = int.from_bytes(data[start:end], "big") ^ int.from_bytes(mask, "big")
        data[start:end] = block.to_bytes(count, "big")

    def decrypt(self, data: bytearray, offset: int = 0) -> None:
        pos = 0
        o = offset
        length = len(data)
        if o < FIRST_SEGMENT_SIZE and pos < length:
            n = min(FIRST_SEGMENT_SIZE - o, length - pos)
            self._process_first_segment(data, pos, pos + n, o)
            pos += n
            o += n
        excess = o % OTHER_SEGMENT_SIZE
        if excess != 0 and pos < length:
            n = min(OTHER_SEGMENT_SIZE - excess, length - pos)
            self._process_other_segment(data, pos, pos + n, o)
            pos += n
            o += n
        while pos < length:
            n = min(OTHER_SEGMENT_SIZE, length - pos)
            self._process_other_segment(data, pos, pos + n, o)
            pos += n
            o += n


class QmcCipher:
    """按密钥长度分派解密器：0=静态表，<=300=Map，>300=RC4。"""

    def __init__(self, key: bytes) -> None:
        if len(key) == 0:
            self._impl: str = "static"
            self._map: QmcV2Map | None = None
            self._rc4: QmcV2Rc4 | None = None
        elif len(key) <= 300:
            self._impl = "map"
            self._map = QmcV2Map(key)
            self._rc4 = None
        else:
            self._impl = "rc4"
            self._map = None
            self._rc4 = QmcV2Rc4(key)

    def decrypt(self, data: bytearray, offset: int = 0) -> None:
        if self._impl == "static":
            _decrypt_static(data, offset)
        elif self._impl == "map":
            assert self._map is not None
            self._map.decrypt(data, offset)
        else:
            assert self._rc4 is not None
            self._rc4.decrypt(data, offset)


EKEY_V2_PREFIX = b"QQMusic EncV2,Key:"
EKEY_V2_KEY1 = bytes.fromhex("3338365A4A5921402 32234255E262928".replace(" ", ""))
EKEY_V2_KEY2 = bytes.fromhex("2A2A23212823242526 5E6131635A2C54".replace(" ", ""))
assert len(EKEY_V2_KEY1) == 16 and len(EKEY_V2_KEY2) == 16


def _make_simple_key(n: int) -> bytes:
    import math

    return bytes(int(abs(math.tan(106.0 + i * 0.1)) * 100.0) & 0xFF for i in range(n))


def _derive_key_v1(decoded: bytes) -> bytes:
    if len(decoded) < 16:
        raise ValueError("ekey 过短，无法解密")
    simple = _make_simple_key(8)
    header, body = decoded[:8], decoded[8:]
    tea_key = bytes(b for pair in zip(simple, header) for b in pair)
    plain = tc_tea.decrypt(body, tea_key)
    return header + plain


def _derive_key_v2(decoded: bytes) -> bytes:
    if not decoded.startswith(EKEY_V2_PREFIX):
        raise ValueError("ekey 缺少 EncV2 前缀")
    body = decoded[len(EKEY_V2_PREFIX) :]
    step1 = tc_tea.decrypt(body, EKEY_V2_KEY1)
    step2 = tc_tea.decrypt(step1, EKEY_V2_KEY2)
    zero = step2.find(b"\x00")
    inner_b64 = step2[: zero if zero != -1 else len(step2)]
    try:
        inner = base64.b64decode(inner_b64)
    except Exception as e:
        raise ValueError(f"ekey 解密失败（EncV2 内层 base64）: {e}") from e
    return _derive_key_v1(inner)


def derive_key(ekey: bytes) -> bytes:
    """将 base64 编码的 ekey 派生为数据解密密钥；TEA 校验失败统一转抛 ValueError。"""
    try:
        decoded = base64.b64decode(ekey, validate=True)
    except Exception as e:
        raise ValueError(f"ekey 不是有效的 base64: {e}") from e
    try:
        if decoded.startswith(EKEY_V2_PREFIX):
            return _derive_key_v2(decoded)
        return _derive_key_v1(decoded)
    except tc_tea.TeaError as e:
        raise ValueError(f"ekey 解密失败（TEA 校验不通过）: {e}") from e


@dataclass
class Footer:
    kind: str  # "qtag" | "stag" | "musicex" | "pc_v1_legacy"
    size: int
    ekey: str | None
    resource_id: int | None = None
    media_mid: str | None = None
    media_filename: str | None = None


def _is_base64(raw: bytes) -> bool:
    return bool(raw) and all(
        (0x41 <= b <= 0x5A) or (0x61 <= b <= 0x7A) or (0x30 <= b <= 0x39) or b in (0x2B, 0x2F, 0x3D)
        for b in raw
    )


def _utf16_ascii(raw: bytes) -> str:
    out = []
    for i in range(0, len(raw) - 1, 2):
        lo, hi = raw[i], raw[i + 1]
        if lo == 0 or lo >= 0x80 or hi != 0:
            break
        out.append(chr(lo))
    return "".join(out)


def parse_footer(tail: bytes) -> Footer | None:
    """探测文件尾部元数据（STag / QTag / MusicEx / PC V1 Legacy）。"""
    if tail.endswith(b"STag"):
        footer = tail[:-4]
        if len(footer) < 4:
            raise ValueError("QQ 音乐文件尾部损坏（STag 长度字段缺失）")
        payload, len_bytes = footer[:-4], footer[-4:]
        payload_len = int.from_bytes(len_bytes, "big")
        if len(payload) < payload_len:
            raise ValueError("QQ 音乐文件尾部损坏（STag 载荷截断）")
        csv = payload[len(payload) - payload_len :].decode("utf-8", "replace")
        parts = csv.split(",")
        if len(parts) < 3:
            raise ValueError(f"QQ 音乐文件尾部损坏（STag 元数据异常）: {csv}")
        seg_id, version, media_mid = parts[0], parts[1], parts[2]
        if version != "2":
            raise ValueError(f"QQ 音乐 STag 版本不支持: {version}")
        if not seg_id.isdigit():
            raise ValueError(f"QQ 音乐 STag 编号异常: {seg_id}")
        return Footer(
            kind="stag",
            size=payload_len + 8,
            ekey=None,
            media_mid=media_mid,
        )

    if tail.endswith(b"QTag"):
        footer = tail[:-4]
        if len(footer) < 4:
            raise ValueError("QQ 音乐文件尾部损坏（QTag 长度字段缺失）")
        payload, len_bytes = footer[:-4], footer[-4:]
        payload_len = int.from_bytes(len_bytes, "big")
        if len(payload) < payload_len:
            raise ValueError("QQ 音乐文件尾部损坏（QTag 载荷截断）")
        csv = payload[len(payload) - payload_len :].decode("utf-8", "replace")
        parts = csv.split(",")
        if len(parts) < 3:
            raise ValueError(f"QQ 音乐文件尾部损坏（QTag 元数据异常）: {csv}")
        ekey, resource_id, version = parts[0], parts[1], parts[2]
        if version != "2":
            raise ValueError(f"QQ 音乐 QTag 版本不支持: {version}")
        if not resource_id.isdigit():
            raise ValueError(f"QQ 音乐 QTag 编号异常: {resource_id}")
        if not _is_base64(ekey.encode("ascii", "ignore")):
            raise ValueError(f"QQ 音乐 QTag 密钥格式异常: {ekey}")
        return Footer(
            kind="qtag",
            size=payload_len + 8,
            ekey=ekey,
            resource_id=int(resource_id) if resource_id else 0,
        )

    if tail.endswith(b"musicex\x00"):
        footer = tail[:-8]
        if len(footer) < 8:
            raise ValueError("QQ 音乐文件尾部损坏（MusicEx 字段缺失）")
        payload, ver_bytes = footer[:-4], footer[-4:]
        version = int.from_bytes(ver_bytes, "little")
        if version != 1:
            raise ValueError(f"QQ 音乐 MusicEx 版本不支持: {version}")
        if len(payload) < 4:
            raise ValueError("QQ 音乐文件尾部损坏（MusicEx 长度字段缺失）")
        payload, size_bytes = payload[:-4], payload[-4:]
        payload_len = int.from_bytes(size_bytes, "little")
        if payload_len != 0xC0:
            raise ValueError(f"QQ 音乐 MusicEx 结构不支持（size={payload_len:#x}）")
        body_len = payload_len - 0x10
        if len(payload) < body_len:
            raise ValueError("QQ 音乐文件尾部损坏（MusicEx 载荷截断）")
        body = payload[len(payload) - body_len :]
        media_filename = _utf16_ascii(body[12 + 60 : 12 + 60 + 100])
        return Footer(kind="musicex", size=payload_len, ekey=None, media_filename=media_filename)

    # PC V1 Legacy 无魔数标记，长度不匹配时按普通音频数据回退 None
    if len(tail) >= 8:
        payload, len_bytes = tail[:-4], tail[-4:]
        payload_len = int.from_bytes(len_bytes, "little")
        max_allowed_ekey_len = 0x500
        if 0 < payload_len <= max_allowed_ekey_len and len(payload) >= payload_len:
            raw = payload[len(payload) - payload_len :]
            zero = raw.find(b"\x00")
            ekey = raw[: zero if zero != -1 else len(raw)]
            if _is_base64(ekey):
                return Footer(
                    kind="pc_v1_legacy",
                    size=payload_len + 4,
                    ekey=ekey.decode("utf-8", "replace"),
                )

    return None


@dataclass
class QmcDecoded:
    audio: bytearray
    audio_ext: str
    footer: Footer | None


def decrypt_bytes(data: bytes, external_ekey: str | None = None) -> QmcDecoded:
    """解密 QMC 字节流；内嵌 ekey 优先，其次使用 external_ekey，否则按 QMCv1 静态表解密。"""
    if len(data) < 8:
        raise ValueError("不是有效的 QQ 音乐加密文件（文件过短）")
    tail = data[max(0, len(data) - 1024) :]
    footer = parse_footer(tail)

    embedded = footer.ekey if footer else None
    ekey = embedded or external_ekey

    # STag / MusicEx 仅含歌曲标识不含密钥，无可用 ekey 时直接提示缺密钥
    if ekey is None and footer is not None:
        if footer.kind == "stag":
            raise ValueError(
                f"{ERR_NO_KEY}（新版 QQ 音乐客户端下载的 STag 文件）；"
                "请用旧版客户端重新下载，或导入 QQ 音乐密钥库后重试"
            )
        if footer.kind == "musicex":
            raise ValueError(
                f"{ERR_NO_KEY}（新版 QQ 音乐客户端下载的 MusicEx 文件 {footer.media_filename}）；"
                "请用旧版客户端重新下载，或导入 QQ 音乐密钥库后重试"
            )

    footer_size = footer.size if footer else 0
    if footer_size > len(data):
        raise ValueError("QQ 音乐文件尾部损坏（footer 大于文件本身）")
    audio = bytearray(memoryview(data)[: len(data) - footer_size])

    if ekey is not None:
        try:
            ekey_bytes = ekey.encode("ascii")
        except UnicodeEncodeError as e:
            raise ValueError("ekey 含非 ASCII 字符，无法解密") from e
        cipher_key = derive_key(ekey_bytes)
    else:
        cipher_key = b""
    cipher = QmcCipher(cipher_key)
    cipher.decrypt(audio, 0)

    ext = sniff_ext(audio)
    if ext is None:
        raise ValueError("解密结果异常（密钥不匹配或文件损坏）")
    return QmcDecoded(audio=audio, audio_ext=ext, footer=footer)


def decrypt_file(path, external_ekey: str | None = None) -> QmcDecoded:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ValueError(f"读取 QQ 音乐文件失败: {e}") from e
    return decrypt_bytes(data, external_ekey)
