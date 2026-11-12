"""暴露给前端的 IPC 方法，通过 window.pywebview.api 调用。"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from . import __version__, history, picker, settings, winutil
from .cancel import CancelState
from .decrypt.qmc_keydb import QmcKeySource
from .events import Emitter


@contextmanager
def _ole_sta():
    """文件对话框线程需要初始化为 OLE STA，否则不支持从资源管理器拖放文件进对话框。"""
    import ctypes

    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    initialized = hr >= 0
    try:
        yield
    finally:
        if initialized:
            ole32.CoUninitialize()


def _sta_dialog(func):
    """在 STA 模式下弹出文件选择框并保持主窗口可拖放，同时防止重复打开多个选择框。"""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self._dialog_lock.acquire(blocking=False):
            hwnd = picker.existing()
            if hwnd:
                picker.focus(hwnd)
            return None
        try:
            with _ole_sta(), picker.keep_main_window_enabled():
                return func(self, *args, **kwargs)
        finally:
            self._dialog_lock.release()

    return wrapper


# 延迟导入 process，避免启动阶段加载解密依赖
def _process():
    from . import process

    return process


def _is_sqlite_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _build_qmc_status(source: QmcKeySource) -> dict:
    return {
        "valid": source.is_usable(),
        "path": source.sqlite_path or source.mmkv_path,
        "kind": "sqlite" if source.sqlite_path else ("mmkv" if source.mmkv_path else None),
        "entries": source.entry_count(),
    }


def _load_qmc_source(path_str: str) -> QmcKeySource:
    path = Path(path_str)
    if _is_sqlite_file(path):
        return QmcKeySource.load_sqlite(path)
    return QmcKeySource.load_mmkv(path)


_TOOL_SEARCH_DIRS: list[Path] = []


def _find_tool_exe(name: str) -> Path:
    """按优先级查找外部工具：程序目录 → 当前工作目录"""
    candidates = [settings.get_tool_dir(), Path.cwd()]
    _TOOL_SEARCH_DIRS.clear()
    _TOOL_SEARCH_DIRS.extend(candidates)
    for d in candidates:
        p = d / name
        if p.exists():
            return p
    return settings.get_tool_dir() / name


def _tool_missing_message(name: str) -> str:
    dirs = "；".join(str(d) for d in _TOOL_SEARCH_DIRS) or str(settings.get_tool_dir())
    return f"{name} 未找到（已查找: {dirs}）——请把 {name} 放到程序 exe 同目录后重试"


def _summarize_version_line(first_line: str) -> str | None:
    """提取 ffmpeg -version 首行版本号，去掉构建后缀"""
    parts = first_line.split()
    if len(parts) < 3:
        return None
    token = parts[2]
    for suffix in ("-essentials_build", "-full_build"):
        token = token.split(suffix)[0]
    # git 构建日期格式只保留 YYYY-MM-DD
    if len(token) >= 10 and token[4] == "-":
        return token[:10]
    return token


class Api:
    """pywebview js_api 对象。"""

    def __init__(self) -> None:
        self.emitter = Emitter()
        self.cancel = CancelState()
        self.settings = settings.load_settings()
        self._settings_lock = threading.Lock()
        self._gesture_lock = threading.Lock()
        self._gesture_gen = 0
        self._dialog_lock = threading.Lock()
        self._batch_lock = threading.Lock()
        self._qmc_cache_lock = threading.Lock()
        self._qmc_cache_key: tuple | None = None
        self._qmc_cache = None

    def _update_setting(self, mutate) -> None:
        with self._settings_lock:
            mutate(self.settings)
            try:
                settings.save_settings(self.settings)
            except OSError as e:
                self.emitter.emit_log(f"设置保存失败（不影响本次操作）: {e}", "warn")

    def _save(self) -> None:
        with self._settings_lock:
            try:
                settings.save_settings(self.settings)
            except OSError as e:
                self.emitter.emit_log(f"设置保存失败（不影响本次操作）: {e}", "warn")

    def get_app_version(self) -> str:
        return __version__

    def get_ffmpeg_version(self) -> str | None:
        exe = _find_tool_exe("ffmpeg.exe")
        if not exe.exists():
            return None
        try:
            out = subprocess.run(
                [str(exe), "-version"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            first = (out.stdout or b"").decode("utf-8", "replace").splitlines()
            if not first or out.returncode != 0:
                return None
            return _summarize_version_line(first[0])
        except (OSError, subprocess.TimeoutExpired):
            return None

    def get_saved_paths(self) -> dict:
        s = self.settings
        return {
            "select_output_path": s.select_output_path,
            "output_format": s.output_format,
            "concurrency": s.concurrency,
            "close_to_tray": s.close_to_tray,
            "qmc_keydb_path": s.qmc_keydb_path,
            "qmc_manual_ekeys": s.qmc_manual_ekeys,
        }

    def save_output_format(self, params=None, **kw) -> None:
        fmt = (params or kw).get("format") if isinstance(params, dict) else params
        self._update_setting(lambda s: setattr(s, "output_format", fmt))

    def save_concurrency(self, params=None, **kw) -> None:
        raw = (params or kw).get("concurrency") if isinstance(params, dict) else params
        self._update_setting(lambda s: setattr(s, "concurrency", max(1, min(6, int(raw)))))

    def save_close_to_tray(self, params=None, **kw) -> None:
        raw = (params or kw).get("enabled") if isinstance(params, dict) else params
        self._update_setting(lambda s: setattr(s, "close_to_tray", bool(raw)))

    _INPUT_FILTER = "加密音频 (*.kgg;*.kgm;*.kgma;*.vpr;*.kgm.flac;*.vpr.flac;*.ncm);;QQ 音乐加密音频 (*.qmc0;*.qmc2;*.qmc3;*.qmc4;*.qmc6;*.qmc8;*.qmcflac;*.qmcogg;*.tkm;*.bkcmp3;*.bkcm4a;*.bkcflac;*.bkcwav;*.bkcape;*.bkcogg;*.bkcwma;*.mmp4;*.mflac;*.mgg;*.mflac0;*.mflac1;*.mflaca;*.mflach;*.mflacl;*.mflacm;*.mgg0;*.mgg1;*.mgga;*.mggh;*.mggl;*.mggm;*.666c6163;*.6d7033;*.6f6767;*.6d3461;*.776176;*.mflac.cache;*.mgg.cache);;所有文件 (*.*)"
    # pywebview 的文件过滤描述不能包含 "."，否则会抛 ValueError
    _DB_FILTER = ["KGMusicV3 数据库 (*.db)", "所有文件 (*.*)"]
    _QMC_KEYDB_FILTER = ["QQ 音乐密钥库 (*.db;*.sqlite;*.mmkv)", "所有文件 (*.*)"]

    @_sta_dialog
    def select_input(self) -> list[str]:
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            directory=self.settings.select_input_path or "",
            allow_multiple=True,
            file_types=self._INPUT_FILTER.split(";;"),
        )
        if not result:
            return []
        first = str(result[0])
        self._update_setting(lambda s: setattr(s, "select_input_path", str(Path(first).parent)))
        return [str(p) for p in result]

    @_sta_dialog
    def select_folder(self) -> str | None:
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FOLDER_DIALOG, directory=self.settings.select_input_path or ""
        )
        if not result:
            return None
        path = str(result[0])
        self._update_setting(lambda s: setattr(s, "select_input_path", path))
        return path

    @_sta_dialog
    def select_output_folder(self) -> str | None:
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FOLDER_DIALOG, directory=self.settings.select_output_path or ""
        )
        if not result:
            return None
        path = str(result[0])
        self._update_setting(lambda s: setattr(s, "select_output_path", path))
        return path

    @_sta_dialog
    def select_db(self) -> dict | None:
        import webview

        current = settings.find_db_path(self.settings)
        directory = str(Path(current["path"]).parent) if current.get("found") else ""
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            directory=directory,
            file_types=self._DB_FILTER,
        )
        if not result:
            return None
        path_str = str(result[0])
        if not settings.is_plausible_db_file(Path(path_str)):
            raise Exception("选择的文件不是有效的 KGMusicV3.db（文件不存在或为空）")
        self._update_setting(lambda s: setattr(s, "db_path", path_str))
        return settings.find_db_path(self.settings)

    @_sta_dialog
    def select_qmc_keydb(self) -> dict | None:
        import webview

        directory = ""
        if self.settings.qmc_keydb_path:
            directory = str(Path(self.settings.qmc_keydb_path).parent)
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            directory=directory,
            file_types=self._QMC_KEYDB_FILTER,
        )
        if not result:
            return None
        path_str = str(result[0])
        try:
            source = _load_qmc_source(path_str)
        except ValueError as e:
            raise Exception(str(e))
        self._update_setting(lambda s: setattr(s, "qmc_keydb_path", path_str))
        return _build_qmc_status(source)

    def get_qmc_db_status(self) -> dict:
        path = self.settings.qmc_keydb_path
        if not path:
            return {"valid": False, "path": None, "kind": None, "entries": 0}
        try:
            st = os.stat(path)
            cache_key = (path, st.st_mtime_ns, st.st_size)
        except OSError:
            return {"valid": False, "path": path, "kind": None, "entries": 0}
        with self._qmc_cache_lock:
            if self._qmc_cache_key == cache_key:
                return _build_qmc_status(self._qmc_cache)
        try:
            source = _load_qmc_source(path)
        except ValueError:
            return {"valid": False, "path": path, "kind": None, "entries": 0}
        with self._qmc_cache_lock:
            self._qmc_cache_key = cache_key
            self._qmc_cache = source
        return _build_qmc_status(source)

    def save_qmc_manual(self, params=None, **kw) -> None:
        text = (params or kw).get("text") if isinstance(params, dict) else params
        self._update_setting(lambda s: setattr(s, "qmc_manual_ekeys", text))

    def get_db_status(self) -> dict:
        return settings.find_db_path(self.settings)

    def refresh_db(self) -> dict:
        """默认位置存在密钥库时切回自动检测并清除手动路径，否则保留手动路径。"""
        info = settings.find_default_db()
        if info is not None:
            if self.settings.db_path is not None:
                self._update_setting(lambda s: setattr(s, "db_path", None))
            return info
        return settings.find_db_path(self.settings)

    def scan_input(self, params=None, **kw) -> list[dict]:
        """扫描输入文件或目录，自动排除输出目录。"""
        paths = (params or kw).get("paths") if isinstance(params, dict) else params
        excluded = [settings.get_tool_dir() / "kgm-vpr-out-ui"]
        if self.settings.select_output_path:
            excluded.append(Path(self.settings.select_output_path))

        files: list = []
        proc = _process()
        for p in paths or []:
            path = Path(p)
            if path.is_dir():
                files.extend(proc.scan_directory(path, excluded))
            elif path.is_file() and proc.is_supported_file(path):
                try:
                    size = path.stat().st_size
                    files.append(
                        proc.FileItem(
                            path=str(path), name=path.name, size=size, formatted_size=proc.format_size(size)
                        )
                    )
                except OSError:
                    continue
        return [f.__dict__ for f in files]

    def open_folder(self, params=None, **kw) -> None:
        path = (params or kw).get("path") if isinstance(params, dict) else params
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.run(["xdg-open", path], check=False)

    def get_history(self) -> list[dict]:
        return history.load()

    def clear_history(self) -> None:
        history.clear()

    def cancel_convert(self) -> None:
        self.cancel.request_cancel()
        self.cancel.kill_all()

    def win_minimize(self) -> None:
        self._syscommand(0xF020)

    def win_show(self) -> None:
        try:
            import webview

            webview.windows[0].show()
        except Exception:
            pass

    def win_maximize(self) -> None:
        self._syscommand(0xF030)

    def win_restore(self) -> None:
        self._syscommand(0xF120)

    def win_close(self) -> None:
        """发送 WM_CLOSE 触发关闭事件（保持系统过渡动画并经过关闭确认）。"""
        import ctypes

        def post() -> None:
            try:
                hwnd = self._native_hwnd()
                if hwnd:
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
            except Exception:
                pass

        self._invoke_on_ui(post)

    def _syscommand(self, cmd: int) -> None:
        import ctypes

        def post() -> None:
            try:
                hwnd = self._native_hwnd()
                if hwnd:
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0112, cmd, 0)
            except Exception:
                pass

        self._invoke_on_ui(post)

    def win_is_maximized(self) -> bool:
        hwnd = self._native_hwnd()
        if hwnd:
            try:
                import ctypes

                return bool(ctypes.windll.user32.IsZoomed(hwnd))
            except Exception:
                pass
        import webview

        return bool(webview.windows[0].maximized)

    def win_maximize_toggle(self) -> None:
        self._syscommand(0xF120 if self.win_is_maximized() else 0xF030)

    def win_drag_start(self) -> None:
        hwnd = self._native_hwnd()
        if hwnd:
            self._start_gesture(hwnd, None)

    def win_resize_start(self, params=None, **kw) -> None:
        p = params if isinstance(params, dict) else kw
        direction = str(p.get("direction", ""))
        if not direction:
            return
        hwnd = self._native_hwnd()
        if hwnd:
            self._start_gesture(hwnd, direction)

    def win_gesture_stop(self) -> None:
        with self._gesture_lock:
            self._gesture_gen += 1

    def _start_gesture(self, hwnd: int, direction: str | None) -> None:
        """后台线程轮询光标位移并用 SetWindowPos 绝对定位，避免逐帧 IPC 导致的拖拽延迟。"""
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        rect = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        start_pt = wt.POINT()
        if not user32.GetCursorPos(ctypes.byref(start_pt)):
            return

        with self._gesture_lock:
            self._gesture_gen += 1
            gen = self._gesture_gen
        min_w, min_h = winutil.min_size_physical()

        start_left, start_top = rect.left, rect.top
        start_right, start_bottom = rect.right, rect.bottom
        start_x, start_y = start_pt.x, start_pt.y

        def loop() -> None:
            begin = time.monotonic()
            while time.monotonic() - begin < 30:
                with self._gesture_lock:
                    if gen != self._gesture_gen:
                        break
                if not (user32.GetAsyncKeyState(0x01) & 0x8000):
                    break
                cur = wt.POINT()
                if not user32.GetCursorPos(ctypes.byref(cur)):
                    break
                dx = cur.x - start_x
                dy = cur.y - start_y
                if direction is None:
                    user32.SetWindowPos(
                        hwnd,
                        0,
                        start_left + dx,
                        start_top + dy,
                        0,
                        0,
                        0x0001 | 0x0004 | 0x0010,  # NOSIZE | NOZORDER | NOACTIVATE
                    )
                else:
                    left, top, right, bottom = start_left, start_top, start_right, start_bottom
                    if "left" in direction:
                        left += dx
                    if "right" in direction:
                        right += dx
                    if "top" in direction:
                        top += dy
                    if "bottom" in direction:
                        bottom += dy
                    if right - left < min_w:
                        if "left" in direction:
                            left = right - min_w
                        else:
                            right = left + min_w
                    if bottom - top < min_h:
                        if "top" in direction:
                            top = bottom - min_h
                        else:
                            bottom = top + min_h
                    user32.SetWindowPos(
                        hwnd,
                        0,
                        left,
                        top,
                        right - left,
                        bottom - top,
                        0x0004 | 0x0010,  # NOZORDER | NOACTIVATE
                    )
                time.sleep(0.006)

        threading.Thread(target=loop, daemon=True).start()

    def _native_hwnd(self) -> int:
        import webview

        w = webview.windows[0]
        native = getattr(w, "native", None)
        if native is None:
            return 0
        try:
            return int(native.Handle.ToInt64())
        except Exception:
            return 0

    def _invoke_on_ui(self, fn) -> None:
        try:
            import webview

            native = getattr(webview.windows[0], "native", None)
            if native is None:
                return
            import System

            native.Invoke(System.Action(fn))
        except Exception:
            fn()

    def start_convert(self, params=None, **kw) -> tuple:
        """执行批量转换，同时只允许运行一个批次。"""
        if not self._batch_lock.acquire(blocking=False):
            raise Exception("转换进行中，请等待当前批次完成或取消")
        try:
            return self._convert_batch(params, kw)
        finally:
            self._batch_lock.release()

    def _convert_batch(self, params, kw) -> tuple:
        options = (params or kw).get("options") if isinstance(params, dict) else params
        if not isinstance(options, dict):
            options = dict(params or kw)
        files = options.get("files", [])
        fmt = options.get("format", "mp3")
        out_folder = options.get("output_folder")
        concurrency = options.get("concurrency") or 3
        concurrency = max(1, min(6, int(concurrency)))

        tool_dir = settings.get_tool_dir()
        out_dir = Path(out_folder) if out_folder else tool_dir / "kgm-vpr-out-ui"
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        needs_kgg = any(Path(p).suffix.lower().lstrip(".") == "kgg" for p in files)

        kgg_dec_exe = _find_tool_exe("kgg-dec.exe")
        kgg_ok = kgg_dec_exe.exists()
        if needs_kgg and not kgg_ok:
            self.emitter.emit_log(_tool_missing_message("kgg-dec.exe"), "warn")
        ffmpeg_exe = _find_tool_exe("ffmpeg.exe")
        ffmpeg_ok = ffmpeg_exe.exists()
        if not ffmpeg_ok:
            self.emitter.emit_log(_tool_missing_message("ffmpeg.exe"), "warn")

        if needs_kgg:
            db_info = settings.find_db_path(self.settings)
            if not db_info["found"]:
                self.emitter.emit_log("未检测到 KGG 密钥库，酷狗系文件可能解密失败", "warn")
        else:
            db_info = {"found": False, "path": None, "source": None}

        # QMC 密钥源加载失败仅告警，不影响 QMCv1 与内嵌 ekey 文件
        qmc_keys = QmcKeySource.empty()
        if self.settings.qmc_keydb_path:
            try:
                qmc_keys = _load_qmc_source(self.settings.qmc_keydb_path)
            except ValueError as e:
                self.emitter.emit_log(f"QQ 音乐密钥库加载失败: {e}", "warn")
        if self.settings.qmc_manual_ekeys:
            qmc_keys.set_manual_text(self.settings.qmc_manual_ekeys)

        proc = _process()
        self.cancel.reset()
        self.cancel.converting.set()
        total = len(files)

        file_sizes = []
        for p in files:
            try:
                file_sizes.append(Path(p).stat().st_size)
            except OSError:
                file_sizes.append(0)

        results: list = [None] * total
        results_lock = threading.Lock()

        def run_one(i: int, fp: str) -> None:
            if self.cancel.is_cancelled():
                return

            name = Path(fp).name
            try:
                env = proc.ConvertEnv(
                    emitter=self.emitter,
                    cancel=self.cancel,
                    out_dir=out_dir,
                    kgg_dec_exe=kgg_dec_exe,
                    ffmpeg_exe=ffmpeg_exe,
                    kgg_ok=kgg_ok,
                    ffmpeg_ok=ffmpeg_ok,
                    db_info=db_info,
                    format=fmt,
                    qmc_keys=qmc_keys,
                    index=i,
                    total=total,
                )
                self.emitter.emit_progress(
                    proc.ProgressInfo(current=i, total=total, index=i, status="processing", percent=None)
                )
                try:
                    result = proc.process_file(env, fp, i)
                except Exception as e:
                    result = proc.ConvertResult(file=name, error=f"任务异常: {e}")
            except Exception as e:
                result = proc.ConvertResult(file=name, error=f"任务异常: {e}")

            with results_lock:
                results[i] = result
            status = "done" if result.error is None else ("cancelled" if result.error == "用户取消" else "failed")
            percent = None if status == "cancelled" else 100
            self.emitter.emit_progress(
                proc.ProgressInfo(current=i + 1, total=total, index=i, status=status, percent=percent)
            )

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                for i, fp in enumerate(files):
                    pool.submit(run_one, i, fp)
        finally:
            self.cancel.converting.clear()
            self.cancel.kill_all()

        cancelled = self.cancel.is_cancelled()
        success = sum(1 for r in results if r is not None and r.error is None)
        failed = sum(1 for r in results if r is not None and r.error is not None and r.error != "用户取消")
        if not cancelled:
            failed += sum(1 for r in results if r is None)
        cancelled_count = max(total - success - failed, 0)

        if cancelled:
            self.emitter.emit_log(f"转换已取消，已完成: {success + failed}/{total}", "warn")

        history_files: list[history.HistoryFile] = []
        for i, fp in enumerate(files):
            r = results[i]
            if r is not None:
                history_files.append(history.HistoryFile(name=r.file, size=file_sizes[i], error=r.error))
            else:
                history_files.append(
                    history.HistoryFile(
                        name=Path(fp).name,
                        size=file_sizes[i],
                        error="未开始（批次已取消）" if cancelled else "任务异常（未返回结果）",
                    )
                )

        entry = history.HistoryEntry(
            time_ms=int(time.time() * 1000),
            total=total,
            success=success,
            failed=failed,
            cancelled=cancelled_count,
            duration_secs=int(time.monotonic() - started),
            output_dir=str(out_dir),
            format=fmt,
            concurrency=concurrency,
            files=history_files,
        )
        try:
            history.append(entry)
        except OSError as e:
            self.emitter.emit_log(f"任务记录写入失败: {e}", "warn")

        def _result(r, fp):
            if r is not None:
                return r.__dict__
            return {"file": Path(fp).name, "error": "用户取消" if cancelled else "任务异常"}

        return ([_result(r, fp) for r, fp in zip(results, files)], str(out_dir), cancelled)
