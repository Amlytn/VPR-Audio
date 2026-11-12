"""转换管线测试 + settings/history 测试。"""
import io
import json
from pathlib import Path

import pytest

from vpr.history import HistoryEntry, HistoryFile, append, clear, load
from vpr.process import (
    CancelState,
    effective_ext,
    find_decoded_file,
    format_ext,
    format_flags,
    format_size,
    is_qmc_ext,
    is_supported_file,
    parse_ffmpeg_duration_secs,
    parse_ffmpeg_time_secs,
    reserve_out_path,
    run_process_with_timeout,
    scan_directory,
    summarize_failure_output,
    _drain_lines_lossy,
    _parse_hms_secs,
)
from vpr.settings import Settings, find_db_path, is_plausible_db_file, load_settings, save_settings, write_atomic


# ============================================================================
# 扩展名判定（is_supported_file / effective_ext）
# ============================================================================


def test_is_supported_file_case_insensitive():
    assert is_supported_file(Path("a.KGG"))
    assert is_supported_file(Path("b.Kgm"))
    assert is_supported_file(Path("c.kgma"))
    assert is_supported_file(Path("d.NcM"))
    assert not is_supported_file(Path("e.flac")), "普通 flac 不再作为输入"
    assert not is_supported_file(Path("f.mp3")), "普通 mp3 不再作为输入"
    assert not is_supported_file(Path("g.txt"))
    assert not is_supported_file(Path("cover"))


def test_is_supported_file_qmc_family():
    assert is_supported_file(Path("song.qmcflac"))
    assert is_supported_file(Path("song.QMC0"))
    assert is_supported_file(Path("song.mflac"))
    assert is_supported_file(Path("song.mgg1"))
    assert is_supported_file(Path("song.mggl"))
    assert is_supported_file(Path("song.tkM"))
    assert is_supported_file(Path("song.666c6163"))
    assert is_supported_file(Path("song.mmp4"))
    # 双后缀：真实扩展名 cache，按 stem 尾段识别
    assert is_supported_file(Path("song.mflac.cache"))
    assert is_supported_file(Path("song.mgg.cache"))
    assert not is_supported_file(Path("song.mp3.cache")), "非 QMC 的 cache 不收"
    assert not is_supported_file(Path("song.cache"))


def test_cache_double_suffix_dispatches_to_qmc():
    for name in ["song.mflac.cache", "song.MFLAC.CACHE", "song.mgg.cache"]:
        eff = effective_ext(Path(name))
        assert eff is not None
        assert is_qmc_ext(eff), f"{name} 必须路由到内置 QMC 解密器"
    assert effective_ext(Path("song.mp3.cache")) is None
    assert effective_ext(Path("song.cache")) is None
    assert effective_ext(Path("song.KGG")) == "kgg"
    assert effective_ext(Path("song.ncm")) == "ncm"
    assert effective_ext(Path("cover")) is None


def test_reserve_out_path_increments_suffix_and_creates_placeholder(tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"x")
    (tmp_path / "song (1).mp3").write_bytes(b"x")
    p = reserve_out_path(tmp_path, "song.mp3")
    created = p.exists()
    assert p.name == "song (2).mp3"
    assert created, "占位文件应当已被创建"


def test_reserve_out_path_closes_placeholder_handle(tmp_path):
    # 回归：占位句柄不关时，Windows 上 unlink 会报 WinError 32，失败/取消会留下
    # 0 字节或半成品文件；同时每输出一个文件泄漏一个 fd。
    p = reserve_out_path(tmp_path, "song.mp3")
    p.unlink()  # 不抛异常才算关闭
    assert not p.exists()


def test_find_decoded_file_skips_extensionless_files(tmp_path):
    (tmp_path / "cover").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / "song.kgg").write_bytes(b"x")
    (tmp_path / "song_kgg-dec.flac").write_bytes(b"x")
    found = find_decoded_file(tmp_path, "song.kgg")
    assert found is not None and found.name == "song_kgg-dec.flac"


def test_find_decoded_file_never_matches_user_file_with_same_stem(tmp_path):
    # 回归：源目录同时存在 song.kgg 与用户自己的 song.flac 时不得误命中（数据丢失风险）
    (tmp_path / "song.kgg").write_bytes(b"x")
    (tmp_path / "song.flac").write_bytes(b"x")
    (tmp_path / "song_kgg-dec.ogg").write_bytes(b"x")
    found = find_decoded_file(tmp_path, "song.kgg")
    assert found is not None and found.name == "song_kgg-dec.ogg"


