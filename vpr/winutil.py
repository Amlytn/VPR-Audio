"""窗口尺寸与 DPI 计算工具。"""
from __future__ import annotations

import ctypes
import os

# 默认窗口尺寸（逻辑像素），小屏下按工作区自动缩小
WINDOW_SIZE = (1440, 900)
MIN_SIZE = (1280, 780)

_SPI_GETWORKAREA = 0x0030
_LOGPIXELSX = 88


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def work_area() -> tuple[int, int] | None:
    """主屏工作区（扣除任务栏，逻辑像素）。"""
    if os.name != "nt":
        return None
    try:
        r = _RECT()
        if ctypes.windll.user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
            w, h = r.right - r.left, r.bottom - r.top
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return None


def effective_window_size() -> tuple[int, int]:
    """初始窗口尺寸：1366×768 这类小屏上不超出工作区（否则底部按钮会被裁掉）。"""
    w, h = WINDOW_SIZE
    wa = work_area()
    return (min(w, wa[0]), min(h, wa[1])) if wa else (w, h)


def effective_min_size() -> tuple[int, int]:
    """最小尺寸：同样按工作区收窄，但保留一个可用的下限。"""
    w, h = MIN_SIZE
    wa = work_area()
    if wa:
        return min(w, max(640, wa[0])), min(h, max(480, wa[1]))
    return w, h


def dpi_scale() -> float:
    """系统 DPI 缩放（96 = 1.0）；取不到时按 1.0。"""
    if os.name != "nt":
        return 1.0
    try:
        user32 = ctypes.windll.user32
        hdc = user32.GetDC(0)
        try:
            scale = ctypes.windll.gdi32.GetDeviceCaps(hdc, _LOGPIXELSX) / 96.0
        finally:
            user32.ReleaseDC(0, hdc)
        return scale if scale > 0 else 1.0
    except Exception:
        return 1.0


def min_size_physical() -> tuple[int, int]:
    """最小尺寸换算到物理像素（拖拽缩放的 SetWindowPos 用）。"""
    w, h = effective_min_size()
    scale = dpi_scale()
    return int(w * scale), int(h * scale)
