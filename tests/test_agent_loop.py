"""agent loop：事件顺序、工具配对、失败不炸、上下文重建。

这里最要紧的一条断言在 :func:`test_replayed_events_equal_live_events` 里 ——
**实时收到的事件序列，必须和留痕里读回来的完全一致**。前端对两者走同一段
渲染代码，一旦不等，「历史会话长什么样」和「现场长什么样」就会分家，
而这种不一致只在翻旧账时才暴露。
"""

from __future__ import annotations

import json

from crb_agent.agent import MAX_TOOL_ROUNDS, Agent, read_feed
from crb_agent.llm import LLMResponse, ToolCall
from crb_agent.store import SessionStore
from crb_agent.tools import Tool, Toolbox


class ScriptedGenerate:
    """按剧本回放模型回复。

    每一步是 ``{"reasoning": [...], "content": [...], "tools": [...]}``：
    列表里每个元素都会当成一个**流式片段**交给回调——这样测试才真的走
    「边收边发」那条路径，而不是一步到位。
    """

    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)
        self.requests: list[list[dict]] = []

    def __call__(self, messages, tools, on_reasoning, on_content) -> LLMResponse:
        self.requests.append([dict(m) for m in messages])
        step = self.steps.pop(0)
        for piece in step.get("reasoning", []):
            on_reasoning(piece)
        for piece in step.get("content", []):
            on_content(piece)
        calls = []
        for spec in step.get("tools", []):
            # ``raw`` 用来模拟「模型吐了一段不是 JSON 的参数」。
            raw = spec.get("raw") or json.dumps(spec.get("input", {}))
            calls.append(ToolCall(id=spec["id"], name=spec["name"], arguments_raw=raw))
        return LLMResponse(
            content="".join(step.get("content", [])),
            reasoning="".join(step.get("reasoning", [])),
            tool_calls=calls,
        )


def _toolbox(settings) -> Toolbox:
    box = Toolbox(settings)

    def echo(payload):
        from crb_agent.events import ToolResult

        return ToolResult(status="ok", summary=f"收到 {payload.get('value')}", data=payload)

    def boom(_payload):
        raise RuntimeError("工具炸了")

    box.register(
        Tool(
            name="probe_echo",
            description="回显",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            risk="read",
            handler=echo,
        )
    )
    box.register(
        Tool(
            name="probe_boom",
            description="故意失败",
            parameters={"type": "object", "properties": {}, "required": []},
            risk="read",
            handler=boom,
        )
    )
    return box


def _agent(settings, steps) -> tuple[Agent, SessionStore, ScriptedGenerate]:
    store = SessionStore(settings.workspace)
    model = ScriptedGenerate(steps)
    agent = Agent(
        store=store,
        toolbox=_toolbox(settings),
        system_prompt="你是测试用的 agent。",
        generate=model,
    )
    return agent, store, model


def test_run_start_goes_on_the_wire_not_only_into_the_log(settings):
    """一轮的开始必须同时**发出去**。

    只写进留痕的话，前端在现场就收不到它——于是「失败/中断的提示」在回放里有、
    现场没有（实测踩过）。这条断言就是那一刻的护栏。
    """
    agent, store, _ = _agent(settings, [{"content": ["好"]}])
    session_id = store.create()
    emitted: list[str] = []
    agent.run(session_id, "嗨", emit=lambda e: emitted.append(e.type))
    assert emitted[0] == "run_start"

    replayed = [r["event"]["type"] for r in read_feed(store, session_id) if r["role"] == "event"]
    assert replayed[0] == "run_start"