def test_find_decoded_file_ignores_encrypted_file_with_artifact_stem(tmp_path):
    # 回归：用户自己的 song_kgg-dec.kgg 不能被当成 kgg-dec 产物（否则清理时会删掉）
    (tmp_path / "song.kgg").write_bytes(b"x")
    (tmp_path / "song_kgg-dec.kgg").write_bytes(b"x")
    assert find_decoded_file(tmp_path, "song.kgg") is None


def test_find_decoded_file_prefix_siblings_isolated(tmp_path):
    # 回归：`一路向北.kgg` 与 `一路向北 (Live).kgg` 同时存在时各自只匹配自己的产物
    (tmp_path / "周杰伦 - 一路向北.kgg").write_bytes(b"x")
    (tmp_path / "周杰伦 - 一路向北 (Live).kgg").write_bytes(b"x")
    (tmp_path / "周杰伦 - 一路向北_kgg-dec.ogg").write_bytes(b"x")
    (tmp_path / "周杰伦 - 一路向北 (Live)_kgg-dec.ogg").write_bytes(b"x")
    a = find_decoded_file(tmp_path, "周杰伦 - 一路向北.kgg")
    b = find_decoded_file(tmp_path, "周杰伦 - 一路向北 (Live).kgg")
    assert a is not None and a.name == "周杰伦 - 一路向北_kgg-dec.ogg"
    assert b is not None and b.name == "周杰伦 - 一路向北 (Live)_kgg-dec.ogg"


def test_scan_directory_skips_excluded_output_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "song.kgg").write_bytes(b"x")
    (out / "song.mp3").write_bytes(b"x")
    files = scan_directory(tmp_path, [out])
    assert len(files) == 1, "输出目录里的 mp3 应被排除"
    assert files[0].name.endswith("song.kgg")


def test_scan_directory_includes_root_even_if_it_is_the_output_dir(tmp_path):
    # 回归：用户把输出目录选成源目录后再「添加文件夹」，不应得到 0 个文件
    (tmp_path / "song.kgg").write_bytes(b"x")
    files = scan_directory(tmp_path, [tmp_path])
    assert len(files) == 1
    assert files[0].name == "song.kgg"


# ============================================================================
# ffmpeg 进度 / 失败摘要 / 工具函数
# ============================================================================


def test_parse_ffmpeg_progress_lines():
    d = parse_ffmpeg_duration_secs("  Duration: 00:03:30.05, start: 0.000000, bitrate: 1411 kb/s")
    assert abs(d - 210.05) < 1e-6
    assert parse_ffmpeg_duration_secs("  Duration: N/A, start: 0.000000") is None

    t = parse_ffmpeg_time_secs(
        "frame=  123 fps= 30 q=-1.0 size=     1kB time=00:01:02.03 bitrate= 16.0kbits/s speed=4.16x"
    )
    assert abs(t - 62.03) < 1e-6
    t2 = parse_ffmpeg_time_secs("time=00:00:00.04 bitrate= ...")
    assert abs(t2 - 0.04) < 1e-6
    assert parse_ffmpeg_time_secs("time=N/A") is None
    assert parse_ffmpeg_time_secs("no time field here") is None


def test_ffmpeg_progress_is_throttled():
    import time

    from vpr.process import ffmpeg_progress_emitter

    class Recorder:
        def __init__(self):
            self.percents = []

        def emit_progress(self, info):
            self.percents.append(info.percent)

    rec = Recorder()
    on_line = ffmpeg_progress_emitter(rec, 0, 1)
    on_line("  Duration: 00:00:10.00, start: 0.0")
    on_line("time=00:00:01.00")  # 10% 立即推
    on_line("time=00:00:02.00")  # 20% 在 100ms 内被节流
    assert rec.percents == [10]
    time.sleep(0.12)
    on_line("time=00:00:03.00")  # 30% 推
    on_line("time=00:00:09.99")  # 99% 不节流
    assert rec.percents == [10, 30, 99]


def test_parse_hms_variants():
    assert _parse_hms_secs("01:02:03") == 3723.0
    assert _parse_hms_secs("12.5") == 12.5
    assert _parse_hms_secs("N/A") is None
    assert _parse_hms_secs("1:2:3:4") is None  # 非法分段数


