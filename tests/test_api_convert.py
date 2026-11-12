"""start_convert 的并发语义：单批次锁 + 有界 worker 池。"""
import threading
import time

import pytest

from vpr import api as api_mod
from vpr import process
from vpr.api import Api


def test_start_convert_rejects_concurrent_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    api = Api()
    assert api._batch_lock.acquire(blocking=False)
    try:
        with pytest.raises(Exception, match="转换进行中"):
            api.start_convert({"files": [], "output_folder": str(tmp_path / "out")})
    finally:
        api._batch_lock.release()


def test_start_convert_bounds_worker_concurrency(tmp_path, monkeypatch):
    """6 个文件、concurrency=2：同时最多 2 个在跑，结果齐全，批次锁释放。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_process_file(env, fp, index):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return process.ConvertResult(file=fp, error=None)

    monkeypatch.setattr(process, "process_file", fake_process_file)
    monkeypatch.setattr(api_mod, "_process", lambda: process)

    api = Api()
    files = [str(tmp_path / f"f{i}.ncm") for i in range(6)]
    results, _out_dir, cancelled = api.start_convert(
        {"files": files, "format": "mp3", "output_folder": str(tmp_path / "out"), "concurrency": 2}
    )

    assert cancelled is False
    assert len(results) == 6
    assert all(r["error"] is None for r in results)
    assert peak <= 2
    assert api._batch_lock.locked() is False
