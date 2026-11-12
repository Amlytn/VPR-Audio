"""任务记录持久化，每批转换以 JSON 行追加到 history.jsonl，保留最近 HISTORY_KEEP 条。"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .settings import write_atomic

HISTORY_KEEP = 200
_lock = threading.Lock()


@dataclass
class HistoryFile:
    """批次内单个文件的转换记录"""

    name: str
    size: int
    error: str | None


@dataclass
class HistoryEntry:
    time_ms: int
    total: int
    success: int
    failed: int
    cancelled: int
    duration_secs: int
    output_dir: str
    format: str
    concurrency: int
    files: list[HistoryFile] = field(default_factory=list)


def history_path() -> Path:
    """历史记录文件路径，与 settings.json 同目录"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "kugou-converter" / "history.jsonl"
    from .settings import get_tool_dir

    return get_tool_dir() / "history.jsonl"


def append(entry: HistoryEntry) -> None:
    """追加一条记录，超出上限时原子重写为最近 HISTORY_KEEP 条"""
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(entry), ensure_ascii=False)

    with _lock:
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""
        lines = [l for l in existing.splitlines() if l.strip()]
        lines.append(line)

        if len(lines) > HISTORY_KEEP:
            kept = lines[-HISTORY_KEEP:]
            write_atomic(path, "\n".join(kept) + "\n")
        else:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


def load() -> list[dict]:
    """按从旧到新顺序读取历史记录，跳过损坏行"""
    try:
        text = history_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def clear() -> None:
    path = history_path()
    with _lock:
        if not path.exists():
            return
        try:
            write_atomic(path, "")
        except OSError:
            pass