def test_drain_lines_lossy_splits_cr_and_lf():
    # 回归：ffmpeg 统计行以 \r 分隔，只按 \n 切会把整段进度攒成一行
    lines: list[str] = []
    raw = b"frame=1 time=00:00:00.50\rframe=2 time=00:00:01.00\r\nbanner line\n"
    out = _drain_lines_lossy(io.BytesIO(raw), lines.append)
    assert lines == ["frame=1 time=00:00:00.50", "frame=2 time=00:00:01.00", "banner line"]
    assert "banner line" in out


def test_summarize_failure_prefers_real_errors_over_ffmpeg_banner():
    combined = (
        "ffmpeg version 2025-03-31-git-35c091f4b7-essentials_build Copyright\n"
        "  built with gcc 14.2.0\n  configuration: --enable-gpl\n"
        "  libavutil      60.  0.100 / 60.  0.100\n"
        "Input #0, mp3, from 'x.mp3':\n"
        "Option map_metadata cannot be applied to input url cover.jpg\n"
        "Error opening input files: Invalid argument\n"
    )
    s = summarize_failure_output(combined, 1)
    assert "cannot be applied" in s
    assert "Invalid argument" in s
    assert "ffmpeg version" not in s
    assert "built with" not in s


def test_summarize_failure_fallback_and_noise_filter():
    combined = "This is free and open source software\nMIT license text\nsome real output one\nline two\nline three\nline four"
    s = summarize_failure_output(combined, 2)
    assert "some real output one" in s
    assert "line three" in s
    assert "line four" not in s
    assert "MIT" not in s
    # 全是噪声 → 退到退出码
    assert summarize_failure_output("ffmpeg version x\n  built with gcc\n", None) == "退出码 -1"


def test_format_size_units():
    assert format_size(0) == "0 B"
    assert format_size(1023) == "1023 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"


def test_format_table_entries_are_complete():
    for value, (ext, flags) in {
        "mp3": format_flags("mp3") and ("mp3", format_flags("mp3")),
        "flac": ("flac", format_flags("flac")),
        "ogg": ("ogg", format_flags("ogg")),
    }.items():
        assert ext
        assert flags


def test_format_ext_flags_cover_all_menu_values():
    assert format_ext("mp3") == "mp3"
    assert format_ext("flac") == "flac"
    assert format_ext("ogg") == "ogg"
    for f in ("mp3", "flac", "ogg"):
        flags = format_flags(f)
        assert "-map_metadata" in flags, f"{f} 缺少 -map_metadata"
        assert "-acodec" in flags, f"{f} 缺少 -acodec"
    assert "libmp3lame" in format_flags("mp3")
    assert "flac" in format_flags("flac")
    assert "libvorbis" in format_flags("ogg")
    assert "-id3v2_version" in format_flags("mp3")  # mp3 强制 ID3v2.3 以兼容 Windows
    assert format_ext("source") is None
    assert format_ext("wav") is None


def test_qmc_key_candidates_reuse_file_bytes(tmp_path, monkeypatch):
    """候选密钥逐个试解时不能重复读盘：一次读入，内存里试。"""
    from types import SimpleNamespace

    from vpr import process
    from vpr.decrypt.qmc_keydb import QmcKeySource

    keys = QmcKeySource.empty()
    keys.set_manual_text("QUJDREVGR0hJSktMTU5PUA==\nWkVZVFdPQkFTRTY0MDAwMDA=")
    env = SimpleNamespace(qmc_keys=keys)
    calls = []

    class Decoded:
        audio = bytearray(b"fLaC")
        audio_ext = "flac"
        footer = None

    def fake_decrypt(data, ekey=None):
        calls.append((data, ekey))
        if ekey == keys.manual_pool[0]:
            raise ValueError("bad key")
        return Decoded()

    monkeypatch.setattr(process.qmc, "decrypt_bytes", fake_decrypt)
    path = tmp_path / "song.mflac"
    out = process._try_decrypt_with_qmc_keys(env, path, path.name, "no key", b"RAW")
    assert out.audio_ext == "flac"
    assert [c[0] for c in calls] == [b"RAW", b"RAW"]


# ============================================================================
# 子进程执行（用真实 python 子进程验证超时/取消语义）
# ============================================================================


