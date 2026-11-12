"""转换管线：文件扫描、输出占位、子进程管理与单文件转换。"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .cancel import CancelState, _kill_proc
from .decrypt import kgm, ncm, qmc
from .decrypt.qmc_keydb import QmcKeySource
from .events import Emitter

QMC_EXTS = frozenset(
    {
        "qmc0", "qmc2", "qmc3", "qmc4", "qmc6", "qmc8", "qmcflac", "qmcogg", "tkm",
        "bkcmp3", "bkcm4a", "bkcflac", "bkcwav", "bkcape", "bkcogg", "bkcwma",
        "666c6163", "6d7033", "6f6767", "6d3461", "776176", "mmp4",
        "mflac", "mgg", "mflac0", "mflac1", "mflaca", "mflach", "mflacl", "mflacm",
        "mgg0", "mgg1", "mgga", "mggh", "mggl", "mggm",
    }
)

KUGOU_NETEASE_EXTS = frozenset({"kgg", "kgm", "kgma", "vpr", "ncm"})


def effective_ext(path: Path) -> str | None:
    """提取文件有效扩展名（小写、无点），兼容 .cache 和 .kgm.flac / .vpr.flac 双后缀。"""
    ext = path.suffix.lower().lstrip(".")
    stem = path.stem
    sub = stem.rsplit(".", 1)[-1].lower() if "." in stem else None
    if ext == "cache":
        return sub if sub in ("mflac", "mgg") else None
    if ext in ("flac", "mp3") and sub in ("kgm", "kgma", "vpr"):
        return sub
    return ext or None


def is_supported_file(path: Path) -> bool:
    ext = effective_ext(path)
    if ext is None:
        return False
    return ext in KUGOU_NETEASE_EXTS or ext in QMC_EXTS


def is_qmc_ext(ext: str) -> bool:
    return ext in QMC_EXTS


def format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    i = 0
    while value >= 1024.0 and i < len(units) - 1:
        value /= 1024.0
        i += 1
    if i == 0:
        return f"{size} {units[i]}"
    return f"{value:.1f} {units[i]}"


FORMAT_TABLE: dict[str, tuple[str, list[str]]] = {
    # ID3v2.3 保证 Windows 资源管理器能正常读取中文标签
    "mp3": ("mp3", ["-map_metadata", "0", "-id3v2_version", "3", "-acodec", "libmp3lame", "-b:a", "320k"]),
    # level 5 与 level 8 体积差不到 1%，编码速度差数倍
    "flac": ("flac", ["-map_metadata", "0", "-acodec", "flac", "-compression_level", "5"]),
    "ogg": ("ogg", ["-map_metadata", "0", "-acodec", "libvorbis", "-q:a", "5"]),
}


def format_ext(fmt: str) -> str | None:
    return FORMAT_TABLE[fmt][0] if fmt in FORMAT_TABLE else None


def format_flags(fmt: str) -> list[str] | None:
    return FORMAT_TABLE[fmt][1] if fmt in FORMAT_TABLE else None


@dataclass
class FileItem:
    path: str
    name: str
    size: int
    formatted_size: str


@dataclass
class ConvertResult:
    file: str
    error: str | None = None


@dataclass
class ProgressInfo:
    current: int
    total: int
    index: int
    status: str  # processing / converting / done / failed / cancelled
    percent: int | None


def scan_directory(root: Path, excluded: list[Path]) -> list[FileItem]:
    """递归扫描加密音频文件，跳过符号链接环及输出子目录。"""
    files: list[FileItem] = []
    excluded_set = {p.resolve() for p in excluded if p.exists()}
    visited: set[Path] = set()
    try:
        root_canon = root.resolve()
    except OSError:
        root_canon = root

    def walk(directory: Path) -> None:
        try:
            canon = directory.resolve()
        except OSError:
            canon = directory
        if canon in visited:
            return
        visited.add(canon)
        if canon in excluded_set and canon != root_canon:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=True):
                    walk(path)
                elif entry.is_file(follow_symlinks=False) and is_supported_file(path):
                    size = entry.stat(follow_symlinks=False).st_size
                    files.append(FileItem(path=str(path), name=path.name, size=size, formatted_size=format_size(size)))
            except OSError:
                continue

    walk(root)
    return files


def reserve_out_path(out_dir: Path, name: str) -> Path:
    """以 O_CREAT | O_EXCL 原子占位不冲突的输出路径，并发下同名自动顺延。"""
    stem = Path(name).stem
    ext = Path(name).suffix

    candidate = out_dir / name
    i = 1
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            # 必须立即关闭，否则 Windows 上清理时会触发 WinError 32
            os.close(fd)
            return candidate
        except FileExistsError:
            candidate = out_dir / f"{stem} ({i}){ext}"
            i += 1
        except OSError as e:
            raise RuntimeError(f"创建输出文件失败 {candidate}: {e}") from e


KGG_DEC_SUFFIX = "_kgg-dec"
KGG_DEC_OUTPUT_EXTS = frozenset(
    {".flac", ".mp3", ".ogg", ".m4a", ".wav", ".ape", ".wma", ".aac", ".opus", ".mp4"}
)


def find_decoded_file(source_dir: Path, file_name: str) -> Path | None:
    """只匹配 `{主干}_kgg-dec.{音频扩展名}`，避免误匹配用户目录里同名的原音频。"""
    base = Path(file_name).stem.lower()
    target_stem = f"{base}{KGG_DEC_SUFFIX}"

    try:
        entries = list(os.scandir(source_dir))
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file():
            continue
        p = Path(entry.path)
        if p.suffix.lower() not in KGG_DEC_OUTPUT_EXTS:
            continue
        f_name = p.name.lower()
        f_stem = p.stem.lower()
        if f_name != file_name.lower() and f_stem == target_stem:
            return p
    return None


# ============================================================================
# 子进程执行（超时 + 取消 + stderr 实时逐行）
# ============================================================================

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def timeout_for_file(path: Path, base: int = 60) -> int:
    """按源文件大小估算超时：基础秒数 + 每 MB 2 秒，上限 30 分钟"""
    try:
        size_mb = path.stat().st_size // (1024 * 1024)
    except OSError:
        size_mb = 0
    return min(base + size_mb * 2, 1800)


def summarize_failure_output(combined: str, code: int | None) -> str:
    """提取子进程失败摘要，过滤开源声明和 ffmpeg 版本横幅。"""
    lines = [line for line in combined.splitlines() if line.strip()]

    def is_noise(line: str) -> bool:
        s = line.strip().lower()
        return (
            "MIT" in line
            or "git.unlock-music" in line
            or "free and open" in line
            or s.startswith("ffmpeg version")
            or s.startswith("built with")
            or s.startswith("configuration:")
            or s.startswith("libav")
        )

    def looks_err(line: str) -> bool:
        low = line.lower()
        return any(w in low for w in ("error", "invalid", "cannot", "failed", "no such", "denied"))

    err_lines = [line for line in lines if looks_err(line)]
    if err_lines:
        brief = err_lines[-3:]
    else:
        brief = [line for line in lines if not is_noise(line)][:3]
    if not brief:
        return f"退出码 {code if code is not None else -1}"
    return " | ".join(brief)[:200]


def _drain_lines_lossy(pipe, on_line) -> str:
    """按 chunk 读取 stderr 并同时按 \\r 和 \\n 切分（ffmpeg 进度行以 \\r 分隔）。"""
    collected: list[str] = []
    buf = b""
    reader = getattr(pipe, "read1", None) or pipe.read

    def emit(raw: bytes) -> None:
        if not raw:
            return
        line = raw.decode("utf-8", "replace").rstrip("\x00")
        if not line:
            return
        if on_line is not None:
            on_line(line)
        collected.append(line)

    while True:
        chunk = reader(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for part in parts:
            emit(part)
        if len(buf) > 65536:
            emit(buf)
            buf = b""
    emit(buf)
    return "\n".join(collected) + ("\n" if collected else "")


def run_process_with_timeout(
    exe: Path,
    args: list[str],
    timeout_secs: int,
    cancel: CancelState,
    task_index: int,
    on_stderr_line=None,
) -> str:
    """运行子进程并处理超时与取消，正常退出时返回合并输出以供上层诊断。"""
    creationflags = CREATE_NO_WINDOW | DETACHED_PROCESS if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            [str(exe), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
    except OSError as e:
        raise RuntimeError(f"工具启动失败: {e}") from e

    cancel.register(task_index, proc)
    try:
        stderr_lines: list[str] = []
        stderr_lock = threading.Lock()

        def stderr_reader() -> None:
            assert proc.stderr is not None
            text = _drain_lines_lossy(proc.stderr, lambda line: on_stderr_line(line) if on_stderr_line else None)
            with stderr_lock:
                stderr_lines.append(text)

        err_thread = threading.Thread(target=stderr_reader, daemon=True)
        err_thread.start()
        stdout_text = ""
        stdout_thread: threading.Thread | None = None

        def stdout_reader() -> None:
            nonlocal stdout_text
            assert proc.stdout is not None
            stdout_text = proc.stdout.read().decode("utf-8", "replace")

        if proc.stdout is not None:
            # 必须后台排空：管道写满会阻塞子进程，超时计时随之失效
            stdout_thread = threading.Thread(target=stdout_reader, daemon=True)
            stdout_thread.start()

        start = time.monotonic()
        killed_reason: str | None = None
        while True:
            if cancel.is_cancelled():
                cancel.kill_all()
                killed_reason = "用户取消"
                break
            if time.monotonic() - start > timeout_secs:
                cancel.kill_task(task_index)
                killed_reason = "进程超时"
                break
            code = proc.poll()
            if code is not None:
                break
            time.sleep(0.05)

        if stdout_thread is not None:
            stdout_thread.join(timeout=5)
        err_thread.join(timeout=5)
        if killed_reason:
            raise RuntimeError(killed_reason)
        code = proc.returncode
        if code == 0:
            # kgg-dec 缺密钥时会返回 0 且仅在输出里打 WARN，需要把输出返回给调用方判断
            with stderr_lock:
                stderr_text = stderr_lines[0] if stderr_lines else ""
            return f"{stderr_text}\n{stdout_text}"
        with stderr_lock:
            stderr_text = stderr_lines[0] if stderr_lines else ""
        combined = f"{stderr_text}\n{stdout_text}"
        raise RuntimeError(summarize_failure_output(combined, code))
    finally:
        cancel.unregister(task_index)


def parse_ffmpeg_duration_secs(line: str) -> float | None:
    idx = line.find("Duration:")
    if idx == -1:
        return None
    token = line[idx + len("Duration:") :].split(",")[0].strip()
    return _parse_hms_secs(token)


def parse_ffmpeg_time_secs(line: str) -> float | None:
    idx = line.find("time=")
    if idx == -1:
        return None
    rest = line[idx + len("time=") :]
    token = ""
    for c in rest:
        if c.isdigit() or c in ":.":
            token += c
        else:
            break
    if not token or token == "N/A":
        return None
    return _parse_hms_secs(token)


def _parse_hms_secs(s: str) -> float | None:
    parts = s.strip().split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 1:
            return float(parts[0])
    except ValueError:
        return None
    return None


@dataclass
class ConvertEnv:
    emitter: Emitter
    cancel: CancelState
    out_dir: Path
    kgg_dec_exe: Path
    ffmpeg_exe: Path
    kgg_ok: bool
    ffmpeg_ok: bool
    db_info: dict
    format: str
    qmc_keys: QmcKeySource
    index: int
    total: int


def _best_effort_remove(emitter: Emitter, path: Path) -> None:
    try:
        path.unlink()
    except OSError as e:
        emitter.emit_log(f"清理文件失败: {path} ({e})", "warn")


def ffmpeg_progress_emitter(emitter: Emitter, index: int, total: int):
    """解析 ffmpeg stderr 进度并按最多 10Hz 节流推送（99% 不节流）。"""
    state = {"total_secs": None, "last_percent": None, "last_emit": 0.0}

    def on_line(line: str) -> None:
        if state["total_secs"] is None:
            state["total_secs"] = parse_ffmpeg_duration_secs(line)
        now = parse_ffmpeg_time_secs(line)
        if now is None:
            return
        duration = state["total_secs"]
        if not duration or duration <= 0:
            return
        percent = int(min(max(round(now / duration * 100), 0), 99))
        if state["last_percent"] == percent:
            return
        now_mono = time.monotonic()
        if percent < 99 and now_mono - state["last_emit"] < 0.1:
            return
        state["last_percent"] = percent
        state["last_emit"] = now_mono
        emitter.emit_progress(ProgressInfo(current=index, total=total, index=index, status="converting", percent=percent))

    return on_line


def finalize_conversion(
    env: ConvertEnv,
    decoded: Path,
    original: Path,
    extra_inputs: list[str],
    extra_output_args: list[str],
) -> ConvertResult:
    """调用 ffmpeg 将解密后的音频转码为目标格式。"""
    emitter = env.emitter
    file_name = original.name
    stem = original.stem

    target_ext = format_ext(env.format)
    if target_ext is None:
        return ConvertResult(file=file_name, error=f"不支持的输出格式: {env.format}")

    try:
        out_path = reserve_out_path(env.out_dir, f"{stem}.{target_ext}")
    except RuntimeError as e:
        return ConvertResult(file=file_name, error=str(e))

    # extra_inputs 必须紧跟主输入，否则选项会作用域错位
    args = ["-i", str(decoded)]
    args += extra_inputs
    args += format_flags(env.format) or []
    args += extra_output_args
    args += ["-y", str(out_path)]

    if not env.ffmpeg_ok:
        _best_effort_remove(emitter, out_path)
        return ConvertResult(file=file_name, error="ffmpeg.exe 未找到")

    timeout = timeout_for_file(original, 60)
    progress_cb = ffmpeg_progress_emitter(emitter, env.index, env.total)
    try:
        run_process_with_timeout(env.ffmpeg_exe, args, timeout, env.cancel, env.index, progress_cb)
        emitter.emit_log(f"已转换: {out_path.name}", "info")
        return ConvertResult(file=file_name, error=None)
    except RuntimeError as e:
        _best_effort_remove(emitter, out_path)
        return ConvertResult(file=file_name, error=str(e))


def process_ncm(env: ConvertEnv, file_path: str, index: int) -> ConvertResult:
    """解密 NCM 并经临时文件转码，保留标签与封面（ogg 暂跳过封面）。"""
    emitter = env.emitter
    path = Path(file_path)
    file_name = path.name

    def fail(e: str) -> ConvertResult:
        return ConvertResult(file=file_name, error=e)

    try:
        content = ncm.decrypt_file(path)
    except ValueError as e:
        return fail(str(e))
    except OSError as e:
        return fail(f"读取 NCM 文件失败: {e}")

    nanos = time.time_ns()
    tmp_audio = Path(tempfile.gettempdir()) / f"vpr-ncm-{os.getpid()}-{index}-{nanos}.{content.audio_ext}"
    try:
        tmp_audio.write_bytes(content.audio)
    except OSError as e:
        return fail(f"写入解密临时文件失败: {e}")

    tmp_cover: Path | None = None
    if env.format != "ogg" and content.cover:
        cover_bytes = content.cover
        if cover_bytes.startswith(b"\xff\xd8"):
            ext = "jpg"
        elif cover_bytes.startswith(b"\x89PNG"):
            ext = "png"
        else:
            ext = ""
        if not ext:
            emitter.emit_log(f"封面格式无法识别，已跳过封面写入: {file_name}", "warn")
        else:
            p = Path(tempfile.gettempdir()) / f"vpr-ncm-{os.getpid()}-{index}-{nanos}.cover.{ext}"
            try:
                p.write_bytes(cover_bytes)
                tmp_cover = p
            except OSError as e:
                emitter.emit_log(f"封面写入失败（不影响转换）: {e}", "warn")

    extra_inputs: list[str] = []
    extra_output: list[str] = []
    if tmp_cover is not None:
        extra_inputs += ["-i", str(tmp_cover)]
        extra_output += ["-map", "0:a", "-map", "1:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    if content.meta:
        for key, val in (("title", content.meta.title), ("artist", content.meta.artist), ("album", content.meta.album)):
            if val:
                extra_output += ["-metadata", f"{key}={val}"]

    result = finalize_conversion(env, tmp_audio, path, extra_inputs, extra_output)
    _best_effort_remove(emitter, tmp_audio)
    if tmp_cover is not None:
        _best_effort_remove(emitter, tmp_cover)
    return result


def process_qmc(env: ConvertEnv, file_path: str, index: int) -> ConvertResult:
    """解密 QMC 并转码。"""
    emitter = env.emitter
    path = Path(file_path)
    file_name = path.name

    def fail(e: str) -> ConvertResult:
        return ConvertResult(file=file_name, error=e)

    try:
        data = path.read_bytes()
    except OSError as e:
        return fail(f"读取 QQ 音乐文件失败: {e}")

    try:
        content = qmc.decrypt_bytes(data)
    except ValueError as e:
        if str(e).startswith(qmc.ERR_NO_KEY):
            try:
                content = _try_decrypt_with_qmc_keys(env, path, file_name, str(e), data)
            except ValueError as e2:
                return fail(str(e2))
        else:
            return fail(str(e))

    nanos = time.time_ns()
    tmp_audio = Path(tempfile.gettempdir()) / f"vpr-qmc-{os.getpid()}-{index}-{nanos}.{content.audio_ext}"
    try:
        tmp_audio.write_bytes(content.audio)
    except OSError as e:
        return fail(f"写入解密临时文件失败: {e}")

    result = finalize_conversion(env, tmp_audio, path, [], [])
    _best_effort_remove(emitter, tmp_audio)
    return result


def process_kgm(env: ConvertEnv, file_path: str, index: int) -> ConvertResult:
    """解密 KGM/KGMA/VPR 并转码。"""
    emitter = env.emitter
    path = Path(file_path)
    file_name = path.name

    def fail(e: str) -> ConvertResult:
        return ConvertResult(file=file_name, error=e)

    try:
        data = path.read_bytes()
    except OSError as e:
        return fail(f"读取文件失败: {e}")

    try:
        content = kgm.decrypt_bytes(data, vpr=effective_ext(path) == "vpr")
    except ValueError as e:
        return fail(str(e))

    nanos = time.time_ns()
    tmp_audio = Path(tempfile.gettempdir()) / f"vpr-kgm-{os.getpid()}-{index}-{nanos}.{content.audio_ext}"
    try:
        tmp_audio.write_bytes(content.audio)
    except OSError as e:
        return fail(f"写入解密临时文件失败: {e}")

    result = finalize_conversion(env, tmp_audio, path, [], [])
    _best_effort_remove(emitter, tmp_audio)
    return result


def _try_decrypt_with_qmc_keys(
    env: ConvertEnv, path: Path, file_name: str, first_err: str, data: bytes
) -> qmc.QmcDecoded:
    """按文件名查 QQ 密钥库，未命中时再尝试最多 8 条手动候选密钥。"""
    keys = env.qmc_keys
    if not keys.is_usable():
        raise ValueError(
            f"{first_err}\n导入入口：工作台「密钥库」一栏把分段开关切到 QQ，点「导入」选择密钥库文件；"
            "或在设置中心「QQ 音乐密钥」粘贴手动密钥。"
        )
    stem = path.stem
    candidates: list[str] = []
    hit = keys.lookup([file_name, stem])
    if hit:
        candidates.append(hit)
    candidates.extend(keys.manual_pool[:8])

    last_err = first_err
    for ekey in candidates:
        try:
            return qmc.decrypt_bytes(data, ekey)
        except ValueError as e:
            last_err = str(e)
    raise ValueError(f"{last_err}（已尝试 QQ 音乐密钥源 {len(candidates)} 条密钥未命中）")


def _remove_kgg_artifacts(emitter: Emitter, source_dir: Path, file_name: str) -> None:
    base = Path(file_name).stem.lower()
    target = f"{base}{KGG_DEC_SUFFIX}"
    try:
        entries = list(os.scandir(source_dir))
    except OSError:
        return
    for entry in entries:
        if not entry.is_file():
            continue
        p = Path(entry.path)
        if p.name.lower() == file_name.lower():
            continue
        if p.suffix.lower() not in KGG_DEC_OUTPUT_EXTS:
            continue
        if p.stem.lower() == target:
            _best_effort_remove(emitter, p)


def _kgg_failure_reason(output: str) -> str:
    low = (output or "").lower()
    if "kgmusicv3.db not found" in low or "db not found" in low:
        return "未找到 KGMusicV3.db，请选择密钥库后重试"
    if "ekey" in low and ("not found" in low or "missing" in low or "不存在" in low):
        return "未找到解密密钥（KGMusicV3.db 中没有该歌曲的 ekey）"
    if "invalid kgg header" in low:
        return "不是有效的 KGG 文件（文件头不匹配）"
    return "未找到解密后的文件"


def process_file(env: ConvertEnv, file_path: str, index: int) -> ConvertResult:
    path = Path(file_path)
    file_name = path.name
    raw_ext = path.suffix.lower()
    eff_ext = effective_ext(path)

    if raw_ext == ".ncm":
        return process_ncm(env, file_path, index)
    if eff_ext is not None and is_qmc_ext(eff_ext):
        return process_qmc(env, file_path, index)
    if raw_ext in (".kgg",):
        if not env.kgg_ok:
            return ConvertResult(file=file_name, error="kgg-dec.exe 未找到")
        args: list[str] = []
        if env.db_info.get("found") and env.db_info.get("path"):
            args += ["--db", env.db_info["path"]]
        args += ["--suffix", KGG_DEC_SUFFIX, file_path]

        source_dir = path.parent
        # 清理上次可能遗留的中间产物，避免无输出时误匹配旧文件
        _remove_kgg_artifacts(env.emitter, source_dir, file_name)

        dec_timeout = timeout_for_file(path, 60)
        try:
            output = run_process_with_timeout(env.kgg_dec_exe, args, dec_timeout, env.cancel, index)
            decoded_file = find_decoded_file(source_dir, file_name)
            if decoded_file is None:
                reason = _kgg_failure_reason(output)
                return ConvertResult(file=file_name, error=reason)
            return finalize_conversion(env, decoded_file, path, [], [])
        except RuntimeError as e:
            return ConvertResult(file=file_name, error=str(e))
        finally:
            # 成败/取消/超时都清理，避免残留物被下一次 find_decoded_file 误匹配
            _remove_kgg_artifacts(env.emitter, source_dir, file_name)
    if eff_ext in ("kgm", "kgma", "vpr"):
        return process_kgm(env, file_path, index)
    return ConvertResult(file=file_name, error=f"不支持的文件类型: {raw_ext}")
