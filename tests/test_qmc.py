"""QMC 解密测试 —— 全部向量来自 unlock-music 官方测试数据（MIT），逐字节核验。"""
import base64

import pytest

from vpr.decrypt import qmc, tc_tea
from vpr.decrypt.qmc import (
    EKEY_V2_KEY1,
    EKEY_V2_KEY2,
    EKEY_V2_PREFIX,
    Footer,
    QmcCipher,
    QmcV2Map,
    QmcV2Rc4,
    derive_key,
    get_segment_key,
    key_compress,
    parse_footer,
    qmc_hash,
)


def gen_key(length: int) -> bytes:
    return bytes(i & 0xFF for i in range(1, length + 1))


def test_simple_make_key_matches_official():
    from vpr.decrypt.qmc import _make_simple_key

    assert _make_simple_key(8) == bytes([0x69, 0x56, 0x46, 0x38, 0x2B, 0x20, 0x15, 0x0B])


def test_hash_matches_official():
    assert qmc_hash(b"hello world") == 4045008896.0


def test_key_compress_matches_official():
    # 官方 v2_map/key.rs test_compress：325B 循环 key → 128B 期望
    key = (bytes(range(ord("a"), ord("z") + 1)) + bytes(range(ord("A"), ord("Z") + 1)) + b"0123456789")
    key = (key * 10)[:325]
    expected = bytes.fromhex(
        "79f400759e3600148a6300b4be770017ba0037000000bf8041bf83dd"
        "bc5c0243148249020055be6dbf49808e4300fa4167a817f4ae161500"
        "c13782dd362138550079419e42c136facf35000041dd4342174d8e8a"
        "dd00bef538b4bf007acc4d0200cfc1c102a80016c1bfc242004900c1"
        "c2f5001741dc83c2009e41c171360080"
    )
    assert key_compress(key) == expected


def test_map_decrypt_matches_official_vector():
    # 官方 v2_map/mod.rs test_decrypt：offset 32760 处 16B 密文 → 全零
    key = (bytes(range(ord("a"), ord("z") + 1)) + bytes(range(ord("A"), ord("Z") + 1)) + b"0123456789")
    key = (key * 10)[:325]
    cipher = QmcV2Map(key)
    data = bytearray(
        bytes.fromhex("009e41c171360080f400759e360014 8a".replace(" ", ""))
    )
    cipher.decrypt(data, 32760)
    assert data == bytearray(16)


def test_rc4_decrypt_matches_official_vector():
    # 官方 v2_rc4/cipher.rs test_qmc2_header：256B 密文 → 全零
    full = [
        0x39, 0x5A, 0x4F, 0x75, 0x38, 0x71, 0x37, 0x6B, 0x36, 0x51, 0x53, 0x6D, 0x7A, 0x66,
        0x53, 0x4B, 0x66, 0x50, 0x69, 0x34, 0x67, 0x6C, 0x33, 0x7A, 0x55, 0x62, 0x35, 0x5A,
        0x32, 0x75, 0x4F, 0x68, 0x44, 0x52, 0x6D, 0x65, 0x75, 0x6E, 0x39, 0x52, 0x30, 0x7A,
        0x68, 0x62, 0x73, 0x59, 0x39, 0x48, 0x55, 0x57, 0x73, 0x32, 0x5A, 0x70, 0x64, 0x50,
        0x4E, 0x52, 0x6A, 0x63, 0x4D, 0x39, 0x37, 0x76, 0x72, 0x47, 0x64, 0x4D, 0x62, 0x6D,
        0x58, 0x68, 0x75, 0x47, 0x37, 0x56, 0x69, 0x6B, 0x4A, 0x79, 0x66, 0x63, 0x70, 0x39,
        0x59, 0x34, 0x43, 0x6B, 0x45, 0x32, 0x5A, 0x31, 0x38, 0x77, 0x70, 0x43, 0x51, 0x79,
        0x6A, 0x62, 0x32, 0x33, 0x65, 0x58, 0x4A, 0x4D, 0x33, 0x4E, 0x70, 0x62, 0x62, 0x67,
        0x4C, 0x54, 0x78, 0x64, 0x64, 0x77, 0x6E, 0x72, 0x37, 0x41, 0x54, 0x39, 0x42, 0x52,
        0x47, 0x32, 0x1A, 0xE4, 0x1B, 0x71, 0x68, 0x29, 0xB3, 0x6E, 0xAD, 0xC5, 0x28, 0x12,
        0xD6, 0xA4, 0x4B, 0x06, 0x7A, 0xDC, 0x90, 0x15, 0x99, 0xD6, 0xBF, 0x72, 0xA2, 0x30,
        0x37, 0x6B, 0x5C, 0xD6, 0x2F, 0x35, 0x14, 0x8A, 0xD6, 0xFB, 0x9F, 0xEE, 0x7D, 0x2D,
        0xB7, 0x37, 0xF2, 0x0B, 0x6E, 0x00, 0xFB, 0xA0, 0x3C, 0x40, 0xF3, 0x36, 0xB2, 0x76,
        0x20, 0x0F, 0x9E, 0xA5, 0xA3, 0x15, 0x60, 0x23, 0x15, 0x29, 0xA1, 0x91, 0xBF, 0xFB,
        0x12, 0x95, 0xAA, 0x8D, 0x92, 0xC6, 0x0B, 0x8D, 0x49, 0x99, 0xA5, 0xE0, 0x05, 0xCF,
        0xB6, 0xAC, 0x07, 0x54, 0x58, 0x28, 0xF9, 0x96, 0xD1, 0x9A, 0xFE, 0x0B, 0x3C, 0xFB,
        0x0B, 0x25, 0x7A, 0x43, 0x5A, 0x33, 0xC3, 0x7A, 0xFC, 0x33, 0xA3, 0xC2, 0x65, 0x48,
        0x29, 0x8D, 0x2C, 0x8F, 0x4E, 0x88, 0xFD, 0x44, 0xFD, 0xD5, 0xCA, 0xB9, 0x8D, 0x62,
        0x4A, 0x48, 0x20, 0x1D,
    ]
    data = bytearray(full)
    key = (bytes(range(ord("a"), ord("z") + 1)) + bytes(range(ord("A"), ord("Z") + 1)) + b"0123456789")
    key = (key * 30)[:512]
    QmcV2Rc4(key).decrypt(data, 0)
    assert data == bytearray(256)


