"""关闭窗口、尺寸自适应与缓存清理测试。"""
import threading
from types import SimpleNamespace

from vpr.app import _on_closing


class FakeWindow:
    def __init__(self) -> None:
        self.hidden = False

    def hide(self) -> None:
        self.hidden = True

    def create_confirmation_dialog(self, *args, **kwargs) -> bool:
        return True


class FakeTray:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def make_api(close_to_tray: bool = True):
    return SimpleNamespace(
        settings=SimpleNamespace(close_to_tray=close_to_tray),
        cancel=SimpleNamespace(converting=threading.Event()),
    )


def test_close_to_tray_without_tray_does_not_hide():
    window = FakeWindow()
    assert _on_closing(window, make_api(True), None) is True
    assert window.hidden is False


def test_close_to_tray_with_tray_hides():
    window = FakeWindow()
    assert _on_closing(window, make_api(True), FakeTray()) is False
    assert window.hidden is True


def test_winutil_effective_sizes_fit_work_area(monkeypatch):
    """小屏（1366×768）上初始/最小尺寸不能超出工作区，否则底部按钮被裁掉。"""
    from vpr import winutil

    monkeypatch.setattr(winutil, "work_area", lambda: (1366, 728))
    assert winutil.effective_window_size() == (1366, 728)
    assert winutil.effective_min_size() == (1280, 728)

    monkeypatch.setattr(winutil, "work_area", lambda: (1920, 1040))
    assert winutil.effective_window_size() == (1440, 900)
    assert winutil.effective_min_size() == (1280, 780)

    monkeypatch.setattr(winutil, "work_area", lambda: None)
    assert winutil.effective_window_size() == (1440, 900)
    assert winutil.effective_min_size() == (1280, 780)


def test_dialog_guard_rejects_second_open(monkeypatch):
    """选择框期间主窗口保持可交互（见 picker），必须防重入：第二个
    「添加文件」不能再叠一个对话框，而是把已开的拉到前面。"""
    import threading

    from vpr import api, picker

    class Fake:
        def __init__(self):
            self._dialog_lock = threading.Lock()

        @api._sta_dialog
        def pick(self):
            return "opened"

    fake = Fake()
    assert fake.pick() == "opened"

    focused = []
    monkeypatch.setattr(picker, "existing", lambda: 0x1234)
    monkeypatch.setattr(picker, "focus", lambda hwnd: focused.append(hwnd))
    fake._dialog_lock.acquire()
    try:
        assert fake.pick() is None
    finally:
        fake._dialog_lock.release()
    assert focused == [0x1234]


def test_clear_webview_cache_only_on_version_change(tmp_path):
    from vpr.app import _clear_webview_cache

    cache = tmp_path / "EBWebView" / "Default" / "Cache"
    code_cache = tmp_path / "EBWebView" / "Default" / "Code Cache"
    cache.mkdir(parents=True)
    (cache / "entry").write_text("x")
    code_cache.mkdir(parents=True)

    _clear_webview_cache(tmp_path, "2.0.1")
    assert not cache.exists()
    assert not code_cache.exists()
    assert (tmp_path / ".vpr-version").read_text(encoding="utf-8") == "2.0.1"

    # 同一版本不清缓存（否则每次启动都丢掉 V8/HTTP 缓存）
    cache.mkdir(parents=True)
    (cache / "entry2").write_text("y")
    _clear_webview_cache(tmp_path, "2.0.1")
    assert cache.exists()


def test_patch_local_http_cache_is_idempotent():
    import bottle

    from vpr.app import _patch_local_http_cache

    _patch_local_http_cache()
    patched = bottle.static_file
    _patch_local_http_cache()
    assert bottle.static_file is patched
    assert getattr(patched, "_vpr_no_store", False)
