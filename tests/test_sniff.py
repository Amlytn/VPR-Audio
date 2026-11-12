"""裸流音频头嗅探测试。"""
from vpr.decrypt.sniff import sniff_ext


def test_sniff_flac_ogg_mp3_m4a():
    assert sniff_ext(b"fLaC" + bytes(8)) == "flac"
    assert sniff_ext(b"OggS" + bytes(8)) == "ogg"
    assert sniff_ext(b"ID3" + bytes(8)) == "mp3"
    assert sniff_ext(b"\xff\xfb" + bytes(8)) == "mp3"
    assert sniff_ext(bytes(4) + b"ftyp" + bytes(8)) == "m4a"


def test_sniff_wav_ape_wma():
    assert sniff_ext(b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + bytes(8)) == "wav"
    assert sniff_ext(b"MAC " + bytes(12)) == "ape"
    assert sniff_ext(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11" + bytes(8)) == "wma"


def test_sniff_unknown_returns_none():
    assert sniff_ext(bytes(32)) is None
    assert sniff_ext(b"") is None