def test_get_segment_key_seed_zero():
    assert get_segment_key(5, 0, 100.0) == 0


def _unhex(s: str) -> bytes:
    return bytes.fromhex("".join(s.split()))


def test_footer_qtag_official_fixture():
    data = _unhex(
        "5b64756d6d792020646174615d3030313132323333614263442b2f3d2c333236"
        "3435343330312c320000001b51546167"
    )
    footer = parse_footer(data)
    assert footer is not None
    assert footer.size == 0x23
    assert footer.ekey == "00112233aBcD+/="
    assert footer.kind == "qtag"
    assert footer.resource_id == 326454301


def test_footer_stag_official_fixture():
    data = _unhex(
        "5b64756d6d792020202020646174615d353137373738352c322c303031793743615232396b365950"
        "0000001853546167"
    )
    footer = parse_footer(data)
    assert footer is not None
    assert footer.size == 0x20
    assert footer.ekey is None
    assert footer.kind == "stag"
    assert footer.media_mid == "001y7CaR29k6YP"


def test_footer_pc_v1_official_fixture():
    data = _unhex(
        "5b64756d6d795f646174615d4e555a3662306c61643262666f4b494f684d6c6e6d74447474675767"
        "6f327165416d554963543772416834456d5a4f664f50796e76722b782b72694d507451546a4f6437"
        "78794f624d4a334c33626337746576656e6b68507056525a51306d4e45504b4d6a4b66784c4d5242"
        "76446b393173596b74393038706a485759453872566d2b736d562f362b5a48525347774743414831"
        "2f494f4232536e4a364751564d685a324b4547516c504a52727a6e5a5a5631584476494a3534734e"
        "7439777651696551527a6a655a62387a6768665954506d4f546151315a4e454c56344d386c506546"
        "576f65356a76554979616b4762374767496d304a47474578444a7374355655332b444d484f387133"
        "35497456494242722b2f4946396149374b434b5a4679354f75797159617a36774e2f6e647a624c35"
        "35476c5838356477594c36706942483034495a786a335a7a43716e69797373523433473856476432"
        "447877345a794c706e676a5072702f55386f6a7a795865356b4f41663165675073697a366d4f2b57"
        "30417469642b366a705a79623666503451676446736b63705648616157313737472f565967643334"
        "464f6b556f4a71306e4d33502b6d327757703159446b644972734e5a4e674e6b554d75467937596e"
        "706a655264384b6f6b456a513834326853646578316c674a4c3932642b706b54384461336a314776"
        "59306e48453630664f4c69655838494f68624d4a6565716b70326e6a666866434b5145664e442f36"
        "49784a523241665862526a30796e776d716152506437776836304b593548476a716e70346a585652"
        "4b4856506c4b7035422f7532344c2b4c447a6143534c427841587362364a5a53766e456f30503230"
        "327a72464654656d2f454474536e372b6a76376452704f664c5376382b4e4741675272545a423149"
        "4d42586359506152703067382f6a31667179442f4d356863516376705749577257417965c0020000"
    )
    footer = parse_footer(data)
    assert footer is not None
    assert footer.size == 0x2C4
    assert footer.ekey is not None and footer.ekey.startswith("NUZ6b0la")
    assert footer.kind == "pc_v1_legacy"


def test_footer_musicex_official_fixture():
    data = _unhex(
        "5b64756d6d792020202020646174615d01010101020202020303030341006100"
        "4200620043006300440064004500650046006600470067000000000000000000"
        "000000000000000000000000000000000000000000000000460030004d003000"
        "300030003100310032003200330033003400340035003500360036002e006d00"
        "66006c0061006300000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000004040404"
        "c0000000010000006d75736963657800"
    )
    footer = parse_footer(data)
    assert footer is not None
    assert footer.size == 0xC0
    assert footer.ekey is None
    assert footer.kind == "musicex"
    assert footer.media_filename == "F0M000112233445566.mflac"


