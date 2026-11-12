"""版本号单一来源：包内 __version__、pyproject 元数据、IPC 返回值必须一致。"""
import tomllib
from pathlib import Path

import vpr
from vpr.api import Api

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_version_matches_package():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["version"] == vpr.__version__


def test_ipc_reports_package_version(tmp_path, monkeypatch):
    # Api 构造会读设置，隔离掉真实 APPDATA
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert Api().get_app_version() == vpr.__version__
