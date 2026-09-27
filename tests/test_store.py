"""会话留痕：只追加、能容下半行、挡住路径穿越。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crb_agent.store import SessionStore


def test_create_list_read(tmp_path: Path):
    store = SessionStore(tmp_path)
    session_id = store.create()
    store.append(session_id, {"t": "user", "content": "你好", "at": 1.0})
    store.append(session_id, {"t": "run_start", "run_id": "r1", "at": 2.0})

    records = store.read(session_id)
    assert [r["t"] for r in records] == ["meta", "user", "run_start"]
    assert store.exists(session_id)

    listed = store.list()
    assert len(listed) == 1
    assert listed[0]["id"] == session_id
    assert listed[0]["turns"] == 1
    assert listed[0]["updated_at"] == 2.0


def test_list_is_newest_first(tmp_path: Path):
    store = SessionStore(tmp_path)
    first = store.create()
    store.append(first, {"t": "user", "content": "旧", "at": 10.0})
    second = store.create()
    store.append(second, {"t": "user", "content": "新", "at": 20.0})
    assert [item["id"] for item in store.list()] == [second, first]


def test_session_id_must_be_hex(tmp_path: Path):
    """会话 id 是唯一从 URL 直接拼出来的文件名，必须挡住 ``../``。"""
    store = SessionStore(tmp_path)
    for bad in ("../evil", "..%2fevil", "abc/def", "", "with space", "大写不行"):
        with pytest.raises(ValueError):
            store.path(bad)


def test_rename_rewrites_only_the_meta_line(tmp_path: Path):
    store = SessionStore(tmp_path)
    session_id = store.create()
    store.append(session_id, {"t": "user", "content": "第一句", "at": 1.0})
    store.rename(session_id, "新标题")

    records = store.read(session_id)
    assert records[0]["title"] == "新标题"
    assert records[1]["content"] == "第一句"
    assert (
        json.loads(store.path(session_id).read_text(encoding="utf-8").splitlines()[0])["title"]
        == "新标题"
    )


def test_ensure_title_only_fills_when_empty(tmp_path: Path):
    store = SessionStore(tmp_path)
    session_id = store.create()
    store.ensure_title(session_id, "第一次")
    store.ensure_title(session_id, "第二次")
    assert store.read(session_id)[0]["title"] == "第一次"


def test_half_written_line_does_not_break_the_session(tmp_path: Path):
    """进程在写的时候被杀会留下半行 JSON——整个会话不该因此读不出来。"""
    store = SessionStore(tmp_path)
    session_id = store.create()
    store.append(session_id, {"t": "user", "content": "完整的一行", "at": 1.0})
    with store.path(session_id).open("a", encoding="utf-8") as handle:
        handle.write('{"t": "user", "content": "写到一半就死')  # 没有换行、JSON 也不完整

    records = store.read(session_id)
    assert len(records) == 2
    assert records[1]["content"] == "完整的一行"


def test_delete_removes_the_file(tmp_path: Path):
    store = SessionStore(tmp_path)
    session_id = store.create()
    store.delete(session_id)
    assert not store.exists(session_id)
    assert store.list() == []


def test_empty_store_lists_nothing(tmp_path: Path):
    assert SessionStore(tmp_path).list() == []


def test_title_is_truncated_for_the_sidebar(tmp_path: Path):
    store = SessionStore(tmp_path)
    session_id = store.create()
    store.ensure_title(session_id, "很长的标题" * 20)
    assert len(store.read(session_id)[0]["title"]) <= 40


def test_unnamed_session_shows_placeholder(tmp_path: Path):
    store = SessionStore(tmp_path)
    store.create()
    assert store.list()[0]["title"] == "（未命名会话）"
