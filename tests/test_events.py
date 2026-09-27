"""事件契约：SSE 序列化、工具结果的两副面孔（给人看 / 给模型看）。"""

from __future__ import annotations

import json

from crb_agent import events as ev


def test_format_sse_uses_named_events():
    """事件名单独一行——前端用 addEventListener 按名分发。"""
    text = ev.format_sse(ev.reasoning_delta("想"))
    assert text.startswith("event: reasoning_delta\n")
    assert text.endswith("\n\n")
    payload = json.loads(text.split("data: ", 1)[1])
    assert payload["type"] == "reasoning_delta"
    assert payload["content"] == "想"


def test_every_event_type_round_trips_through_wire():
    produced = [
        ev.reasoning_delta("r"),
        ev.assistant_delta("a"),
        ev.assistant_message("全"),
        ev.tool_call("c1", "crb_plan", {"mode": "preview"}, "dangerous"),
        ev.tool_result("c1", "crb_plan", ev.ToolResult("ok", "好了", "dangerous", {"n": 1}), 1234),
        ev.error("炸了"),
        ev.done("run1", "completed"),
    ]
    for event in produced:
        wire = json.loads(ev.format_sse(event).split("data: ", 1)[1])
        assert wire["type"] == event.type
        assert "at" in wire
    # 工具结果里既有给人看的一行话，也有给模型看的结构化载荷
    result_wire = json.loads(ev.format_sse(produced[4]).split("data: ", 1)[1])
    assert result_wire["summary"] == "好了"
    assert result_wire["elapsed_ms"] == 1234
    assert result_wire["data"] == {"n": 1}


def test_tool_result_for_model_always_carries_status():
    """失败也是一种事实——回灌给模型的文本里必须有 status。"""
    ok = json.loads(ev.ToolResult("ok", "成功").for_model())
    assert ok["status"] == "ok"
    failed = json.loads(ev.ToolResult("error", "没查着").for_model())
    assert failed["status"] == "error"
    assert failed["summary"] == "没查着"


def test_tool_result_for_model_handles_unserializable_payload():
    """工具塞了奇怪对象进来也不该让这一轮崩掉。"""

    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    payload = ev.ToolResult("ok", "带了个奇怪对象", data={"x": Weird()}).for_model()
    assert "weird" in payload


def test_risk_levels_are_only_read_or_dangerous():
    """风险等级就两档：只读、会改东西。别再加中间档去糊弄颜色。"""
    assert set(ev.RiskLevel.__args__) == {"read", "dangerous"}