def test_footer_no_tail_falls_back_to_none():
    data = b"fLaCxxxxxxxx" + bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
    assert parse_footer(data) is None


# ============================================================================
# ekey 往返
# ============================================================================


def _make_v1_ekey(key: bytes, header_seed: int) -> str:
    """构造 v1 ekey 的加密侧（仅测试）：header 8B 与 simpleKey 交织成 TEA 密钥"""
    from vpr.decrypt.qmc import _make_simple_key

    header = bytes((header_seed + i) & 0xFF for i in range(8))
    simple = _make_simple_key(8)
    tea_key = bytes(b for pair in zip(simple, header) for b in pair)
    cipher = tc_tea.encrypt(key[8:], tea_key)
    return base64.b64encode(header + cipher).decode()


def test_ekey_roundtrip_v1_and_encv2():
    key = bytes(range(0x10, 0x18)) + b"\x42" * 248
    v1_ekey = _make_v1_ekey(key, 0x10)
    assert derive_key(v1_ekey.encode()) == key

    # EncV2：v1 解码原文 → b64 → TEA(K2) → TEA(K1) → 前缀+密文整体 base64
    decoded_v1 = base64.b64decode(v1_ekey)
    inner_b64 = base64.b64encode(decoded_v1)
    step1 = tc_tea.encrypt(inner_b64, EKEY_V2_KEY2)
    step2 = tc_tea.encrypt(step1, EKEY_V2_KEY1)
    encv2_ekey = base64.b64encode(EKEY_V2_PREFIX + step2).decode()
    assert derive_key(encv2_ekey.encode()) == key

    # 无效 ekey：不崩溃、报错可读
    with pytest.raises(ValueError):
        derive_key(b"not-base64!!")
    with pytest.raises(ValueError):
        derive_key(b"QUJD")


# ============================================================================
# decrypt_bytes 集成
# ============================================================================


def test_decrypt_bytes_static_flac():
    raw = b"fLaC" + b"\x00" * 60
    encrypted = bytearray(raw)
    QmcCipher(b"").decrypt(encrypted, 0)
    decoded = qmc.decrypt_bytes(bytes(encrypted))
    assert bytes(decoded.audio) == raw
    assert decoded.audio_ext == "flac"
    assert decoded.footer is None


def test_decrypt_bytes_stag_reports_no_key():
    raw = b"fLaC" + b"\x00" * 64
    encrypted = bytearray(raw)
    QmcCipher(b"").decrypt(encrypted, 0)
    encrypted += _unhex("353137373738352c322c303031793743615232396b3659500000001853546167")
    with pytest.raises(ValueError, match="不含解密密钥"):
        qmc.decrypt_bytes(bytes(encrypted))


def test_decrypt_bytes_musicex_with_external_ekey():
    key = bytes(range(0x30, 0x38)) + b"\x57" * 248
    raw = b"fLaC" + b"\x5A" * 96

    tail = bytearray(0xB0)
    tail += (0xC0).to_bytes(4, "little")
    tail += (1).to_bytes(4, "little")
    tail += b"musicex\x00"

    encrypted = bytearray(raw)
    QmcCipher(b"").decrypt(encrypted, 0)
    encrypted += tail
    with pytest.raises(ValueError, match="不含解密密钥"):
        qmc.decrypt_bytes(bytes(encrypted))

    # 提供外部 ekey（Map 密钥 256B）→ 解回原文
    v1_ekey = _make_v1_ekey(key, 0x30)
    cipher = QmcCipher(key)
    assert cipher._impl == "map"
    encrypted_v2 = bytearray(raw)
    cipher.decrypt(encrypted_v2, 0)
    encrypted_v2 += tail
    decoded = qmc.decrypt_bytes(bytes(encrypted_v2), v1_ekey)
    assert bytes(decoded.audio) == raw
    assert decoded.audio_ext == "flac"
    assert decoded.footer is not None and decoded.footer.kind == "musicex"
    assert decoded.footer.media_filename == ""


def test_derive_key_bad_ekey_raises_value_error():
    # TEA 校验失败需统一转成 ValueError，避免候选池试解中断
    bad = base64.b64encode(bytes(16)).decode()
    with pytest.raises(ValueError, match="TEA"):
        derive_key(bad.encode())


def test_decrypt_static_wav_is_recognized():
    # 回归：bkcwav 解出 RIFF/WAVE 时不应误报「解密结果异常」
    raw = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"fmt " + bytes(64)
    encrypted = bytearray(raw)
    QmcCipher(b"").decrypt(encrypted, 0)
    decoded = qmc.decrypt_bytes(bytes(encrypted))
    assert bytes(decoded.audio) == raw
    assert decoded.audio_ext == "wav"
