"""会话留痕。

一个会话 = ``<workspace>/sessions/<id>.jsonl`` 一行一条记录，只追加：

    {"t": "meta",      "id": …, "title": …, "created_at": …}
    {"t": "user",      "content": …, "at": …}
    {"t": "run_start", "run_id": …, "at": …}
    {"t": "event",     "event": {…},  "at": …}      ← 与 SSE 收到的**同一条**事件
    {"t": "run_end",   "run_id": …, "status": …, "at": …}

刻意让「事件」这一行就是前端收到的那条 SSE 载荷：于是**回放和实时是同一条
代码路径**（前端都走 ``appendRunEvent``）。两边一旦分家，历史会话和现场会话
迟早会长得不一样，而这种不一致只在「翻旧账」时才暴露。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: 侧栏里标题最长显示这么多个字。再长就截断——只是给列表看的，不是真相。
TITLE_LIMIT = 40


def _now() -> float:
    return time.time()


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---- 路径 ----------------------------------------------------------
    def path(self, session_id: str) -> Path:
        # 会话 id 是我们自己生成的（uuid4 hex），但仍要挡住 ``../``：
        # 这是**唯一**由 URL 直接拼出来的文件路径。
        if not session_id or not all(c in "0123456789abcdef" for c in session_id):
            raise ValueError(f"不合法的会话 id：{session_id!r}")
        return self.root / f"{session_id}.jsonl"

    # ---- 写 ------------------------------------------------------------
    def create(self, title: str = "") -> str:
        session_id = uuid.uuid4().hex
        self._append_raw(
            session_id,
            {"t": "meta", "id": session_id, "title": title, "created_at": _now()},
        )
        return session_id

    def append(self, session_id: str, record: dict[str, Any]) -> None:
        self._append_raw(session_id, record)

    def rename(self, session_id: str, title: str) -> None:
        """重写整个文件换掉标题。

        会话文件是一行一条、只追加的，唯独标题要改——所以这里读全部、
        改第一行、原子替换。会话文件很小（最多几 MB），不值得为它设计
        一套「标题也追加」的机制。
        """
        path = self.path(session_id)
        with self._lock:
            records = _read_records(path)
            if not records:
                return
            records[0]["title"] = title
            _write_all(path, records)

    def ensure_title(self, session_id: str, title: str) -> None:
        """只在标题还空着时写入（用第一条用户消息当标题）。"""
        records = _read_records(self.path(session_id))
        if records and not records[0].get("title"):
            self.rename(session_id, title[:TITLE_LIMIT])

    def delete(self, session_id: str) -> None:
        path = self.path(session_id)
        with self._lock:
            path.unlink(missing_ok=True)

    def _append_raw(self, session_id: str, record: dict[str, Any]) -> None:
        path = self.path(session_id)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    # ---- 读 ------------------------------------------------------------
    def read(self, session_id: str) -> list[dict[str, Any]]:
        return _read_records(self.path(session_id))

    def list(self) -> list[dict[str, Any]]:
        """侧栏用的会话列表，最近更新的在前。"""
        out: list[dict[str, Any]] = []
        for path in self.root.glob("*.jsonl"):
            records = _read_records(path)
            if not records:
                continue
            meta = records[0]
            last_at = next(
                (r.get("at") or 0 for r in reversed(records) if r.get("at")),
                meta.get("created_at") or 0,
            )
            out.append(
                {
                    "id": meta.get("id") or path.stem,
                    "title": meta.get("title") or "（未命名会话）",
                    "created_at": meta.get("created_at") or 0,
                    "updated_at": last_at,
                    "turns": sum(1 for r in records if r.get("t") == "user"),
                }
            )
        out.sort(key=lambda item: item["updated_at"], reverse=True)
        return out

    def exists(self, session_id: str) -> bool:
        return self.path(session_id).is_file()


# ---------------------------------------------------------------- 内部
def _read_records(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            # 半行（进程在写的时候被杀）不该让整个会话读不出来。
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _write_all(path: Path, records: Iterable[dict[str, Any]]) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    body = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records)
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
