# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：python scripts/build.py 或 pyinstaller scripts/VPR-Audio.spec

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
        (str(FRONTEND), "frontend"),   # 前端构建产物（含 vpr-shim.js）
        (str(ASSETS), "assets"),       # 托盘/应用图标
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
    a.binaries,
    a.datas,
    [],
    name="VPR Audio",
    console=False,           # 桌面应用不出控制台窗口
    disable_windowed_traceback=False,
    upx=True,
    icon=str(ASSETS / "app.ico") if (ASSETS / "app.ico").exists() else None,
)
