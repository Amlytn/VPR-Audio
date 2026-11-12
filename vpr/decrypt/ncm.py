"""网易云音乐 .ncm 容器解密。

结构：magic(8) + 2 + keyLen + keyBlock + metaLen + metaBlock + crc + gap + audio。
keyBlock 为 RC4 密钥（AES-ECB 解出），metaBlock 为 JSON 元数据与封面。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from Crypto.Cipher import AES

from .sniff import sniff_ext

MAGIC = b"CTENFDAM"
CORE_KEY = b"hzHRAmso5kInbaxW"  # 密钥块的 AES key
META_KEY = bytes([0x23, 0x31, 0x34, 0x6C, 0x6A, 0x6B, 0x5F, 0x21, 0x5C, 0x5D, 0x26, 0x30, 0x55, 0x3C, 0x27, 0x28])  # 元数据块的 AES key
KEY_PLAIN_HEADER = b"neteasecloudmusic"
META_B64_PREFIX = b"163 key(Don't modify):"

ERR_NOT_NCM = "不是有效的 NCM 文件"
ERR_TRUNCATED = "NCM 文件损坏或截断"
ERR_KEY_BLOCK = "NCM 密钥块解析失败（文件可能损坏）"
ERR_NO_AUDIO = "NCM 文件无音频数据"
ERR_BAD_DECRYPT = "NCM 解密结果异常（密钥不匹配或文件损坏）"


@dataclass
class NcmMeta:
    title: str | None = None
    artist: str | None = None
    album: str | None = None


@dataclass
class NcmContent:
    audio: bytearray
    audio_ext: str
    meta: NcmMeta | None
    cover: bytes | None


class _Reader:
    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.buf):
            raise ValueError(ERR_TRUNCATED)
        s = self.buf[self.pos : end]
        self.pos = end
        return s

    def u32le(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def rest(self) -> memoryview:
        s = memoryview(self.buf)[self.pos :]
        self.pos = len(self.buf)
        return s


def _aes_ecb_decrypt(key: bytes, data: bytearray) -> None:
    if len(key) != 16 or len(data) % 16 != 0:
        raise ValueError(ERR_KEY_BLOCK)
    aes = AES.new(key, AES.MODE_ECB)
    data[:] = aes.decrypt(bytes(data))


def _unpad(data: bytes) -> bytes | None:
    if not data:
        return None
    n = data[-1]
    if n == 0 or n > 16 or n > len(data):
        return None
    if any(b != n for b in data[-n:]):
        return None
    return data[:-n]


def build_keystream(key: bytes) -> bytes:
    """生成 NCM 变体 RC4 的 256 字节循环密钥流表。"""
    s = list(range(256))
    last = 0
    for i in range(256):
        swap = s[i]
        c = (swap + last + key[i % len(key)]) & 0xFF
        s[i], s[c] = s[c], s[i]
        last = c
    ks = bytearray(256)
    for k in range(256):
        i = (k + 1) & 0xFF
        j = (i + s[i]) & 0xFF
        ks[k] = s[(s[i] + s[j]) & 0xFF]
    return bytes(ks)


def _parse_meta(json_bytes: bytes) -> NcmMeta | None:
    try:
        v = json.loads(json_bytes.decode("utf-8", "replace"))
        if not isinstance(v, dict):
            return None
    except Exception:
        return None

    def str_field(key: str) -> str | None:
        val = v.get(key)
        return val if isinstance(val, str) and val else None

    artist_val = v.get("artist")
    artist = None
    if isinstance(artist_val, list):
        names = [pair[0] for pair in artist_val if isinstance(pair, list) and pair and isinstance(pair[0], str)]
        artist = " / ".join(names) if names else None

    meta = NcmMeta(title=str_field("musicName"), artist=artist, album=str_field("album"))
    if meta.title is None and meta.artist is None and meta.album is None:
        return None
    return meta


def decrypt_bytes(data: bytes) -> NcmContent:
    r = _Reader(data)
    if r.take(8) != MAGIC:
        raise ValueError(ERR_NOT_NCM)
    r.take(2)  # gap

    key_len = r.u32le()
    key_block = bytearray(r.take(key_len))
    for i in range(len(key_block)):
        key_block[i] ^= 0x64
    _aes_ecb_decrypt(CORE_KEY, key_block)
    plain = _unpad(bytes(key_block))
    if plain is None or not plain.startswith(KEY_PLAIN_HEADER):
        raise ValueError(ERR_KEY_BLOCK)
    rc4_key = plain[len(KEY_PLAIN_HEADER) :]
    if not rc4_key:
        raise ValueError(ERR_KEY_BLOCK)

    # 元数据解析失败不影响音频解密
    meta_len = r.u32le()
    meta_block = bytearray(r.take(meta_len))
    for i in range(len(meta_block)):
        meta_block[i] ^= 0x63

    def try_parse_meta() -> NcmMeta | None:
        try:
            # xor 0x63 后先剥离明文前缀再做 Base64 解码
            body = bytes(meta_block)
            if not body.startswith(META_B64_PREFIX):
                return None
            cipher = bytearray(base64.b64decode(body[len(META_B64_PREFIX) :]))
            _aes_ecb_decrypt(META_KEY, cipher)
            unpadded = _unpad(bytes(cipher))
            if unpadded is None:
                return None
            json_bytes = unpadded.removeprefix(b"music:")
            return _parse_meta(json_bytes)
        except Exception:
            return None

    meta = try_parse_meta()

    r.take(4)  # crc
    r.take(5)  # unknown
    image_len = r.u32le()
    cover = r.take(image_len) if image_len > 0 else None

    audio = bytearray(r.rest())
    if not audio:
        raise ValueError(ERR_NO_AUDIO)
    ks = build_keystream(rc4_key)
    _apply_keystream(audio, ks)
    ext = sniff_ext(audio)
    if ext is None:
        raise ValueError(ERR_BAD_DECRYPT)
    return NcmContent(audio=audio, audio_ext=ext, meta=meta, cover=cover)


_CHUNK = 65536


def _apply_keystream(audio: bytearray, ks: bytes) -> None:
    """按 64KB 块做大整数 XOR（65536 为 256 字节密钥流周期的整数倍）。"""
    assert _CHUNK % len(ks) == 0
    mask_stream = ks * (_CHUNK // len(ks))
    for start in range(0, len(audio), _CHUNK):
        n = min(_CHUNK, len(audio) - start)
        block = int.from_bytes(audio[start : start + n], "big")
        mask = int.from_bytes(mask_stream[:n], "big")
        audio[start : start + n] = (block ^ mask).to_bytes(n, "big")


def decrypt_file(path) -> NcmContent:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ValueError(f"读取 NCM 文件失败: {e}") from e
    return decrypt_bytes(data)
