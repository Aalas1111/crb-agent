"""agent loop：思考 → 调工具 → 再思考 → 最后回复。

一轮（run）的骨架，照 ``pi`` / SilverTavern 的做法：

    ┌ 收到用户消息
    │  模型流式输出：reasoning_delta… assistant_delta…
    ├─ 模型要调工具 → tool_call → 执行 → tool_result → 回到上面再来一轮
    └─ 模型不再调工具 → assistant_message → run 结束

**关键约定：所有事件按发生顺序交给 ``emit``，中间不缓存、不重排、不合并。**
时间线是这一层的产物；前端的职责只是把它画出来。所以「思考块在哪、工具卡在哪」
不需要两边协商——顺序就是数组顺序。

工具执行是**串行**的：crb 的每一步都在改学校系统的状态（存草稿 / 提交 / 撤回），
并发跑两个写操作会让「先看懂再动手」变成「先动手再说」。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from . import events as ev
from .events import Event, ToolResult
from .llm import LLMError, StreamingLLM, assistant_message, describe_llm_error, tool_message
from .store import SessionStore
from .tools import Toolbox

#: 一轮里最多允许几轮工具调用。兜底用的：正常的借用流程 3~5 轮就结束了，
#: 超过这个数基本意味着模型在绕圈子（或某个工具一直报错它一直重试）。
MAX_TOOL_ROUNDS = 24

#: 一次响应里最多执行几个工具调用。模型偶尔会一口气要查七八间教室，
#: 真让它跑完会很慢，而且后几个通常基于前几个的结果就该改了。
MAX_CALLS_PER_ROUND = 4

#: 回灌给模型的工具结果最多这么多个字符。超了截断并**明确标注**截断——
#: 悄悄截断会让模型以为「就这么多」，于是基于残缺数据下结论。
TOOL_RESULT_LIMIT = 16_000

#: 上下文预算（按字符粗算，约 4 字符/token）。超了就丢掉最老的工具块。
CONTEXT_CHAR_BUDGET = 400_000

Emit = Callable[[Event], None]


class _Cancelled(Exception):
    """用户点了停止（或关掉了页面）。"""


class _RoundLimit(Exception):
    """工具轮数用完了——模型没能收敛。"""


class GenerateFn(Protocol):
    """把「一轮模型调用」抽象出来——测试里换成脚本化的假模型。

    ``stream()`` 收到的是最终的模型回复，中途的增量通过两个回调实时交出去。
    """

    def __call__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_reasoning: Callable[[str], None],
        on_content: Callable[[str], None],
    ) -> Any: ...


class Agent:
    def __init__(
        self,
        *,
        store: SessionStore,
        toolbox: Toolbox,
        system_prompt: str,
        generate: GenerateFn | None = None,
        llm: StreamingLLM | None = None,
    ) -> None:
        self.store = store
        self.toolbox = toolbox
        self.system_prompt = system_prompt
        self._llm = llm
        self._generate = generate

    # ---- 对外入口 ------------------------------------------------------
    def run(
        self,
        session_id: str,
        text: str,
        *,
        emit: Emit | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """跑一轮。返回 ``{"run_id", "status", "events"}``。

        ``emit`` 会被同步调用；抛异常就等于这一轮失败（SSE 那条连接断了就是这样）。
        """
        emit = emit or (lambda _event: None)
        events: list[Event] = []

        def push(event: Event) -> None:
            event.at = time.time()
            events.append(event)
            self.store.append(session_id, {"t": "event", "event": event.to_wire(), "at": event.at})
            emit(event)

        run_id = uuid.uuid4().hex
        self.store.append(session_id, {"t": "user", "content": text, "at": time.time()})
        self.store.ensure_title(session_id, text.replace("\n", " ").strip())
        self.store.append(session_id, {"t": "run_start", "run_id": run_id, "at": time.time()})
        # 留痕里的 run_start 是给读的人看的边界；这条事件是给前端用的（见它的说明）。
        push(ev.run_start(run_id))

        messages = self.build_messages(session_id)
        status = "completed"
        try:
            self._loop(session_id, messages, push, cancel)
        except _Cancelled:
            status = "cancelled"
            push(ev.error("已停止。"))
        except _RoundLimit:
            status = "failed"
            push(ev.error(f"工具调用轮数超过上限（{MAX_TOOL_ROUNDS} 轮），已停下。"))
        except LLMError as exc:
            status = "failed"
            push(ev.error(f"模型调用失败：{describe_llm_error(exc)}"))
        except Exception as exc:  # noqa: BLE001 - 任何意外都要留痕，别让 SSE 静默断掉
            status = "failed"
            push(ev.error(f"这一轮出错了：{type(exc).__name__}: {exc}"))

        self.store.append(
            session_id,
            {"t": "run_end", "run_id": run_id, "status": status, "at": time.time()},
        )
        push(ev.done(run_id, status))
        return {"run_id": run_id, "status": status, "events": events}

    # ---- 上下文 --------------------------------------------------------
    def build_messages(self, session_id: str) -> list[dict[str, Any]]:
        """把留痕投影成模型要的 messages。

        只认 ``user`` / ``assistant`` / ``tool`` 三类记录——它们就是对话本身；
        ``event`` 那些是给界面看的，不进上下文（否则等于让模型读自己的日记）。
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for record in self.store.read(session_id):
            kind = record.get("t")
            if kind == "user":
                messages.append({"role": "user", "content": record.get("content", "")})
            elif kind == "assistant":
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": record.get("content", ""),
                }
                if record.get("reasoning"):
                    message["reasoning_content"] = record["reasoning"]
                if record.get("tool_calls"):
                    message["tool_calls"] = record["tool_calls"]
                messages.append(message)
            elif kind == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": record.get("tool_call_id", ""),
                        "content": record.get("content", ""),
                    }
                )
        return messages

    # ---- 主循环 --------------------------------------------------------
    def _loop(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        push: Emit,
        cancel: threading.Event | None,
    ) -> None:
        for _round in range(MAX_TOOL_ROUNDS):
            if cancel is not None and cancel.is_set():
                raise _Cancelled
            self._trim(messages)

            response = self._call_model(messages, push, cancel)

            if response.tool_calls:
                calls = response.tool_calls[:MAX_CALLS_PER_ROUND]
                stored_calls = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments_raw or "{}"},
                    }
                    for call in calls
                ]
                # 先把 assistant 这一turn 落盘：工具的执行结果必须挂在它后面，
                # 否则下一轮拼出来的 messages 里 tool 消息找不到它的 tool_call。
                self.store.append(
                    session_id,
                    {
                        "t": "assistant",
                        "content": response.content or "",
                        "reasoning": response.reasoning,
                        "tool_calls": stored_calls,
                        "at": time.time(),
                    },
                )
                messages.append(assistant_message(response))
                # 模型可能同时给了正文和工具调用；那些正文已经在 assistant_delta
                # 里流出去了，这里只把它留在时间线上，不重复推。

                for call in calls:
                    if cancel is not None and cancel.is_set():
                        raise _Cancelled
                    push(
                        ev.tool_call(
                            call.id, call.name, call.arguments(), self.toolbox.risk_of(call.name)
                        )
                    )
                    started = time.monotonic()
                    result = self.toolbox.execute(call.name, call.arguments())
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    push(ev.tool_result(call.id, call.name, result, elapsed_ms))
                    payload = result.for_model()
                    if len(payload) > TOOL_RESULT_LIMIT:
                        payload = (
                            payload[:TOOL_RESULT_LIMIT]
                            + f"…（已截断，完整长度 {len(payload)} 字符）"
                        )
                    self.store.append(
                        session_id,
                        {
                            "t": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": payload,
                            "at": time.time(),
                        },
                    )
                    messages.append(tool_message(call.id, payload))
                continue

            content = response.content or ""
            self.store.append(
                session_id,
                {
                    "t": "assistant",
                    "content": content,
                    "reasoning": response.reasoning,
                    "at": time.time(),
                },
            )
            push(ev.assistant_message(content))
            return

        raise _RoundLimit

    def _call_model(
        self,
        messages: list[dict[str, Any]],
        push: Emit,
        cancel: threading.Event | None,
    ) -> Any:
        def on_reasoning(delta: str) -> None:
            if cancel is not None and cancel.is_set():
                raise _Cancelled
            push(ev.reasoning_delta(delta))

        def on_content(delta: str) -> None:
            if cancel is not None and cancel.is_set():
                raise _Cancelled
            push(ev.assistant_delta(delta))

        if self._generate is not None:
            return self._generate(messages, self.toolbox.manifest(), on_reasoning, on_content)
        assert self._llm is not None, "要么给 llm，要么给 generate"
        return self._llm.chat_stream(
            messages,
            tools=self.toolbox.manifest(),
            on_reasoning=on_reasoning,
            on_content=on_content,
        )

    def _trim(self, messages: list[dict[str, Any]]) -> None:
        """超预算就丢掉**最老的**工具块。

        丢掉的是一个「assistant(带 tool_calls) + 紧随其后的 tool 消息」整块——
        不能只丢一半：OpenAI 的消息格式要求 tool 消息必须能对上一条带 tool_calls
        的 assistant，缺口会被直接拒掉（400）。最近几轮永远保留，那是当前任务的
        依据。
        """
        system = messages[0]
        budget = CONTEXT_CHAR_BUDGET - len(str(system.get("content", "")))
        while True:
            total = sum(len(str(m.get("content", ""))) for m in messages[1:])
            if total <= budget or len(messages) <= 2:
                return
            block_start = -1
            for index in range(1, len(messages)):
                if messages[index].get("role") == "assistant" and messages[index].get("tool_calls"):
                    # 连同它前面那条 user 一起丢，否则会留下连续的 user 消息
                    # （有些兼容端点会因此报角色必须交替）。
                    block_start = index - 1 if messages[index - 1].get("role") == "user" else index
                    break
            if block_start < 0:
                return
            block_end = block_start + 1
            while block_end < len(messages) and messages[block_end].get("role") == "tool":
                block_end += 1
            del messages[block_start:block_end]