def test_reasoning_tool_reply_are_emitted_in_time_order(settings):
    """核心断言：思考 → 工具 → 再思考 → 回复，事件就是按这个顺序出来的。"""
    agent, store, _ = _agent(
        settings,
        [
            {
                "reasoning": ["我需要", "先查一下。"],
                "tools": [{"id": "c1", "name": "probe_echo", "input": {"value": "hello"}}],
            },
            {
                "reasoning": ["拿到了。"],
                "content": ["好", "了。"],
            },
        ],
    )
    session_id = store.create()
    emitted: list[str] = []
    result = agent.run(session_id, "查一下", emit=lambda e: emitted.append(e.type))

    assert result["status"] == "completed"
    assert emitted == [
        "run_start",
        "reasoning_delta",  # 我需要
        "reasoning_delta",  # 先查一下。
        "tool_call",
        "tool_result",
        "reasoning_delta",  # 拿到了。
        "assistant_delta",  # 好
        "assistant_delta",  # 了。
        "assistant_message",
        "done",
    ]


def test_tool_result_pairs_with_its_call(settings):
    agent, store, _ = _agent(
        settings,
        [
            {
                "tools": [
                    {"id": "c1", "name": "probe_echo", "input": {"value": "one"}},
                    {"id": "c2", "name": "probe_echo", "input": {"value": "two"}},
                ]
            },
            {"content": ["完成"]},
        ],
    )
    session_id = store.create()
    seen: list[tuple[str, str]] = []
    agent.run(
        session_id,
        "跑两个",
        emit=lambda e: seen.append((e.type, e.data.get("id", ""))),
    )

    tools_seen = [entry for entry in seen if entry[0].startswith("tool_")]
    assert tools_seen[0] == ("tool_call", "c1")
    assert tools_seen[1] == ("tool_result", "c1")
    assert tools_seen[2] == ("tool_call", "c2")
    assert tools_seen[3] == ("tool_result", "c2")
    # 结果确实对上了各自的参数
    results = [
        e for e in read_feed(store, session_id) if e.get("event", {}).get("type") == "tool_result"
    ]
    assert results[0]["event"]["summary"] == "收到 one"
    assert results[1]["event"]["summary"] == "收到 two"


def test_failing_tool_is_reported_and_run_continues(settings):
    """工具炸了不等于这一轮炸了——把失败当成事实告诉模型，让它自己决定下一步。"""
    agent, store, _ = _agent(
        settings,
        [
            {"tools": [{"id": "c1", "name": "probe_boom", "input": {}}]},
            {"content": ["它挂了，我换个办法。"]},
        ],
    )
    session_id = store.create()
    events = []
    result = agent.run(session_id, "试试会不会炸", emit=events.append)

    assert result["status"] == "completed"
    failures = [e for e in events if e.type == "tool_result"]
    assert len(failures) == 1
    assert failures[0].data["status"] == "error"
    assert "工具炸了" in failures[0].data["summary"]
    # 模型拿到的也是失败事实，而不是空
    tool_messages = [m for m in read_feed(store, session_id)]
    assert any(e.get("event", {}).get("type") == "assistant_message" for e in tool_messages)


def test_unknown_tool_is_rejected_without_crashing(settings):
    agent, store, _ = _agent(
        settings,
        [
            {"tools": [{"id": "c1", "name": "nonexistent_tool", "input": {}}]},
            {"content": ["没有这个工具。"]},
        ],
    )
    session_id = store.create()
    events = []
    agent.run(session_id, "用不存在的工具", emit=events.append)
    failure = next(e for e in events if e.type == "tool_result")
    assert failure.data["status"] == "error"
    assert "没有这个工具" in failure.data["summary"]


def test_run_stops_at_round_limit(settings):
    """模型一直绕圈子时要有硬上限，不然会一直烧 token。"""
    steps = [
        {"tools": [{"id": f"c{i}", "name": "probe_echo", "input": {"value": "x"}}]}
        for i in range(MAX_TOOL_ROUNDS)
    ]
    agent, store, _ = _agent(settings, steps)
    session_id = store.create()
    events = []
    result = agent.run(session_id, "绕圈", emit=events.append)

    assert result["status"] == "failed"
    errors = [e for e in events if e.type == "error"]
    assert errors and "轮数" in errors[-1].data["message"]


