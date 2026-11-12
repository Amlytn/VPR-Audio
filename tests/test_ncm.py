"""NCM 解密测试。"""
import pytest

from vpr.decrypt import ncm
from vpr.decrypt.ncm import (
    CORE_KEY,
    KEY_PLAIN_HEADER,
    MAGIC,
    META_B64_PREFIX,
    META_KEY,
    build_keystream,
    decrypt_bytes,
)


def _aes_ecb_encrypt(key: bytes, data: bytearray) -> None:
    from Crypto.Cipher import AES

    aes = AES.new(key, AES.MODE_ECB)
    data[:] = aes.encrypt(bytes(data))


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def build_container(audio_plain: bytes, rc4_key: bytes, meta_json: str, cover: bytes) -> bytes:
    """按 NCM 容器格式构造测试样本"""
    # 密钥块：neteasecloudmusic + rc4_key → pad → AES → xor 0x64
    key_plain = KEY_PLAIN_HEADER + rc4_key
    key_block = bytearray(_pad(key_plain))
    _aes_ecb_encrypt(CORE_KEY, key_block)
    for i in range(len(key_block)):
        key_block[i] ^= 0x64
    # 元数据块：JSON 加 "music:" 前缀 → pad → AES → Base64 → 加明文前缀 → xor 0x63
    meta_plain = b"music:" + meta_json.encode()
    meta_cipher = bytearray(_pad(meta_plain))
    _aes_ecb_encrypt(META_KEY, meta_cipher)
    meta_block = bytearray(META_B64_PREFIX)
    import base64

    meta_block += base64.b64encode(bytes(meta_cipher))
    for i in range(len(meta_block)):
        meta_block[i] ^= 0x63
    # 音频体：同一密钥流异或（对称）
    ks = build_keystream(rc4_key)
    audio = bytearray(audio_plain)
    for i in range(len(audio)):
        audio[i] ^= ks[i & 0xFF]

    out = bytearray()
    out += MAGIC
    out += bytes([0x01, 0x70])
    out += len(key_block).to_bytes(4, "little")
    out += key_block
    out += len(meta_block).to_bytes(4, "little")
    out += meta_block
    out += bytes(4)  # crc
    out += bytes(5)  # unknown
    out += len(cover).to_bytes(4, "little")
    out += cover
    out += audio
    return bytes(out)


META_JSON = (
    '{"musicName":"夜曲","artist":[["周杰伦","6931"],["杨瑞代","123"]],'
    '"album":"十一月的萧邦","format":"flac","bitrate":1999000,"duration":263546}'
)


def fake_flac() -> bytes:
    return b"fLaC" + bytes(42)


def test_roundtrip_with_cover_and_metadata():
    cover = b"\xff\xd8fakejpegdata"
    container = build_container(fake_flac(), b"rc4-key-abc", META_JSON, cover)
    content = decrypt_bytes(container)
    assert bytes(content.audio) == fake_flac()
    assert content.audio_ext == "flac"
    assert content.meta is not None
    assert content.meta.title == "夜曲"
    assert content.meta.artist == "周杰伦 / 杨瑞代"
    assert content.meta.album == "十一月的萧邦"
    assert content.cover == cover


def test_roundtrip_no_cover_and_mp3_sniff():
    audio = b"ID3" + bytes(30)
    container = build_container(audio, b"k2", None if False else '{"musicName":"x"}', b"")
    content = decrypt_bytes(container)
    assert bytes(content.audio) == audio
    assert content.audio_ext == "mp3"
    assert content.cover is None


def test_bad_header_rejected():
    with pytest.raises(ValueError, match="不是有效的 NCM"):
        decrypt_bytes(b"NOTNCM!!" + bytes(64))


def test_tampered_key_block_rejected():
    container = bytearray(build_container(fake_flac(), b"k", META_JSON, b""))
    # 篡改密钥块中间字节 → 解不出合法前缀
    key_len = int.from_bytes(container[10:14], "little")
    container[14 + key_len // 2] ^= 0xFF
    with pytest.raises(ValueError, match="密钥块|解密结果异常"):
        decrypt_bytes(bytes(container))


def test_truncated_various_levels():
    full = build_container(fake_flac(), b"k", META_JSON, b"\x89PNG")
    with pytest.raises(ValueError, match="截断"):
        decrypt_bytes(full[:12])  # 截在头后
    with pytest.raises(ValueError, match="截断|密钥块|解密结果异常"):
        decrypt_bytes(full[: len(full) // 2])


def test_keystream_periodicity():
    # 256B 周期等价：对同一密钥，任意相隔 256 字节的密钥流字节相同
    ks = build_keystream(b"some-key")
    assert len(ks) == 256
    # 简单自洽：apply 两次还原（XOR 对称）
    audio = bytearray(b"fLaC" + bytes(1000))
    original = bytes(audio)
    ncm._apply_keystream(audio, ks)
    assert bytes(audio) != original
    ncm._apply_keystream(audio, ks)
    assert bytes(audio) == original


def test_apply_keystream_matches_naive():
    # 大块 XOR 实现必须与逐字节参照一致（含非 256 倍数长度、任意相位）
    ks = build_keystream(b"key-xyz")
    import os

    audio = bytearray(os.urandom(70000 + 137))
    expected = bytearray(audio)
    for i in range(len(expected)):
        expected[i] ^= ks[i & 0xFF]
    actual = bytearray(audio)
    ncm._apply_keystream(actual, ks)
    assert bytes(actual) == bytes(expected)
