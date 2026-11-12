"""应用入口：窗口、托盘、关闭确认与单实例控制。"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

import webview

from .api import Api
from .picker import close_open as close_open_picker
from .settings import settings_path
from .winutil import effective_min_size, effective_window_size

APP_NAME = "VPR Audio"


class SingleInstance:
    """基于锁文件与 PID 存活检查的单实例控制，崩溃残留锁会自动接管。"""

    def __init__(self) -> None:
        import tempfile

        self.lock_path = Path(tempfile.gettempdir()) / "vpr-audio.lock"
        self._fh = None

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True  # 检测失败按存活处理

    def acquire(self) -> bool:
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if self._pid_alive(pid):
                return False
            try:
                self.lock_path.unlink()
            except OSError:
                return False
        try:
            self._fh = open(self.lock_path, "w")  # noqa: SIM115
            self._fh.write(str(__import__("os").getpid()))
            self._fh.flush()
            return True
        except OSError:
            return False

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
                self.lock_path.unlink()
            except OSError:
                pass


def _make_tray_icon_image():
    from PIL import Image

    icon_candidates = [
        get_resource_dir() / "assets" / "icon.png",
        get_resource_dir() / "frontend" / "icon.png",
        settings_tool_dir() / "assets" / "icon.png",
        settings_tool_dir() / "icon.png",
    ]
    for p in icon_candidates:
        if p.exists():
            try:
                return Image.open(p).convert("RGBA")
            except Exception:
                continue
    return Image.new("RGBA", (20, 20), (0, 0, 0, 0))


def settings_tool_dir() -> Path:
    from .settings import get_tool_dir

    return get_tool_dir()


class Tray:
    """系统托盘图标与右键菜单。"""

    def __init__(self, window, api: Api) -> None:
        self.window = window
        self.api = api
        self.icon = None
        self._start()

    def _start(self) -> None:
        import pystray

        image = _make_tray_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self._on_show, default=True),
            pystray.MenuItem("退出", self._on_quit),
        )
        self.icon = pystray.Icon("vpr-tray", image, APP_NAME, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _on_show(self, icon=None, item=None) -> None:
        try:
            self.window.show()
            # 仅最小化时 restore，避免把最大化窗口还原
            hwnd = self.api._native_hwnd()
            if hwnd:
                import ctypes

                if ctypes.windll.user32.IsIconic(hwnd):
                    self.window.restore()
        except Exception:
            pass

    def _on_quit(self, icon=None, item=None) -> None:
        if self.api.cancel.converting.is_set():
            try:
                confirmed = self.window.create_confirmation_dialog(
                    "确认退出", "正在转换中，确定要退出吗？未完成的转换会被中断。"
                )
            except Exception:
                confirmed = True
            if not confirmed:
                return
        self.api.cancel.request_cancel()
        self.api.cancel.kill_all()
        self.api.cancel.converting.clear()
        if self.icon is not None:
            self.icon.stop()
        try:
            self.window.destroy()
        except Exception:
            pass
        os_exit(0)

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass


def os_exit(code: int) -> None:
    import os

    os._exit(code)


def _on_closing(window, api: Api, tray: Tray | None) -> bool:
    """关闭事件处理，返回 False 阻止关闭。"""
    # 托盘创建失败时不隐藏窗口，避免无法唤回
    if api.settings.close_to_tray and tray is not None:
        window.hide()
        return False
    if api.cancel.converting.is_set():
        try:
            confirmed = window.create_confirmation_dialog(
                "确认退出", "正在转换中，确定要退出吗？未完成的转换会被中断。"
            )
        except Exception:
            confirmed = True
        if not confirmed:
            return False
        api.cancel.request_cancel()
        api.cancel.kill_all()
        api.cancel.converting.clear()
    if tray is not None:
        tray.stop()
    return True


# 保留标准窗口框架样式以启用 DWM 动画，再通过 WM_NCCALCSIZE 隐藏原生边框
GWL_STYLE = -16
GWL_WNDPROC = -4
WM_NCCALCSIZE = 0x0083
WS_OVERLAPPEDWINDOW = 0x00CF0000  # CAPTION | THICKFRAME | SYSMENU | MIN/MAXBOX
WS_CLIPCHILDREN = 0x02000000
WS_CLIPSIBLINGS = 0x04000000

# SetWindowPos flags: NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED
_SWP_KEEP = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020

_frame_old_proc = 0
_frame_proc_ref = None
_frame_user32 = None


def _frame_wndproc(h, msg, wp, lp):
    if msg == WM_NCCALCSIZE and wp:
        return 0
    u = _frame_user32
    if _frame_old_proc:
        return u.CallWindowProcW(_frame_old_proc, h, msg, wp, lp)
    return u.DefWindowProcW(h, msg, wp, lp)


def _apply_custom_frame(window) -> None:
    """设置标准窗口样式并挂接 WndProc 隐藏非客户区，支持 show 前后多次调用。"""
    global _frame_old_proc, _frame_proc_ref, _frame_user32
    try:
        import ctypes

        hwnd = int(window.native.Handle.ToInt64())
        u = ctypes.windll.user32
        _frame_user32 = u
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.CallWindowProcW.restype = ctypes.c_ssize_t
        u.CallWindowProcW.argtypes = [ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

        style = u.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        new_style = style | WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN | WS_CLIPSIBLINGS
        if new_style != style:
            u.SetWindowLongPtrW(hwnd, GWL_STYLE, new_style)

        if _frame_proc_ref is None:
            wndproc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
            )
            _frame_proc_ref = wndproc_type(_frame_wndproc)
        target = ctypes.cast(_frame_proc_ref, ctypes.c_void_p).value
        current = u.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
        if current != target:
            _frame_old_proc = current
            u.SetWindowLongPtrW(hwnd, GWL_WNDPROC, target)

        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, _SWP_KEEP)
    except Exception:
        pass


# WebView2 的 HTML5 File 对象不含本地路径，通过 pywebview 的 pywebviewFullPath 补齐
def _register_drop_bridge(window, emitter) -> None:
    try:
        def on_drop(event) -> None:
            try:
                data = event.get("dataTransfer") or {}
                files = data.get("files") or []
                paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
                if not paths:
                    if files:
                        emitter.emit_log(
                            "未获取到文件路径：压缩包/网盘里的文件请先解压或下载到本地再拖入", "warn"
                        )
                    else:
                        emitter.emit_log("未获取到文件", "warn")
                    return
                close_open_picker()
                window.evaluate_js(
                    f"window.__vprDrop && window.__vprDrop({json.dumps(paths, ensure_ascii=False)})"
                )
            except Exception:
                pass

        # 等待 DOM 就绪后再绑定 drop 事件
        body = None
        for _ in range(20):
            try:
                body = window.dom.body
            except Exception:
                body = None
            if body is not None:
                break
            time.sleep(0.25)
        if body is None:
            return
        body.events.drop += on_drop
        window.evaluate_js("window.__vprDropBridgeActive = true")
    except Exception as e:
        try:
            emitter.emit_log(f"拖放初始化失败，拖入文件可能无法识别: {e}", "warn")
        except Exception:
            pass


def _system_light_theme() -> bool:
    try:
        import winreg

        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return bool(val)
    except Exception:
        return False


def _theme_background(theme: str) -> str:
    return "#141414" if str(theme).strip() == "dark" else "#ffffff"


def _sync_background(window) -> None:
    """将窗口与 WebView2 底色同步为当前页面主题，避免浅色模式下闪黑帧。"""
    try:
        theme = window.evaluate_js("document.documentElement.getAttribute('data-theme')")
        html = _theme_background(theme)
        from System import Action
        from System.Drawing import Color, ColorTranslator

        color = ColorTranslator.FromHtml(html)
        native = window.native
        if native is None:
            return
        done = threading.Event()

        def apply() -> None:
            try:
                native.BackColor = color
                webview_ctl = getattr(getattr(native, "browser", None), "webview", None)
                if webview_ctl is not None:
                    webview_ctl.DefaultBackgroundColor = color
            except Exception:
                pass
            done.set()

        # WndProc 子类化后从非 UI 线程直接写 .NET 属性会死锁，需经 BeginInvoke 切到 UI 线程
        native.BeginInvoke(Action(apply))
        done.wait(0.3)
    except Exception:
        pass


def _patch_local_http_cache() -> None:
    """给 pywebview 内置静态文件响应补充 no-store，防止 WebView2 缓存旧前端资源。"""
    try:
        import bottle

        current = bottle.static_file
        if getattr(current, "_vpr_no_store", False):
            return

        def static_file(*args, **kwargs):
            resp = current(*args, **kwargs)
            try:
                resp.set_header("Cache-Control", "no-store, no-cache, must-revalidate")
                resp.set_header("Pragma", "no-cache")
            except Exception:
                pass
            return resp

        static_file._vpr_no_store = True
        bottle.static_file = static_file
    except Exception:
        pass


def _clear_webview_cache(storage: Path, version: str) -> None:
    """版本变更时清理 WebView2 的 HTTP 缓存（保留 localStorage）。"""
    marker = storage / ".vpr-version"
    try:
        previous = marker.read_text(encoding="utf-8").strip()
    except OSError:
        previous = ""
    if previous == version:
        return
    for sub in ("EBWebView/Default/Cache", "EBWebView/Default/Code Cache"):
        shutil.rmtree(storage / sub, ignore_errors=True)
    try:
        marker.write_text(version, encoding="utf-8")
    except OSError:
        pass


def _focus_existing_window() -> None:
    """唤起已运行的实例窗口。"""
    try:
        import ctypes

        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, APP_NAME)
        if not hwnd:
            return
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        else:
            u.ShowWindow(hwnd, 5)  # SW_SHOW
        u.SetForegroundWindow(hwnd)
    except Exception:
        pass


def main() -> None:
    single = SingleInstance()
    if not single.acquire():
        _focus_existing_window()
        print("VPR Audio 已在运行", file=sys.stderr)
        sys.exit(0)

    shown_flag = threading.Event()

    api = Api()
    win_w, win_h = effective_window_size()
    min_w, min_h = effective_min_size()
    window = webview.create_window(
        APP_NAME,
        url=FrontendLocator(),
        width=win_w,
        height=win_h,
        min_size=(min_w, min_h),
        js_api=api,
        frameless=True,
        easy_drag=False,  # 拖拽交由 vpr-shim.js 控制，避免与边缘缩放冲突
        on_top=False,
        hidden=True,      # 等待页面就绪后再显示，避免启动闪烁
        background_color=_theme_background("light" if _system_light_theme() else "dark"),
    )
    api.emitter.bind(window)

    def on_loaded() -> None:
        # 不要在这里重定义 __vprEvent：shim 已提供，覆盖会让进度与日志失去接收方
        _apply_custom_frame(window)
        _sync_background(window)
        window.evaluate_js("window.__vprIntroPrepare && window.__vprIntroPrepare()")
        try:
            window.evaluate_js("window.__vprStartIntro && window.__vprStartIntro()")
        except Exception:
            pass
        # 避免 WebView2 初始把焦点落到第一个可聚焦按钮上出现焦点框
        try:
            window.evaluate_js(
                "(function(){var b=document.body,a=document.activeElement;"
                "if(a&&a!==b){a.blur()}b.focus()})()"
            )
        except Exception:
            pass
        try:
            window.show()
        except Exception:
            pass
        threading.Thread(target=_register_drop_bridge, args=(window, api.emitter), daemon=True).start()

    window.events.loaded += on_loaded
    tray_holder: list[Tray | None] = [None]

    def on_closing_wrapper() -> bool:
        return _on_closing(window, api, tray_holder[0])

    window.events.closing += on_closing_wrapper

    def on_shown() -> None:
        shown_flag.set()
        # WinForms show 时可能重置窗口样式，再补一次框架设置
        _apply_custom_frame(window)
        if tray_holder[0] is None:
            try:
                tray_holder[0] = Tray(window, api)
            except Exception:
                tray_holder[0] = None

    window.events.shown += on_shown

    # 防止极端情况下 loaded 未触发导致窗口一直隐藏
    def _safety_show() -> None:
        if not shown_flag.is_set():
            try:
                window.show()
            except Exception:
                pass

    safety = threading.Timer(25.0, _safety_show)
    safety.daemon = True
    safety.start()

    import atexit

    atexit.register(single.release)
    # 启用本地 HTTP（确保 clipboard 等安全上下文 API 可用）并持久化 WebView 存储
    storage = settings_path().parent / "webview"
    try:
        storage.mkdir(parents=True, exist_ok=True)
    except OSError:
        import tempfile

        storage = Path(tempfile.gettempdir()) / "vpr-audio-storage"
        storage.mkdir(exist_ok=True)
    _patch_local_http_cache()
    _clear_webview_cache(storage, api.get_app_version())
    webview.start(
        debug="--vpr-debug" in sys.argv,
        http_server=True,
        private_mode=False,
        storage_path=str(storage),
    )


def get_resource_dir() -> Path:
    """随包资源根目录（打包后为 sys._MEIPASS，开发模式为源码目录）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


class FrontendLocator(str):
    def __new__(cls) -> "FrontendLocator":
        index = get_resource_dir() / "frontend" / "index.html"
        return str.__new__(cls, str(index))


if __name__ == "__main__":
    main()