def test_replayed_events_equal_live_events(settings):
    """留痕读回来的事件序列 == 实时发出的那条序列（前端两条路共用一段渲染）。"""
    agent, store, _ = _agent(
        settings,
        [
            {
                "reasoning": ["想一下"],
                "tools": [{"id": "c1", "name": "probe_echo", "input": {"value": "v"}}],
            },
            {"reasoning": ["嗯"], "content": ["结论"]},
        ],
    )
    session_id = store.create()
    live = []
    agent.run(session_id, "问题", emit=live.append)

    replayed = [
        record["event"] for record in read_feed(store, session_id) if record["role"] == "event"
    ]
    assert replayed == [event.to_wire() for event in live]


def test_feed_keeps_user_and_run_boundaries(settings):
    agent, store, _ = _agent(settings, [{"content": ["一"]}])
    session_id = store.create()
    agent.run(session_id, "第一句话", emit=lambda _e: None)
    agent.run(session_id, "第二句话", emit=lambda _e: None)

    roles = [record["role"] for record in read_feed(store, session_id)]
    assert roles.count("user") == 2
    assert roles.count("run_start") == 2
    assert roles.count("run_end") == 2
    assert roles[0] == "user"


def test_model_history_has_correct_message_roles(settings):
    """第二轮请求里应该能看到：user → assistant(带 tool_calls) → tool → assistant。"""
    agent, store, model = _agent(
        settings,
        [
            {"tools": [{"id": "c1", "name": "probe_echo", "input": {"value": "v"}}]},
            {"content": ["好了"]},
        ],
    )
    session_id = store.create()
    agent.run(session_id, "帮我查", emit=lambda _e: None)

    # 重建上下文（模拟「同一个人在同一个会话里再问一句」）
    messages = agent.build_messages(session_id)
    roles = [m["role"] for m in messages]
    assert roles[0] == "system"
    assert roles[1:] == ["user", "assistant", "tool", "assistant"]

    assistant_with_tools = messages[2]
    assert assistant_with_tools["tool_calls"][0]["function"]["name"] == "probe_echo"
    tool_message = messages[3]
    assert tool_message["tool_call_id"] == "c1"
    assert json.loads(tool_message["content"])["status"] == "ok"


def test_title_comes_from_first_user_message(settings):
    agent, store, _ = _agent(settings, [{"content": ["好"]}])
    session_id = store.create()
    agent.run(session_id, "帮我看看这周的教室借用申请", emit=lambda _e: None)
    listed = store.list()
    assert listed[0]["title"] == "帮我看看这周的教室借用申请"


def test_tool_arguments_that_are_not_json_tell_the_model(settings):
    """模型给出坏 JSON 时，把它翻译成一句人话回灌，而不是静默当成空参数。

    真实路径：``ToolCall.arguments()`` 把坏 JSON 变成 ``__parse_error__``，
    ``Toolbox.execute`` 认这个键并回一句人话，模型据此自己改。
    """
    agent, store, _ = _agent(
        settings,
        [
            {
                "tools": [
                    {
                        "id": "c1",
                        "name": "probe_echo",
                        "raw": '{"value": "缺个引号}',  # 不是合法 JSON
                    }
                ]
            },
            {"content": ["我重新给一次参数。"]},
        ],
    )
    session_id = store.create()
    events = []
    result = agent.run(session_id, "随便", emit=events.append)

    assert result["status"] == "completed"
    failure = next(e for e in events if e.type == "tool_result")
    assert failure.data["status"] == "error"
    assert "不是合法 JSON" in failure.data["summary"]

    # 回灌给模型的那条 tool message 也要带上这句人话
    tool_messages = [m for m in agent.build_messages(session_id) if m["role"] == "tool"]
    assert "不是合法 JSON" in tool_messages[-1]["content"]
