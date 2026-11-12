"""QQ 音乐离线密钥源管理（SQLite / MMKV / 手动文本）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import mmkv

SQLITE_TABLE_CANDIDATES: list[tuple[str, str, str]] = [
    ("audio_file_ekey_table", "file_path", "ekey"),
    ("EKeyFileInfo", "filePath", "eKey"),
    ("p2p_cache_info_table", "file_id", "ekey"),
]


def basename_of(raw: str) -> str:
    return raw.rstrip().replace("\\", "/").rsplit("/", 1)[-1].strip()


def stem_of(name: str) -> str:
    i = name.rfind(".")
    return name[:i] if i > 0 else name


class QmcKeySource:
    def __init__(self) -> None:
        self.by_name: dict[str, str] = {}
        self.manual_pool: list[str] = []
        self.sqlite_path: str | None = None
        self.mmkv_path: str | None = None

    @classmethod
    def empty(cls) -> "QmcKeySource":
        return cls()

    @classmethod
    def load_sqlite(cls, path) -> "QmcKeySource":
        """只读打开 SQLite 密钥库并读取已知密钥表。"""
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"无法打开密钥库（只读）: 文件不存在 {p}")
        # 使用 as_uri() 编码路径里的 '#' 和 '%'，避免 sqlite URI 解析成 fragment
        uri = f"{p.resolve().as_uri()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as e:
            raise ValueError(f"无法打开密钥库（只读）: {e}") from e
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cur.fetchall()]
        except sqlite3.Error as e:
            raise ValueError(f"密钥库结构读取失败: {e}") from e
        finally:
            conn.close()

        source = cls.empty()
        source.sqlite_path = str(p)
        matched_any = False
        try:
            conn = sqlite3.connect(uri, uri=True)
            for table, path_col, ekey_col in SQLITE_TABLE_CANDIDATES:
                if not any(t.lower() == table.lower() for t in tables):
                    continue
                try:
                    cur = conn.execute(f'SELECT "{path_col}", "{ekey_col}" FROM "{table}"')
                except sqlite3.Error:
                    continue
                for raw_name, ekey in cur.fetchall():
                    ekey = (ekey or "").strip() if isinstance(ekey, str) else ""
                    if not ekey:
                        continue
                    name = basename_of(str(raw_name or ""))
                    if not name:
                        continue
                    source.by_name[name.lower()] = ekey
                    matched_any = True
        finally:
            conn.close()
        if not matched_any:
            raise ValueError("密钥库中未发现已知的 QQ 音乐密钥表")
        return source

    @classmethod
    def load_mmkv(cls, path) -> "QmcKeySource":
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as e:
            raise ValueError(f"读取 MMKV 密钥库失败: {e}") from e
        try:
            pairs = mmkv.parse(data)
        except ValueError as e:
            raise ValueError(f"MMKV 解析失败: {e}") from e
        if not pairs:
            raise ValueError(
                "MMKV 文件中未发现可用的 QQ 音乐密钥（可能是加密型 vault，"
                "请先用官方脚本解出明文再导入）"
            )
        source = cls.empty()
        source.mmkv_path = str(p)
        source.merge_pairs(pairs)
        return source

    def set_manual_text(self, text: str) -> None:
        """解析手动粘贴文本（支持 JSON 对象、`名称=ekey` 行或纯 ekey 候选）。"""
        self.manual_pool.clear()
        trimmed = text.strip()
        if trimmed.startswith("{"):
            try:
                obj = json.loads(trimmed)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                for name, value in obj.items():
                    if not isinstance(value, str):
                        continue
                    if mmkv.looks_like_ekey(value):
                        self.by_name[basename_of(name).lower()] = value
                return
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # 先判纯 ekey，避免把 base64 末尾的 '=' 误当分隔符
            if mmkv.looks_like_ekey(line):
                self.manual_pool.append(line)
                continue
            sep_indices = [i for i in (line.find("\t"), line.find("="), line.rfind(",")) if i > 0]
            if not sep_indices:
                continue
            i = min(sep_indices)
            name, ekey = line[:i].strip(), line[i + 1 :].strip()
            if not name or not mmkv.looks_like_ekey(ekey):
                continue
            self.by_name[basename_of(name).lower()] = ekey

    def merge_pairs(self, pairs) -> None:
        for name, ekey in pairs:
            name = basename_of(name).lower()
            if not name or not ekey:
                continue
            self.by_name[name] = ekey

    def lookup(self, names) -> str | None:
        """按文件名或去扩展名主干查找 ekey。"""
        for name in names:
            base = basename_of(name)
            for candidate in (base, stem_of(base)):
                ekey = self.by_name.get(candidate.lower())
                if ekey is not None:
                    return ekey
        return None

    def entry_count(self) -> int:
        return len(self.by_name)

    def is_usable(self) -> bool:
        return bool(self.by_name) or bool(self.manual_pool)
