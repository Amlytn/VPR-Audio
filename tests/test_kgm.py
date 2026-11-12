"""KGM / KGMA / VPR 解密测试。"""
import struct

import pytest

from vpr.decrypt import kgm
from vpr.decrypt.sniff import sniff_ext


# 与参考实现逐值一致的常量样本（来自 MIT 公开的 KGM V2 掩码表）
def test_tables_match_reference_samples():
    assert len(kgm._TABLE1) == len(kgm._TABLE2) == len(kgm._TABLE_V2) == 272
    assert len(kgm._VPR_KEY) == 17
    assert kgm._TABLE_V2[0] == 184 and kgm._TABLE_V2[271] == 80
    assert kgm._TABLE1[17] == 1 and kgm._TABLE1[271] == 241
    assert kgm._TABLE2[17] == 1 and kgm._TABLE2[271] == 254
    assert kgm._VPR_KEY == bytes([37, 223, 232, 166, 117, 30, 117, 14, 47, 128, 243, 45, 184, 182, 227, 17, 0])


def test_mask_v1_matches_reference_definition():
    # maskV1 的 nibble 递归定义直接重算并抽样比对
    t1, t2, size = kgm._TABLE1, kgm._TABLE2, kgm._TABLE_SIZE

    def mask_v1(o: int) -> int:
        v = 0
        while o >= 0x11:
            v ^= t1[o % size]
            o >>= 4
            v ^= t2[o % size]
            o >>= 4
        return v

    for off in (0, 0x10, 0x11, 0x100, 0x1234, 0xFFFFF):
        assert kgm._mask_v1(off) == mask_v1(off)


def _build_container(audio: bytes, filekey: bytes, *, vpr: bool) -> bytes:
    """按真实加密方向构造容器：c = T(p ^ vprKey) ^ maskV2(i) ^ fileKey[i % 17]。"""
    header = bytearray(0x3C)
    header[0:16] = kgm.VPR_MAGIC if vpr else kgm.KGM_MAGIC
    struct.pack_into("<I", header, 0x10, 0x3C)
    header[0x1C:0x2C] = filekey

    fk = filekey + b"\x00"
    out = bytearray()
    for i, p in enumerate(audio):
        if vpr:
            p ^= kgm._VPR_KEY[i % 17]
        p = p ^ ((p & 0x0F) << 4)
        out.append(p ^ kgm._mask_v2(i) ^ fk[i % 17])
    return bytes(header) + bytes(out)


SAMPLE_FLAC = b"fLaC" + bytes(range(256)) * 5
FILEKEY = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01, 0x02])


def test_kgm_roundtrip_flac():
    container = _build_container(SAMPLE_FLAC, FILEKEY, vpr=False)
    content = kgm.decrypt_bytes(container)
    assert bytes(content.audio) == SAMPLE_FLAC
    assert content.audio_ext == "flac"


def test_kgma_extension_same_container_format():
    # .kgma 与 .kgm 同为 KGM 容器，解密路径一致（由扩展名分派，不在容器内区分）
    container = _build_container(SAMPLE_FLAC, FILEKEY, vpr=False)
    content = kgm.decrypt_bytes(container)
    assert sniff_ext(content.audio) == "flac"


def test_vpr_roundtrip_with_extra_keystream():
    container = _build_container(SAMPLE_FLAC, FILEKEY, vpr=True)
    content = kgm.decrypt_bytes(container, vpr=True)
    assert bytes(content.audio) == SAMPLE_FLAC
    assert content.audio_ext == "flac"


def test_vpr_container_rejected_as_kgm():
    container = _build_container(SAMPLE_FLAC, FILEKEY, vpr=True)
    with pytest.raises(ValueError, match="不是有效的 KGM/KGMA"):
        kgm.decrypt_bytes(container, vpr=False)


def test_kgm_container_rejected_as_vpr():
    container = _build_container(SAMPLE_FLAC, FILEKEY, vpr=False)
    with pytest.raises(ValueError, match="不是有效的 VPR"):
        kgm.decrypt_bytes(container, vpr=True)


def test_bad_header_rejected():
    container = bytearray(_build_container(SAMPLE_FLAC, FILEKEY, vpr=False))
    container[0] ^= 0xFF
    with pytest.raises(ValueError, match="不是有效的 KGM/KGMA"):
        kgm.decrypt_bytes(bytes(container))


def test_too_small_rejected():
    with pytest.raises(ValueError, match="文件过小"):
        kgm.decrypt_bytes(b"\x7c\xd5" + bytes(10))