# ---------------------------------------------------------------- 便捷函数
def read_feed(store: SessionStore, session_id: str) -> list[dict[str, Any]]:
    """把留痕投影成界面要的 feed（用户消息 + 事件，按时间顺序）。

    和实时流是**同一批事件**，所以前端只有一条渲染路径。``run_start`` /
    ``run_end`` 一并带上：前端靠它们把每一轮的消息分开。
    """
    feed: list[dict[str, Any]] = []
    for record in store.read(session_id):
        kind = record.get("t")
        if kind == "user":
            feed.append(
                {"role": "user", "content": record.get("content", ""), "at": record.get("at", 0)}
            )
        elif kind == "run_start":
            feed.append(
                {"role": "run_start", "run_id": record.get("run_id", ""), "at": record.get("at", 0)}
            )
        elif kind == "event":
            feed.append(
                {"role": "event", "event": record.get("event", {}), "at": record.get("at", 0)}
            )
        elif kind == "run_end":
            feed.append(
                {
                    "role": "run_end",
                    "run_id": record.get("run_id", ""),
                    "status": record.get("status", ""),
                    "at": record.get("at", 0),
                }
            )
    return feed


def dumps_event(event: Event) -> str:
    return json.dumps(event.to_wire(), ensure_ascii=False, default=str)


__all__ = ["Agent", "ToolResult", "read_feed", "MAX_TOOL_ROUNDS"]
