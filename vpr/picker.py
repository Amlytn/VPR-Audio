"""原生选择框打开期间保持主窗口可接收拖放。

WinForms 的 OpenFileDialog.ShowDialog(owner) 会禁用主窗口，导致 OLE 拖放无法投递。
这里在对话框打开期间定时恢复主窗口的启用状态，并在拖放发生时关闭对话框。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes

WM_CLOSE = 0x0010
_GW_OWNER = 4
SW_RESTORE = 9
_SEARCH_INTERVAL_SECS = 0.05

_picker_lock = threading.Lock()
_picker_hwnd = 0


def _user32():
    u = ctypes.windll.user32
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetWindow.restype = wintypes.HWND
    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsWindow.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.IsWindowEnabled.argtypes = [wintypes.HWND]
    u.IsWindowEnabled.restype = wintypes.BOOL
    u.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    u.EnableWindow.restype = wintypes.BOOL
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    return u


def _main_hwnd() -> int:
    try:
        import webview

        native = getattr(webview.windows[0], "native", None)
        if native is None:
            return 0
        return int(native.Handle.ToInt64())
    except Exception:
        return 0


def _find_picker(u, owner_hwnd: int) -> int:
    """owner 是主窗口的 #32770 选择框。按 owner 匹配而不是 PID：选择框在不同
    Windows 版本上可能由独立进程承载（实测 Win11 会托管到另一个 python 进程）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lp):
        if u.IsWindowVisible(hwnd) and u.GetWindow(hwnd, _GW_OWNER) == owner_hwnd:
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, cls, 64)
            if cls.value == "#32770":
                found.append(int(hwnd))
        return True

    u.EnumWindows(_enum, 0)
    return found[0] if found else 0


def _watch(u, main_hwnd: int, stop: threading.Event) -> None:
    global _picker_hwnd

    found = False
    last_search = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if not found and now - last_search >= _SEARCH_INTERVAL_SECS:
            last_search = now
            dialog = _find_picker(u, main_hwnd)
            if dialog:
                with _picker_lock:
                    _picker_hwnd = dialog
                found = True
        # 选择框每把主窗口禁用一次就恢复一次（对话框重新激活时可能再次禁用）
        if not u.IsWindowEnabled(main_hwnd):
            u.EnableWindow(main_hwnd, True)
        stop.wait(0.01)


@contextmanager
def keep_main_window_enabled():
    """包住一次原生选择框调用：选择框把主窗口禁用后立即恢复。非 Windows /
    拿不到主窗口时静默降级（行为退回原样）。"""
    stop = threading.Event()
    thread = None
    hwnd = _main_hwnd() if os.name == "nt" else 0
    if hwnd:
        u = _user32()
        thread = threading.Thread(target=_watch, args=(u, hwnd, stop), daemon=True)
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1)


def existing() -> int:
    """当前仍开着的选择框句柄（没有则 0）。选择框期间主窗口是启用的，
    用户可能再点一次「添加文件」——调用方据此把已开的选择框拉到前面，
    而不是叠出第二个。"""
    with _picker_lock:
        hwnd = _picker_hwnd
    if not hwnd:
        return 0
    try:
        u = _user32()
        if u.IsWindow(hwnd) and u.GetWindow(hwnd, _GW_OWNER) == _main_hwnd():
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, cls, 64)
            if cls.value == "#32770":
                return hwnd
    except Exception:
        pass
    return 0


def focus(hwnd: int) -> None:
    """把已开的选择框切到前台（重复点击「添加文件」时）。"""
    try:
        u = _user32()
        u.ShowWindow(hwnd, SW_RESTORE)
        u.SetForegroundWindow(hwnd)
    except Exception:
        pass


def close_open() -> None:
    """收起当前打开的选择框（拖放落下时调用；没有则无操作）。"""
    with _picker_lock:
        hwnd = _picker_hwnd
    if not hwnd:
        return
    try:
        u = _user32()
        # 句柄可能已被回收：只对「仍然属于主窗口的对话框」发关闭
        if u.IsWindow(hwnd) and u.GetWindow(hwnd, _GW_OWNER) == _main_hwnd():
            u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    except Exception:
        pass