def test_run_process_success_and_failure(tmp_path):
    import sys

    cancel = CancelState()
    # 成功路径：返回 stderr+stdout（kgg-dec 的 [WARN] 诊断依赖这段输出）
    out = run_process_with_timeout(Path(sys.executable), ["-c", "print('ok')"], 30, cancel, 0)
    assert "ok" in out
    out = run_process_with_timeout(
        Path(sys.executable),
        ["-c", "import sys; print('warn-line', file=sys.stderr)"],
        30,
        cancel,
        0,
    )
    assert "warn-line" in out
    # 失败路径：非零退出 → RuntimeError 带失败摘要（stderr 的错误行/前 3 行非噪声）
    with pytest.raises(RuntimeError, match="boom-err|退出码"):
        run_process_with_timeout(Path(sys.executable), ["-c", "import sys; print('boom-err'); sys.exit(3)"], 30, cancel, 0)


def test_run_process_timeout_kills(tmp_path):
    import sys

    cancel = CancelState()
    with pytest.raises(RuntimeError, match="进程超时"):
        run_process_with_timeout(Path(sys.executable), ["-c", "import time; time.sleep(60)"], 1, cancel, 0)


def test_run_process_cancel_kills():
    import sys
    import threading

    cancel = CancelState()

    def cancel_soon():
        import time

        time.sleep(0.5)
        cancel.request_cancel()

    threading.Thread(target=cancel_soon, daemon=True).start()
    with pytest.raises(RuntimeError, match="用户取消"):
        run_process_with_timeout(Path(sys.executable), ["-c", "import time; time.sleep(30)"], 60, cancel, 0)


# ============================================================================
# settings / history
# ============================================================================


def test_is_plausible_db_file_sanity(tmp_path):
    good = tmp_path / "good.db"
    good.write_bytes(bytes(2048))  # 非 SQLite 头但体积正常的文件（真实 KGMusicV3.db 头非标准魔数）
    (tmp_path / "empty.db").write_bytes(b"")
    (tmp_path / "tiny.db").write_bytes(b"x")

    assert is_plausible_db_file(good)
    assert not is_plausible_db_file(tmp_path / "empty.db"), "0 字节文件应被拒绝"
    assert not is_plausible_db_file(tmp_path / "tiny.db"), "过小的文件应被拒绝"
    assert not is_plausible_db_file(tmp_path / "missing.db"), "不存在的文件应被拒绝"
    assert not is_plausible_db_file(tmp_path), "目录不算库文件"


def test_find_db_path_skips_invalid_custom_path(tmp_path, monkeypatch):
    bad = tmp_path / "fake.db"
    bad.write_bytes(b"garbage")
    settings = Settings(db_path=str(bad))
    # 屏蔽默认位置（APPDATA/LOCALAPPDATA 与程序目录的真实库）
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    import vpr.settings as st

    orig = st.get_tool_dir
    monkeypatch.setattr(st, "get_tool_dir", lambda: tmp_path)
    info = find_db_path(settings)
    assert not info["found"] or info["path"] != str(bad), "即使降级找到库，也绝不返回坏文件本身"


