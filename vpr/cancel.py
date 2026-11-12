"""转换取消状态与子进程管理。

独立成轻量模块，避免应用启动期导入 Api 时连带加载 pycryptodome 等解密依赖。
"""
from __future__ import annotations

import subprocess
import threading


class CancelState:
    """线程安全的取消标志 + 正在运行子进程表"""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self.converting = threading.Event()
        self._procs: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def request_cancel(self) -> None:
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def reset(self) -> None:
        self._cancel.clear()

    def register(self, index: int, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[index] = proc

    def unregister(self, index: int) -> None:
        with self._lock:
            self._procs.pop(index, None)

    def kill_task(self, index: int) -> None:
        with self._lock:
            proc = self._procs.pop(index, None)
        if proc is not None:
            _kill_proc(proc)

    def kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
            self._procs.clear()
        for proc in procs:
            _kill_proc(proc)


def _kill_proc(proc: subprocess.Popen) -> None:
    """终止子进程。"""
    try:
        proc.kill()
    except OSError:
        pass
