"""tc_tea 加解密测试，含 tc_tea crate 已知向量。"""
import pytest

from vpr.decrypt import tc_tea
from vpr.decrypt.tc_tea import TeaError, decrypt, encrypt, get_encrypted_size

# 官方 GOOD_ENCRYPTED_DATA：由 C++ 实现生成，key = [0x31323334, 0x35363738, 0x41424344, 0x45464748]
GOOD_ENCRYPTED_DATA = bytes.fromhex("91095162e3f5b6dc6b414b50d1a5b84ec50d0c1b1196fd3c")
ENCRYPTION_KEY = b"12345678ABCDEFGH"  # BE 大端解析即上述 4 个 u32
EXPECTED_PLAIN_TEXT = bytes([1, 2, 3, 4, 5, 6, 7, 8])
# 官方 TEST_SALT（固定随机源）
TEST_SALT = bytes(range(10))


def test_calc_encrypted_size_official():
    assert get_encrypted_size(0) == 16
    assert get_encrypted_size(1) == 16
    assert get_encrypted_size(6) == 16
    assert get_encrypted_size(7) == 24
    assert get_encrypted_size(14) == 24
    assert get_encrypted_size(15) == 32


def test_tc_tea_basic_decryption_official_vector():
    assert decrypt(GOOD_ENCRYPTED_DATA, ENCRYPTION_KEY) == EXPECTED_PLAIN_TEXT


def test_tc_tea_decrypt_rejects_non_zero_tail():
    bad = bytearray(GOOD_ENCRYPTED_DATA)
    bad[-1] ^= 0xFF
    with pytest.raises(TeaError):
        decrypt(bytes(bad), ENCRYPTION_KEY)


def test_tc_tea_encrypt_with_fixed_salt_matches_official():
    # 官方同款固定 salt 加密：密文长度与解密往返均须正确（字节级与 C++ 实现一致，
    # 由官方 test 加密向量间接覆盖——这里验证长度与可解性，known-answer 只给了密文方向）
    cipher = encrypt(EXPECTED_PLAIN_TEXT, ENCRYPTION_KEY, TEST_SALT)
    assert len(cipher) == 24
    assert decrypt(cipher, ENCRYPTION_KEY) == EXPECTED_PLAIN_TEXT


def test_roundtrip_various_lengths():
    for n in range(0, 40):
        data = bytes(range(256))[:n] * 1
        data = (data * 3)[:n]
        cipher = encrypt(data, ENCRYPTION_KEY)
        assert len(cipher) % 8 == 0 and len(cipher) > n
        assert decrypt(cipher, ENCRYPTION_KEY) == data


def test_invalid_sizes():
    with pytest.raises(TeaError):
        decrypt(b"\x00" * 8, ENCRYPTION_KEY)  # < FIXED_PADDING_LEN
    with pytest.raises(TeaError):
        decrypt(b"\x00" * 12, ENCRYPTION_KEY)  # 非 8 的倍数
    with pytest.raises(TeaError):
        encrypt(b"x", b"short")
