#!/usr/bin/env python
"""VPR Audio 打包脚本。

用法：
    .venv/Scripts/python scripts/build.py            # onefile：dist/VPR Audio.exe
    .venv/Scripts/python scripts/build.py --onedir   # onedir：dist/VPR Audio/（启动更快）

产物内含 Python 运行时 + 解密逻辑 + 前端；ffmpeg.exe / kgg-dec.exe 不打包，
构建后放在 exe 同目录（--onedir 会自动复制到 dist/VPR Audio/）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ONEFILE_SPEC = ROOT / "scripts" / "VPR-Audio.spec"
ONEDIR_SPEC = ROOT / "scripts" / "VPR-Audio-onedir.spec"


def main() -> int:
    onedir = "--onedir" in sys.argv[1:]
    spec = ONEDIR_SPEC if onedir else ONEFILE_SPEC
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        # CI / 非 venv 环境：直接用当前解释器（PyInstaller 仍需已安装）
        py = Path(sys.executable)
    cmd = [
        str(py),
        "-m",
        "PyInstaller",
        str(spec),
        "--noconfirm",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
    ]
    print(">", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc == 0 and onedir:
        target = ROOT / "dist" / "VPR Audio"
        for name in ("ffmpeg.exe", "kgg-dec.exe"):
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, target / name)
                print(f"已复制 {name} -> {target}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
