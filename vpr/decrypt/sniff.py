from __future__ import annotations


def sniff_ext(audio: bytes) -> str | None:
    """根据解密后的音频头魔数判断真实格式，未识别则返回 None。"""
    if audio.startswith(b"fLaC"):
        return "flac"
    if audio.startswith(b"OggS"):
        return "ogg"
    if audio.startswith(b"ID3") or (len(audio) >= 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0):
        return "mp3"  # ID3 头，或无标签裸 MP3 帧同步
    if len(audio) >= 8 and audio[4:8] == b"ftyp":
        return "m4a"  # ISO-BMFF（qmc2/4/6/8、tkm 解出的是 m4a/mp4 容器）
    if len(audio) >= 12 and audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
        return "wav"  # bkcwav
    if audio.startswith(b"MAC "):
        return "ape"  # bkcape
    if audio.startswith(b"\x30\x26\xB2\x75\x8E\x66\xCF\x11"):
        return "wma"  # ASF 头（bkcwma）
    return None
