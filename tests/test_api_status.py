"""get_qmc_db_status 的缓存：启动/刷新反复查询同一密钥库时不必整读。"""
import os

from vpr import api as api_mod
from vpr.api import Api


def test_qmc_db_status_caches_by_mtime(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    db = tmp_path / "keys.db"
    db.write_bytes(b"x" * 64)
    calls = []

    class FakeSource:
        sqlite_path = str(db)
        mmkv_path = None

        def is_usable(self):
            return True

        def entry_count(self):
            return 3

    def fake_load(path):
        calls.append(path)
        return FakeSource()

    monkeypatch.setattr(api_mod, "_load_qmc_source", fake_load)
    api = Api()
    api.settings.qmc_keydb_path = str(db)

    first = api.get_qmc_db_status()
    second = api.get_qmc_db_status()
    assert first == second
    assert len(calls) == 1

    st = os.stat(db)
    os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    api.get_qmc_db_status()
    assert len(calls) == 2
