"""配置读写与酷狗密钥库查找。"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Settings:
    select_output_path: str | None = None
    select_input_path: str | None = None
    db_path: str | None = None
    output_format: str | None = None
    concurrency: int | None = None
    close_to_tray: bool | None = None
    qmc_keydb_path: str | None = None
    qmc_manual_ekeys: str | None = None


VALID_OUTPUT_FORMATS = frozenset({"mp3", "flac", "ogg"})


def _valid_format(value) -> str | None:
    return value if value in VALID_OUTPUT_FORMATS else None


def _valid_concurrency(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 6 else None


def get_tool_dir() -> Path:
    """打包后返回 exe 所在目录，开发模式返回项目根目录"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def settings_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "kugou-converter" / "settings.json"
    return get_tool_dir() / "settings.json"


def legacy_settings_path() -> Path:
    return get_tool_dir() / "settings.json"


def _legacy_settings_paths() -> list[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    base = Path(appdata)
    return [
        base / "酷狗音乐kgg转换器" / "settings.json",
        base / "kugou-kgg-converter" / "settings.json",
        base / "kugou-converter-ui" / "settings.json",
    ]


def _load_legacy_settings() -> Settings | None:
    """兼容旧版配置文件中的路径字段"""
    for path in _legacy_settings_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        migrated = Settings(
            select_output_path=data.get("selectOutputPath") or None,
            select_input_path=data.get("selectInputPath") or None,
            db_path=data.get("dbPath") or None,
        )
        if migrated.select_output_path or migrated.select_input_path or migrated.db_path:
            return migrated
    return None


def load_settings() -> Settings:
    for path in (settings_path(), legacy_settings_path()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        try:
            return Settings(
                select_output_path=data.get("select_output_path"),
                select_input_path=data.get("select_input_path"),
                db_path=data.get("db_path"),
                output_format=_valid_format(data.get("output_format")),
                concurrency=_valid_concurrency(data.get("concurrency")),
                close_to_tray=data.get("close_to_tray"),
                qmc_keydb_path=data.get("qmc_keydb_path"),
                qmc_manual_ekeys=data.get("qmc_manual_ekeys"),
            )
        except Exception:
            continue
    migrated = _load_legacy_settings()
    return migrated if migrated is not None else Settings()


def write_atomic(path: Path, contents: str) -> None:
    """先写临时文件再替换目标，避免写入中断损坏配置文件"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(contents, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_settings(settings: Settings) -> None:
    text = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
    path = settings_path()
    first_err: Exception | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, text)
        return
    except OSError as e:
        first_err = e
    write_atomic(legacy_settings_path(), text)
    _ = first_err


def is_plausible_db_file(path: Path) -> bool:
    """酷狗 KGMusicV3.db 文件头不是标准 SQLite 魔数，这里只检查文件是否存在且 >= 1KB"""
    min_db_size = 1024
    try:
        st = path.stat()
        return os.path.isfile(str(path)) and st.st_size >= min_db_size
    except OSError:
        return False


def _found(path: Path, source: str) -> dict:
    return {"found": True, "path": str(path), "source": source}


def find_db_path(settings: Settings) -> dict:
    """按优先级查找密钥库：手动路径 -> 程序目录 -> APPDATA -> LOCALAPPDATA"""
    if settings.db_path:
        p = Path(settings.db_path)
        if is_plausible_db_file(p):
            return _found(p, "custom")
    return find_default_db() or {"found": False, "path": None, "source": None}


def find_default_db() -> dict | None:
    local_db = get_tool_dir() / "KGMusicV3.db"
    if is_plausible_db_file(local_db):
        return _found(local_db, "local")

    appdata = os.environ.get("APPDATA")
    if appdata:
        db = Path(appdata) / "KuGou8" / "KGMusicV3.db"
        if is_plausible_db_file(db):
            return _found(db, "appdata")

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        db = Path(localappdata) / "KuGou8" / "KGMusicV3.db"
        if is_plausible_db_file(db):
            return _found(db, "localappdata")

    return None
