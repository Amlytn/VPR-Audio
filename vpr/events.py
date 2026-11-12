"""后端向前端推送事件的统一出口。"""
from __future__ import annotations

import json
import threading


class Emitter:
    """封装前端全局回调调用，窗口未绑定或已关闭时静默忽略。"""

    def __init__(self) -> None:
        self._window = None
        self._js_func: str = "__vprEvent"
        self._lock = threading.Lock()

    def bind(self, window) -> None:
        with self._lock:
            self._window = window

    def _dispatch(self, event: str, payload) -> None:
        with self._lock:
            window = self._window
        if window is None:
            return
        try:
            js = f"{self._js_func}({json.dumps(event)}, {json.dumps(payload, ensure_ascii=False)})"
            window.evaluate_js(js)
        except Exception:
            # 窗口正在关闭等场景：推送失败不致命
            pass

    def emit_progress(self, info) -> None:
        """转换进度事件"""
        from .process import ProgressInfo

        if isinstance(info, ProgressInfo):
            payload = {
                "current": info.current,
                "total": info.total,
                "index": info.index,
                "status": info.status,
                "percent": info.percent,
            }
        else:
            payload = info
        self._dispatch("convert-progress", payload)

    def emit_log(self, message: str, level: str = "info") -> None:
        """日志事件（payload 为 [message, level]）"""
        self._dispatch("convert-log", [message, level])
