"""QmcKeySource 测试 + mmkv 补充测试。"""
from vpr.decrypt import mmkv, qmc_keydb
from vpr.decrypt.qmc_keydb import QmcKeySource, basename_of, stem_of


def test_basename_of_variants():
    assert basename_of("/storage/emulated/0/Music/song.mflac") == "song.mflac"
    assert basename_of("C:\\Music\\song.mflac") == "song.mflac"
    assert basename_of("song.mflac") == "song.mflac"
    assert basename_of("  song.mflac ") == "song.mflac"


def test_stem_of():
    assert stem_of("song.mflac") == "song"
    assert stem_of(".hidden") == ".hidden"  # 点在首位原样
    assert stem_of("nodot") == "nodot"


def test_manual_text_parsing_named_and_pool():
    src = QmcKeySource.empty()
    src.set_manual_text(
        "# 注释行\n"
        "song.mflac = AbCdEf1234567890+/\n"
        "F0M0001.mflac= NUZ6b0laXXXXXXXX\n"
        "\n"
        "plainEkeyOnly123+/=\n"
    )
    assert src.lookup(["/music/song.mflac"]) == "AbCdEf1234567890+/"
    assert src.lookup(["F0M0001.mflac"]) == "NUZ6b0laXXXXXXXX"
    assert src.manual_pool == ["plainEkeyOnly123+/="]
    assert src.entry_count() == 2
    assert src.is_usable()


def test_manual_text_json_object():
    src = QmcKeySource.empty()
    src.set_manual_text('{"song.mflac": "AbCdEf1234567890+/", "bad": "short"}')
    assert src.lookup(["song.mflac"]) == "AbCdEf1234567890+/"
    assert src.entry_count() == 1
    assert not src.manual_pool


def test_sqlite_load_probe_and_lookup(tmp_path):
    import sqlite3

    db_path = tmp_path / "player_process_db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE audio_file_ekey_table (file_path TEXT, ekey TEXT)")
    conn.execute("INSERT INTO audio_file_ekey_table VALUES ('/storage/emulated/0/Music/song.mflac', 'EKEY_A')")
    conn.execute("CREATE TABLE EKeyFileInfo (filePath TEXT, eKey TEXT)")
    conn.execute("INSERT INTO EKeyFileInfo VALUES ('C:\\Music\\other.mgg', 'EKEY_B')")
    conn.commit()
    conn.close()

    src = QmcKeySource.load_sqlite(db_path)
    assert src.entry_count() == 2
    assert src.lookup(["song.mflac"]) == "EKEY_A"
    assert src.lookup(["/x/other.mgg"]) == "EKEY_B"
    assert src.lookup(["missing.mflac"]) is None


def test_sqlite_path_with_hash_and_percent(tmp_path):
    """路径含 '#' 或 '%' 时需经 as_uri() 打开，否则 sqlite URI 会解析错位。"""
    import sqlite3

    folder = tmp_path / "a#b%20c"
    folder.mkdir()
    db_path = folder / "player_process_db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE audio_file_ekey_table (file_path TEXT, ekey TEXT)")
    conn.execute("INSERT INTO audio_file_ekey_table VALUES ('song.mflac', 'EKEY_A')")
    conn.commit()
    conn.close()

    src = QmcKeySource.load_sqlite(db_path)
    assert src.lookup(["song.mflac"]) == "EKEY_A"


def test_sqlite_load_rejects_db_without_known_tables(tmp_path):
    import sqlite3

    db_path = tmp_path / "random.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE unrelated (a TEXT)")
    conn.commit()
    conn.close()

    try:
        QmcKeySource.load_sqlite(db_path)
        assert False, "应报错"
    except ValueError as e:
        assert "未发现已知的 QQ 音乐密钥表" in str(e)


# ============================================================================
# mmkv 补充
# ============================================================================


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n == 0:
            out.append(b)
            break
        out.append(b | 0x80)
    return bytes(out)


def _entry(key: str, value: bytes) -> bytes:
    kb = key.encode()
    return _varint(len(kb)) + kb + _varint(len(value)) + value


def _wrapped(raw: bytes) -> bytes:
    return _varint(len(raw)) + raw


def _build(entries: list[bytes]) -> bytes:
    body = b"".join(entries)
    return (4 + len(body)).to_bytes(4, "little") + bytes(4) + body


def test_mmkv_parse_roundtrip_and_ekey_filter():
    data = _build(
        [
            _entry("F0M000112233445566.mflac", _wrapped(b"AbCdEf1234567890+/==")),
            _entry("not-an-ekey", _wrapped(b"short")),
            _entry("song.mgg", _wrapped(b"NUZ6b0laQ2FuYmVUYWtlT24zNTY+/w==")),
        ]
    )
    pairs = mmkv.parse(data)
    assert len(pairs) == 2, "非 ekey 值应被过滤"
    assert pairs[0] == ("F0M000112233445566.mflac", "AbCdEf1234567890+/==")
    assert pairs[1][0] == "song.mgg"


def test_mmkv_last_write_wins_and_empty_deletes():
    data = _build(
        [
            _entry("song.mflac", _wrapped(b"AbCdEf1234567890+/==")),
            _entry("song.mflac", _wrapped(b"ZYXWVu9876543210+/==")),
            _entry("gone.mflac", _wrapped(b"AbCdEf1234567890+/==")),
            _entry("gone.mflac", b""),
        ]
    )
    pairs = mmkv.parse(data)
    assert len(pairs) == 1
    assert pairs[0][1] == "ZYXWVu9876543210+/=="


def test_mmkv_rejects_truncated_and_garbage():
    import pytest

    with pytest.raises(ValueError):
        mmkv.parse(b"ab")
    with pytest.raises(ValueError):
        mmkv.parse(_build([_entry("k", b"v")])[:6])
    with pytest.raises(ValueError):
        mmkv.parse(bytes(32))
    with pytest.raises(ValueError):
        mmkv.parse(b"just some random text bytes!!")


def test_mmkv_multi_byte_varint_lengths():
    long_ekey = "A" * 300
    data = _build([_entry("song.mflac", _wrapped(long_ekey.encode()))])
    pairs = mmkv.parse(data)
    assert pairs[0][1] == long_ekey
