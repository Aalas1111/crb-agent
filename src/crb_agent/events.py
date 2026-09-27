"""流式事件契约（服务端 → 前端的唯一接口）。

这套事件是照搬「时间顺序即真相」的做法：**agent 一轮里发生的一切都往同一个
有序列表里追加**，前端拿到什么顺序就渲染什么顺序，中间不做任何重排。所以
「思考 → 调工具 → 再思考 → 最后回复」天然就是从上往下的时间线。

前端只做两件事（见 ``web/feed.js``）：

1. ``reasoning_delta`` / ``assistant_delta`` 往**当前**块上追加（流式合并）；
2. ``tool_result`` 按 ``id`` 找到对应的 ``tool_call`` 卡片**原地**补上结果。

两者都是「就地更新」，绝不新开一块——否则卡片会跳到列表末尾，时间顺序就断了。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

EventType = Literal[
    "run_start",
    "reasoning_delta",
    "assistant_delta",
    "assistant_message",
    "tool_call",
    "tool_result",
    "error",
    "done",
]

#: 工具的风险等级。只有 ``write`` 与 ``dangerous`` 会改变学校系统里的东西；
#: ``read`` 永远是安全的。前端据此给卡片上色（dangerous 打红标）。
RiskLevel = Literal["read", "dangerous"]


@dataclass
class ToolResult:
    """一次工具调用的结果。

    ``summary`` 是**给人看的一行话**（卡片标题右边那行）；``data`` 是给 LLM
    看的结构化载荷，原样塞进 tool message 里。两者都必填——只有 ``data``
    的话卡片上没话说，只有 ``summary`` 的话 LLM 拿不到细节。
    """

    status: Literal["ok", "error"]
    summary: str
    risk: RiskLevel = "read"
    data: Any = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "risk": self.risk,
            "data": self.data,
        }

    def for_model(self) -> str:
        """回灌给 LLM 的文本。**永远带 status**：失败也是一种事实。"""
        import json

        payload = {"status": self.status, "summary": self.summary}
        if self.data is not None:
            payload["data"] = self.data
        return json.dumps(payload, ensure_ascii=False, default=str)


@dataclass
class Event:
    """一条事件。``data`` 的形状由 ``type`` 决定（见 :data:`EventType`）。"""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    #: 追加时刻（epoch 秒）。前端用它算「已运行多久」，也给留痕排序用。
    at: float = 0.0

    def to_wire(self) -> dict[str, Any]:
        return {"type": self.type, **asdict(self)["data"], "at": self.at}


def run_start(run_id: str) -> Event:
    """一轮的开始。

    这条**必须**发出去（而不是只写进留痕）：前端靠它重置「当前正在流的块」,
    并在失败/中断时决定要不要加一条提示。只在留痕里有、SSE 里没有的话，
    现场与回放就会分家——实测踩过：回放时失败提示正常，现场时它压根不出现。
    """
    return Event("run_start", {"run_id": run_id})


def reasoning_delta(content: str) -> Event:
    return Event("reasoning_delta", {"content": content})


def assistant_delta(content: str) -> Event:
    return Event("assistant_delta", {"content": content})


def assistant_message(content: str) -> Event:
    return Event("assistant_message", {"content": content})


def tool_call(call_id: str, name: str, input_: dict[str, Any], risk: RiskLevel) -> Event:
    return Event("tool_call", {"id": call_id, "name": name, "input": input_, "risk": risk})


def tool_result(call_id: str, name: str, result: ToolResult, elapsed_ms: int) -> Event:
    return Event(
        "tool_result",
        {
            "id": call_id,
            "name": name,
            "elapsed_ms": elapsed_ms,
            **result.to_wire(),
        },
    )


def error(message: str) -> Event:
    return Event("error", {"message": message})


def done(run_id: str, status: Literal["completed", "failed", "cancelled"]) -> Event:
    return Event("done", {"run_id": run_id, "status": status})


def format_sse(event: Event) -> str:
    """按 SSE 规范序列化。

    事件名单独发一行 ``event:``：前端用 ``addEventListener(type, …)`` 按名分发，
    比在一堆 JSON 里 switch 干净得多（这也是 SilverTavern 的做法）。
    """
    import json

    payload = json.dumps(event.to_wire(), ensure_ascii=False, default=str)
    return f"event: {event.type}\ndata: {payload}\n\n"
