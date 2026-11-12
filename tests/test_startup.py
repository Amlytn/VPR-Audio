"""启动时序回归测试：首帧对齐、font-display 与声纹条冻结。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "vpr" / "frontend" / "vpr-shim.js"
CSS = ROOT / "vpr" / "frontend" / "assets" / "index-iYjH7ZYe.css"


def test_built_css_font_display_is_swap():
    """避免冷启动字体加载超时退回系统默认字体，需保留 swap。"""
    css = CSS.read_text(encoding="utf-8")
    assert "font-display:optional" not in css
    assert "font-display:swap" in css


def test_shim_defines_intro_hooks():
    shim = SHIM.read_text(encoding="utf-8")
    assert "window.__vprIntroPrepare" in shim
    assert "window.__vprStartIntro" in shim
    assert "__vprReplayIntro" in shim


def test_shim_start_intro_shows_window_in_same_task():
    """__vprStartIntro 需在同一次 JS 任务内调用 win_show 并解除动画暂停。"""
    shim = SHIM.read_text(encoding="utf-8")
    start = shim.index("window.__vprStartIntro = function")
    body = shim[start : shim.index("window.__vprReplayIntro")]
    assert "win_show" in body
    assert "vpr-suspend" in body


def test_shim_prepare_freezes_wave_bars():
    """声纹条由 WAAPI 驱动，需在 prepare 中显式归零冻结。"""
    shim = SHIM.read_text(encoding="utf-8")
    prepare = shim.index("window.__vprIntroPrepare = function")
    body = shim[prepare : shim.index("window.__vprStartIntro = function")]
    assert "scaleY(0)" in body
    assert "getAnimations" in body


def test_shim_intro_fallback_does_not_replay():
    """正常启动调过 __vprStartIntro 后，30s 兜底定时器不应重复触发入场动画。"""
    shim = SHIM.read_text(encoding="utf-8")
    start = shim.index("window.__vprStartIntro = function")
    assert shim[start : start + 220].find("__vprIntroStarted = true") != -1
    fallback_end = shim.index("}, 30000)")
    assert "__vprIntroStarted" in shim[fallback_end - 500 : fallback_end]


def test_api_has_win_show():
    from vpr.api import Api

    assert callable(getattr(Api, "win_show", None))


def test_app_has_background_sync():
    from vpr import app

    assert hasattr(app, "_sync_background")