def test_write_atomic_creates_and_replaces_without_leftover_tmp(tmp_path):
    target = tmp_path / "settings.json"
    write_atomic(target, "first")
    assert target.read_text(encoding="utf-8") == "first"
    write_atomic(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    tmp_left = [e for e in tmp_path.iterdir() if e.name.endswith(".tmp")]
    assert len(tmp_left) == 0, "rename 成功后不应残留临时文件"


def test_settings_roundtrip_keeps_input_path(tmp_path, monkeypatch):
    # 回归：对话框路径记忆新增 select_input_path，读写不能丢字段
    monkeypatch.setenv("APPDATA", str(tmp_path))
    save_settings(Settings(select_input_path=str(tmp_path), select_output_path=str(tmp_path)))
    loaded = load_settings()
    assert loaded.select_input_path == str(tmp_path)
    assert loaded.select_output_path == str(tmp_path)


def test_settings_normalizes_invalid_output_format_and_concurrency(tmp_path, monkeypatch):
    """手改/损坏的 settings.json 不能让整批转换都失败：非法值按未设置处理。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = tmp_path / "kugou-converter" / "settings.json"
    path.parent.mkdir(parents=True)

    path.write_text(json.dumps({"output_format": "aac", "concurrency": 99}), encoding="utf-8")
    loaded = load_settings()
    assert loaded.output_format is None
    assert loaded.concurrency is None

    path.write_text(json.dumps({"output_format": "flac", "concurrency": 4}), encoding="utf-8")
    loaded = load_settings()
    assert loaded.output_format == "flac"
    assert loaded.concurrency == 4


def test_legacy_settings_migration(tmp_path, monkeypatch):
    # 旧版桌面客户端 userData：camelCase → snake_case，仅在当前设置不存在时生效
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr("vpr.settings.get_tool_dir", lambda: tmp_path / "tool")
    old = tmp_path / "酷狗音乐kgg转换器" / "settings.json"
    old.parent.mkdir(parents=True)
    old.write_text(
        json.dumps({"dbPath": "D:/x/KGMusicV3.db", "selectOutputPath": "D:/out"}),
        encoding="utf-8",
    )
    loaded = load_settings()
    assert loaded.db_path == "D:/x/KGMusicV3.db"
    assert loaded.select_output_path == "D:/out"


def test_history_append_load_clear_roundtrip(tmp_path, monkeypatch):
    import vpr.history as h

    monkeypatch.setattr(h, "history_path", lambda: tmp_path / "history.jsonl")
    clear()
    entry = HistoryEntry(
        time_ms=1700000000000,
        total=3,
        success=2,
        failed=1,
        cancelled=0,
        duration_secs=30,
        output_dir="D:/out",
        format="mp3",
        concurrency=3,
        files=[HistoryFile(name="a.kgg", size=100, error=None), HistoryFile(name="b.kgg", size=200, error="boom")],
    )
    append(entry)
    append(entry)
    loaded = load()
    assert len(loaded) == 2
    assert loaded[0]["total"] == 3
    assert loaded[0]["files"][1]["error"] == "boom"
    clear()
    assert load() == []


def test_history_keeps_last_200(tmp_path, monkeypatch):
    import vpr.history as h

    monkeypatch.setattr(h, "history_path", lambda: tmp_path / "history.jsonl")
    clear()
    for i in range(205):
        append(
            HistoryEntry(
                time_ms=i, total=1, success=1, failed=0, cancelled=0,
                duration_secs=1, output_dir="x", format="mp3", concurrency=1,
            )
        )
    loaded = load()
    assert len(loaded) == 200
    assert loaded[0]["time_ms"] == 5  # 最旧的 5 条被裁掉
    assert loaded[-1]["time_ms"] == 204
    clear()


# ============================================================================
# 酷狗安卓双后缀（song.kgm.flac / song.vpr.flac）
# ============================================================================


def test_effective_ext_handles_kugou_android_suffixes():
    assert effective_ext(Path("song.kgm.flac")) == "kgm"
    assert effective_ext(Path("song.KGMA.FLAC")) == "kgma"
    assert effective_ext(Path("song.vpr.flac")) == "vpr"
    assert effective_ext(Path("song.vpr.mp3")) == "vpr"
    # 普通双后缀不受影响：flac/mp3 仍是自身
    assert effective_ext(Path("song.live.flac")) == "flac"
    assert is_supported_file(Path("song.kgm.flac"))
    assert is_supported_file(Path("song.vpr.flac"))
    assert not is_supported_file(Path("song.live.flac"))


def test_process_file_dispatches_kugou_android_names(tmp_path, monkeypatch):
    from vpr import process as proc

    seen: list[str] = []

    def fake_kgm(env, file_path, index):
        seen.append(Path(file_path).name)
        return proc.ConvertResult(file=Path(file_path).name, error=None)

    monkeypatch.setattr(proc, "process_kgm", fake_kgm)
    for name in ("a.kgm.flac", "b.vpr.flac"):
        result = proc.process_file(object(), str(tmp_path / name), 0)
        assert result.error is None
    assert seen == ["a.kgm.flac", "b.vpr.flac"]


def test_process_kgm_derives_vpr_from_effective_ext(tmp_path, monkeypatch):
    """双后缀文件名必须让 process_kgm 走 VPR 附加密钥流（而不是按 raw suffix 判断）。"""
    from vpr import process as proc

    calls: list[bool] = []

    def fake_decrypt(data, *, vpr=False):
        calls.append(vpr)
        return proc.kgm.KgmDecoded(audio=bytearray(b"fLaC" + b"\x00" * 32), audio_ext="flac")

    monkeypatch.setattr(proc.kgm, "decrypt_bytes", fake_decrypt)
    monkeypatch.setattr(
        proc,
        "finalize_conversion",
        lambda env, decoded, original, extra_in, extra_out: proc.ConvertResult(file=original.name),
    )
    src = tmp_path / "song.vpr.flac"
    src.write_bytes(b"\x05" * 64)
    env = type("Env", (), {"emitter": None})()
    result = proc.process_kgm(env, str(src), 0)
    assert result.error is None
    assert calls == [True]
