"""酷狗 KGM / KGMA / VPR 解密（V2 XOR 掩码方案）。

    fileKey   = header[0x1C:0x2C] + 0x00        # 17 字节
    offset    = uint32 LE @ header[0x10]        # 音频数据起点
    out[i]    = T( maskV2(i) ^ audio[i] ^ fileKey[i % 17] )
    T(x)      = x ^ ((x & 0x0F) << 4)
    maskV2(i) = tableV2[i % 272] ^ maskV1(i >> 4)
    .vpr      = out[i] ^ vprKey[i % 17]

常量表（_TABLE1 / _TABLE2 / _TABLE_V2 / _VPR_KEY）取自 unlock-music / kugou-crypto（MIT）。
按 4MB 分块做大整数 XOR 合并，再查表完成 T 变换。仅覆盖 V2 掩码格式；
头部 0x14 为加密版本、0x18 为 key slot，新版 v3 / v5 不在此实现范围内。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .sniff import sniff_ext

KGM_MAGIC = bytes([0x7C, 0xD5, 0x32, 0xEB, 0x86, 0x02, 0x7F, 0x4B,
                   0xA8, 0xAF, 0xA6, 0x8E, 0x0F, 0xFF, 0x99, 0x14])
VPR_MAGIC = bytes([0x05, 0x28, 0xBC, 0x96, 0xE9, 0xE4, 0x5A, 0x43,
                   0x91, 0xAA, 0xBD, 0xD0, 0x7A, 0xF5, 0x36, 0x31])

_TABLE_SIZE = 272

_TABLE1 = [
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 1, 33, 1, 97, 1, 33, 1, 225, 1, 33, 1, 97, 1, 33, 1,
    210, 35, 2, 2, 66, 66, 2, 2, 194, 194, 2, 2, 66, 66, 2, 2,
    211, 211, 2, 3, 99, 67, 99, 3, 227, 195, 227, 3, 99, 67, 99, 3,
    148, 180, 148, 101, 4, 4, 4, 4, 132, 132, 132, 132, 4, 4, 4, 4,
    149, 149, 149, 149, 4, 5, 37, 5, 229, 133, 165, 133, 229, 5, 37, 5,
    214, 182, 150, 182, 214, 39, 6, 6, 198, 198, 134, 134, 198, 198, 6, 6,
    215, 215, 151, 151, 215, 215, 6, 7, 231, 199, 231, 135, 231, 199, 231, 7,
    24, 56, 24, 120, 24, 56, 24, 233, 8, 8, 8, 8, 8, 8, 8, 8,
    25, 25, 25, 25, 25, 25, 25, 25, 8, 9, 41, 9, 105, 9, 41, 9,
    218, 58, 26, 58, 90, 58, 26, 58, 218, 43, 10, 10, 74, 74, 10, 10,
    219, 219, 27, 27, 91, 91, 27, 27, 219, 219, 10, 11, 107, 75, 107, 11,
    156, 188, 156, 124, 28, 60, 28, 124, 156, 188, 156, 109, 12, 12, 12, 12,
    157, 157, 157, 157, 29, 29, 29, 29, 157, 157, 157, 157, 12, 13, 45, 13,
    222, 190, 158, 190, 222, 62, 30, 62, 222, 190, 158, 190, 222, 47, 14, 14,
    223, 223, 159, 159, 223, 223, 31, 31, 223, 223, 159, 159, 223, 223, 14, 15,
    0, 32, 0, 96, 0, 32, 0, 224, 0, 32, 0, 96, 0, 32, 0, 241,
]

_TABLE2 = [
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 1, 35, 1, 103, 1, 35, 1, 239, 1, 35, 1, 103, 1, 35, 1,
    223, 33, 2, 2, 70, 70, 2, 2, 206, 206, 2, 2, 70, 70, 2, 2,
    222, 222, 2, 3, 101, 71, 101, 3, 237, 207, 237, 3, 101, 71, 101, 3,
    157, 191, 157, 99, 4, 4, 4, 4, 140, 140, 140, 140, 4, 4, 4, 4,
    156, 156, 156, 156, 4, 5, 39, 5, 235, 141, 175, 141, 235, 5, 39, 5,
    219, 189, 159, 189, 219, 37, 6, 6, 202, 202, 142, 142, 202, 202, 6, 6,
    218, 218, 158, 158, 218, 218, 6, 7, 233, 203, 233, 143, 233, 203, 233, 7,
    25, 59, 25, 127, 25, 59, 25, 231, 8, 8, 8, 8, 8, 8, 8, 8,
    24, 24, 24, 24, 24, 24, 24, 24, 8, 9, 43, 9, 111, 9, 43, 9,
    215, 57, 27, 57, 95, 57, 27, 57, 215, 41, 10, 10, 78, 78, 10, 10,
    214, 214, 26, 26, 94, 94, 26, 26, 214, 214, 10, 11, 109, 79, 109, 11,
    149, 183, 149, 123, 29, 63, 29, 123, 149, 183, 149, 107, 12, 12, 12, 12,
    148, 148, 148, 148, 28, 28, 28, 28, 148, 148, 148, 148, 12, 13, 47, 13,
    211, 181, 151, 181, 211, 61, 31, 61, 211, 181, 151, 181, 211, 45, 14, 14,
    210, 210, 150, 150, 210, 210, 30, 30, 210, 210, 150, 150, 210, 210, 14, 15,
    0, 34, 0, 102, 0, 34, 0, 238, 0, 34, 0, 102, 0, 34, 0, 254,
]

_TABLE_V2 = [
    184, 213, 61, 178, 233, 175, 120, 140, 131, 51, 113, 81, 118, 160, 205, 55,
    47, 62, 53, 141, 169, 190, 152, 183, 231, 140, 34, 206, 90, 97, 223, 104,
    105, 137, 254, 165, 182, 222, 169, 119, 252, 200, 189, 189, 229, 109, 62, 90,
    54, 239, 105, 78, 190, 225, 233, 102, 28, 243, 217, 2, 182, 242, 18, 155,
    68, 208, 111, 185, 53, 137, 182, 70, 109, 115, 130, 6, 105, 193, 237, 215,
    133, 194, 48, 223, 162, 98, 190, 121, 45, 98, 98, 61, 13, 126, 190, 72,
    137, 35, 2, 160, 228, 213, 117, 81, 50, 2, 83, 253, 22, 58, 33, 59,
    22, 15, 195, 178, 187, 179, 226, 186, 58, 61, 19, 236, 246, 1, 69, 132,
    165, 112, 15, 147, 73, 12, 100, 205, 49, 213, 204, 76, 7, 1, 158, 0,
    26, 35, 144, 191, 136, 30, 59, 171, 166, 62, 196, 115, 71, 16, 126, 59,
    94, 188, 227, 0, 132, 255, 9, 212, 224, 137, 15, 91, 88, 112, 79, 251,
    101, 216, 92, 83, 27, 211, 200, 198, 191, 239, 152, 176, 80, 79, 15, 234,
    229, 131, 88, 140, 40, 44, 132, 103, 205, 208, 158, 71, 219, 39, 80, 202,
    244, 99, 99, 232, 151, 127, 27, 75, 12, 194, 193, 33, 76, 204, 88, 245,
    148, 82, 163, 243, 211, 224, 104, 244, 0, 35, 243, 94, 10, 123, 147, 221,
    171, 18, 178, 19, 232, 132, 215, 167, 159, 15, 50, 76, 85, 29, 4, 54,
    82, 220, 3, 243, 249, 78, 66, 233, 61, 97, 239, 124, 182, 179, 147, 80,
]

_VPR_KEY = bytes([37, 223, 232, 166, 117, 30, 117, 14, 47, 128, 243, 45, 184, 182, 227, 17, 0])


@dataclass
class KgmDecoded:
    audio: bytearray
    audio_ext: str


def _mask_v1(offset: int) -> int:
    value = 0
    while offset >= 0x11:
        value ^= _TABLE1[offset % _TABLE_SIZE]
        offset >>= 4
        value ^= _TABLE2[offset % _TABLE_SIZE]
        offset >>= 4
    return value


def _mask_v2(offset: int) -> int:
    return _TABLE_V2[offset % _TABLE_SIZE] ^ _mask_v1(offset >> 4)


# T 变换逐字节独立（T(x) = x ^ ((x & 0x0F) << 4)，GF(2) 线性），可用 256 项查表一次完成
_T_BYTE = bytes((x ^ ((x & 0x0F) << 4)) & 0xFF for x in range(256))
_TABLE_V2_BYTES = bytes(_TABLE_V2)
# 分块大小必须是 16 的倍数：maskV1(i >> 4) 在一个 16 字节块内恒定，块边界才能对齐
_CHUNK = 4 << 20


def _cycle(stream: bytes, start: int, length: int) -> bytes:
    """取 stream 从 start 起的循环小段（fileKey / vprKey 都是 17 字节循环）"""
    off = start % len(stream)
    rotated = stream[off:] + stream[:off]
    return (rotated * (length // len(stream) + 2))[:length]


def _mask_chunk(base: int, length: int) -> tuple[bytes, bytearray]:
    """构造 [base, base+length) 的掩码流：tableV2 循环 + 每 16 字节的 maskV1 叠加量。

    叠加量单独返回：调用方用大整数 XOR 合并，避免逐字节 Python 运算。
    只有 maskV1 需要每 16 字节算一次（这是唯一无法向量化的部分）。"""
    off = base % _TABLE_SIZE
    rotated = _TABLE_V2_BYTES[off:] + _TABLE_V2_BYTES[:off]
    mask = (rotated * (length // _TABLE_SIZE + 2))[:length]
    overlay = bytearray(length)
    for start in range(0, length, 16):
        value = _mask_v1((base + start) >> 4)
        if value:
            overlay[start : start + 16] = bytes((value,)) * min(16, length - start)
    return mask, overlay


def decrypt_bytes(data: bytes | bytearray | memoryview, *, vpr: bool = False) -> KgmDecoded:
    """解密 KGM/KGMA/VPR 容器字节。vpr=True 时启用 VPR 附加密钥流。

    头部魔数不符、头部长度越界、解密结果嗅探失败均抛 ValueError。
    """
    buf = bytes(data)
    if len(buf) < 0x3C:
        raise ValueError("文件过小，不是有效的 KGM/KGMA/VPR 容器")

    is_vpr = buf.startswith(VPR_MAGIC)
    if vpr and not is_vpr:
        raise ValueError("不是有效的 VPR 文件（头部魔数不符）")
    if not vpr and not buf.startswith(KGM_MAGIC):
        raise ValueError("不是有效的 KGM/KGMA 文件（头部魔数不符）")

    filekey = buf[0x1C:0x2C] + b"\x00"
    header_len = struct.unpack_from("<I", buf, 0x10)[0]
    crypto_version = struct.unpack_from("<I", buf, 0x14)[0]
    crypto_slot = struct.unpack_from("<I", buf, 0x18)[0]
    if header_len < 0x3C or header_len > len(buf):
        raise ValueError("头部长度异常，文件已损坏或不是 KGM 容器")
    audio = bytearray(buf[header_len:])
    if not audio:
        raise ValueError("音频数据为空，文件已损坏")

    # XOR 是对合运算：解密与加密用同一变换。分块后每块一次性合并
    # 掩码/密钥流/密文（大整数 XOR），再查表完成 T——输出与逐字节循环逐字节一致。
    # 注意顺序：T 先于 VPR 附加密钥流（加密方向是 先 ^vprKey → T → ^mask → ^fileKey）。
    total = len(audio)
    for base in range(0, total, _CHUNK):
        length = min(_CHUNK, total - base)
        mask, overlay = _mask_chunk(base, length)
        mixed = int.from_bytes(audio[base : base + length], "big")
        mixed ^= int.from_bytes(mask, "big")
        mixed ^= int.from_bytes(overlay, "big")
        mixed ^= int.from_bytes(_cycle(filekey, base, length), "big")
        chunk = mixed.to_bytes(length, "big").translate(_T_BYTE)
        if is_vpr:
            key = int.from_bytes(_cycle(_VPR_KEY, base, length), "big")
            chunk = (int.from_bytes(chunk, "big") ^ key).to_bytes(length, "big")
        audio[base : base + length] = chunk

    ext = sniff_ext(audio)
    if ext is None:
        raise ValueError(
            "解密结果异常（裸流魔数无法识别），密钥不匹配或文件已损坏"
            f"；文件头标注加密版本 v{crypto_version} / slot {crypto_slot}，"
            "本实现只覆盖旧版 V2 掩码方案（新版 KGM 可能是 MD5 box 或 KGMusicV3.db 密钥格式）"
        )
    return KgmDecoded(audio=audio, audio_ext=ext)
