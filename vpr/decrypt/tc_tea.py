"""Tencent modified TEA（Tars-CBC）加解密，对齐 tc_tea crate 0.2.1。

- 32 轮 TEA（rounds=16）；
- CBC 双 IV 交织：iv2' = plain ^ iv1，cipher = ECB(iv2') ^ iv2；
- 打包：1(pad) + pad + 2(salt) + body + 7(zero)，首字节低 3 位为 pad 长度；
- 解密校验尾部 7 字节全零。
"""
from __future__ import annotations

import secrets

_DELTA = 0x9E3779B9
_MASK32 = 0xFFFFFFFF

SALT_LEN = 2
ZERO_LEN = 7
FIXED_PADDING_LEN = 1 + SALT_LEN + ZERO_LEN


def get_encrypted_size(body_size: int) -> int:
    """官方 get_encrypted_size：len = 10 + body；pad = (8 - len&7) & 7；返回 len+pad"""
    length = FIXED_PADDING_LEN + body_size
    pad_len = (8 - (length & 0b0111)) & 0b0111
    return length + pad_len


class TeaError(Exception):
    """TEA 解密失败（密钥不匹配 / 数据损坏）"""


def _key_words(key: bytes) -> tuple[int, int, int, int]:
    if len(key) != 16:
        raise TeaError("TEA 密钥必须为 16 字节")
    return tuple(  # type: ignore[return-value]
        int.from_bytes(key[i * 4 : i * 4 + 4], "big") for i in range(4)
    )


def _ecb_encrypt(block_u64: int, k) -> int:
    v0 = (block_u64 >> 32) & _MASK32
    v1 = block_u64 & _MASK32
    s = 0
    for _ in range(16):
        # 官方：sum 先加 DELTA，两个半轮共用同一个 sum
        s = (s + _DELTA) & _MASK32
        v0 = (v0 + ((((v1 << 4) + k[0]) & _MASK32) ^ ((s + v1) & _MASK32) ^ (((v1 >> 5) + k[1]) & _MASK32))) & _MASK32
        v1 = (v1 + ((((v0 << 4) + k[2]) & _MASK32) ^ ((s + v0) & _MASK32) ^ (((v0 >> 5) + k[3]) & _MASK32))) & _MASK32
    return (v0 << 32) | v1


def _ecb_decrypt(block_u64: int, k) -> int:
    v0 = (block_u64 >> 32) & _MASK32
    v1 = block_u64 & _MASK32
    s = (_DELTA * 16) & _MASK32
    for _ in range(16):
        v1 = (v1 - ((((v0 << 4) + k[2]) & _MASK32) ^ ((s + v0) & _MASK32) ^ (((v0 >> 5) + k[3]) & _MASK32))) & _MASK32
        v0 = (v0 - ((((v1 << 4) + k[0]) & _MASK32) ^ ((s + v1) & _MASK32) ^ (((v1 >> 5) + k[1]) & _MASK32))) & _MASK32
        s = (s - _DELTA) & _MASK32
    return (v0 << 32) | v1


def decrypt(data: bytes, key: bytes) -> bytes:
    """tc_tea CBC 解密。数据必须为 8 的倍数且 ≥10 字节；校验失败抛 TeaError。

    对应官方 cbc::decrypt：双 IV 交织链 + 尾部 7 字节 zero 校验。
    """
    k = _key_words(key)
    if len(data) < FIXED_PADDING_LEN or len(data) % 8 != 0:
        raise TeaError("TEA 密文长度非法")

    plain = bytearray(len(data))
    iv1 = 0
    iv2 = 0
    for i in range(0, len(data), 8):
        cipher_block = int.from_bytes(data[i : i + 8], "big")
        result = cipher_block ^ iv2
        next_iv2 = _ecb_decrypt(result, k)
        plain_block = next_iv2 ^ iv1
        plain[i : i + 8] = plain_block.to_bytes(8, "big")
        iv1 = cipher_block
        iv2 = next_iv2

    pad_size = plain[0] & 0b111
    start_loc = 1 + pad_size + SALT_LEN
    end_loc = len(plain) - ZERO_LEN
    if any(plain[end_loc:]):
        raise TeaError("TEA 解密校验失败（尾部 zero 字节不对）")
    return bytes(plain[start_loc:end_loc])


def encrypt(data: bytes, key: bytes, salt: bytes | None = None) -> bytes:
    """tc_tea CBC 加密。`salt` 供测试固定随机源（官方 TEST_SALT 语义），缺省随机。"""
    k = _key_words(key)
    if salt is None:
        salt = secrets.token_bytes(10)

    length = FIXED_PADDING_LEN + len(data)
    pad_len = (8 - (length & 0b0111)) & 0b0111
    header_len = 1 + pad_len + SALT_LEN
    expected_output_len = length + pad_len

    # header = salt 前 header_len 字节，首字节低 3 位写 pad_len；后接明文头部
    header = bytearray(16)
    header[:header_len] = salt[:header_len]
    header[0] = (header[0] & 0b11111000) | pad_len
    copy_to_header_len = min(16 - header_len, len(data))
    header[header_len : header_len + copy_to_header_len] = data[:copy_to_header_len]
    rest = data[copy_to_header_len:]

    out = bytearray()
    iv1 = 0
    iv2 = 0

    def encrypt_round(plain_block: bytes) -> None:
        nonlocal iv1, iv2
        plain_u64 = int.from_bytes(plain_block, "big")
        iv2_next = plain_u64 ^ iv1
        result = _ecb_encrypt(iv2_next, k)
        cipher_block = result ^ iv2
        out.extend(cipher_block.to_bytes(8, "big"))
        iv1 = cipher_block
        iv2 = iv2_next

    encrypt_round(bytes(header[:8]))
    encrypt_round(bytes(header[8:16]))
    for i in range(0, len(rest) - len(rest) % 8, 8):
        encrypt_round(rest[i : i + 8])
    tail_len = len(rest) % 8
    if tail_len:
        encrypt_round(rest[len(rest) - tail_len :].ljust(8, b"\x00"))

    return bytes(out)