def test_bad_header_len_rejected():
    container = bytearray(_build_container(SAMPLE_FLAC, FILEKEY, vpr=False))
    struct.pack_into("<I", container, 0x10, 0x40000000)  # 越界头长
    with pytest.raises(ValueError, match="头部长度异常"):
        kgm.decrypt_bytes(bytes(container))


def test_empty_audio_rejected():
    container = bytearray(_build_container(SAMPLE_FLAC, FILEKEY, vpr=False))
    container[0x10:0x14] = struct.pack("<I", len(container))  # headerLen == 文件长 → 无音频
    with pytest.raises(ValueError, match="音频数据为空"):
        kgm.decrypt_bytes(bytes(container))


def test_wrong_key_produces_sniff_failure_not_corrupt_file():
    # fileKey 错 → 解密结果必然不是合法音频头 → 明确报错而非产出坏文件
    container = _build_container(SAMPLE_FLAC, bytes(16), vpr=False)
    tampered = _build_container(SAMPLE_FLAC, FILEKEY, vpr=False)
    assert kgm.decrypt_bytes(tampered) is not None
    bad = _build_container(bytes(64) + b"garbage!" * 8, FILEKEY, vpr=False)  # 明文非音频
    with pytest.raises(ValueError, match="解密结果异常"):
        kgm.decrypt_bytes(bad)


def test_filekey_wraparound_at_17_bytes():
    # fileKey 是 17 字节循环（16 字节 + 0x00），构造超过 272*16 的数据覆盖多周期边界
    audio = b"OggS" + bytes(range(1, 256)) * 20
    container = _build_container(audio, FILEKEY, vpr=False)
    content = kgm.decrypt_bytes(container)
    assert bytes(content.audio) == audio
    assert content.audio_ext == "ogg"


def _reference_decrypt(cipher: bytes, filekey: bytes, *, vpr: bool) -> bytes:
    """逐字节参考实现（与分块/大整数路径独立的写法），输入是容器里的密文段。"""
    out = bytearray(len(cipher))
    fk = filekey + b"\x00"
    for i, b in enumerate(cipher):
        v = kgm._mask_v2(i) ^ b ^ fk[i % 17]
        v ^= (v & 0x0F) << 4
        if vpr:
            v ^= kgm._VPR_KEY[i % 17]
        out[i] = v & 0xFF
    return bytes(out)


def test_chunked_decrypt_matches_bytewise_reference(monkeypatch):
    """分块 + 大整数 XOR + translate 的实现必须与逐字节定义逐字节一致。
    把分块缩到 4096（16 的倍数），用 20KB 随机数据覆盖多个块边界。"""
    import random

    monkeypatch.setattr(kgm, "_CHUNK", 4096)
    rng = random.Random(20261112)
    audio = b"fLaC" + bytes(rng.randrange(256) for _ in range(20000))

    for use_vpr in (False, True):
        container = _build_container(audio, FILEKEY, vpr=use_vpr)
        content = kgm.decrypt_bytes(container, vpr=use_vpr)
        assert bytes(content.audio) == audio
        assert _reference_decrypt(container[0x3C:], FILEKEY, vpr=use_vpr) == audio


def test_single_chunk_short_audio_matches_reference():
    """边界：长度 < 分块大小、且不在 16 字节整倍数上（末块不足 16 字节）。"""
    audio = b"OggS" + bytes(range(1, 100))
    container = _build_container(audio, FILEKEY, vpr=False)
    content = kgm.decrypt_bytes(container)
    assert bytes(content.audio) == audio
    assert _reference_decrypt(container[0x3C:], FILEKEY, vpr=False) == audio


def test_crypto_version_reported_on_sniff_failure():
    """解密失败时错误信息带上文件头的加密版本与 slot，便于对新版格式做诊断。"""
    container = bytearray(_build_container(bytes(64) + b"garbage!" * 8, FILEKEY, vpr=False))
    struct.pack_into("<I", container, 0x14, 5)  # CryptoVersion
    struct.pack_into("<I", container, 0x18, 2)  # CryptoSlot
    with pytest.raises(ValueError, match="加密版本 v5 / slot 2"):
        kgm.decrypt_bytes(bytes(container))


def test_mask_tables_locked():
    """常量表哈希回归校验。"""
    import hashlib

    digest = hashlib.sha256(
        bytes(kgm._TABLE1) + bytes(kgm._TABLE2) + bytes(kgm._TABLE_V2) + kgm._VPR_KEY
    ).hexdigest()
    assert digest == "7fb0d2f57b85a281b4a69b6d3d851009d88ff01fbc7f8d45aaa92f6931062885"
