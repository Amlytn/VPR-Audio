# -*- mode: python ; coding: utf-8 -*-
# VPR Audio onedir 打包配置：启动不经过 onefile 自解压，冷/热启动都更快。
# 用法：python scripts/build.py --onedir
# 产物：dist/VPR Audio/VPR Audio.exe（同目录带 _internal 与 ffmpeg/kgg-dec）
import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve().parent
FRONTEND = PROJECT_ROOT / "vpr" / "frontend"
ASSETS = PROJECT_ROOT / "assets"

a = Analysis(
    [str(PROJECT_ROOT / "vpr" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(FRONTEND), "frontend"),
        (str(ASSETS), "assets"),
    ],
    hiddenimports=[
        "vpr.decrypt.tc_tea",
        "vpr.decrypt.qmc",
        "vpr.decrypt.ncm",
        "vpr.decrypt.kgm",
        "vpr.decrypt.mmkv",
        "vpr.decrypt.qmc_keydb",
        "vpr.decrypt.sniff",
        "webview.platforms.edgechromium",
        "pystray._win32",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VPR Audio",
    console=False,
    disable_windowed_traceback=False,
    upx=True,
    icon=str(ASSETS / "app.ico") if (ASSETS / "app.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="VPR Audio",
)
