"""流式 LLM 客户端：OpenAI 兼容的 ``/chat/completions`` + function calling。

与 ``yqa`` 的 ``llm.py`` 最大的区别是**这里必须流式**——用户盯着屏幕看思考
过程，「等 40 秒然后一次全出来」和「一边想一边出来」是两个产品。所以这个
客户端只做流式，不做非流式（少一条没人走的代码路径）。

重试的规矩（拿事故换来的）：

* **只重试网络错误与 5xx**；400/401/422 直接抛——那是我们写错了，重试三次
  只会把同一个错误重复三遍，还多花 3 倍等待。
* **已经吐出去字符之后不再重试。** 重试会把同一段思考再流一遍，前端就会看到
  重复的句子。这时候错误是事实，报给用户比悄悄重来诚实。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

ReasoningCallback = Callable[[str], None]
ContentCallback = Callable[[str], None]


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments_raw: str

    def arguments(self) -> dict[str, Any]:
        """解析参数。LLM 偶尔给出坏 JSON——**不要**替它猜，原样告诉它。"""
        text = (self.arguments_raw or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            return {"__parse_error__": f"参数不是合法 JSON：{exc}", "__raw__": text[:500]}
        return parsed if isinstance(parsed, dict) else {"__parse_error__": "参数必须是 JSON 对象"}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "in": self.prompt_tokens,
            "out": self.completion_tokens,
            "total": self.total_tokens,
            "cached": self.cached_tokens,
        }


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""


class StreamingLLM:
    """带流式回调的客户端。线程安全？不——一个实例给一个会话用，别跨线程共享。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 300.0,
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> None:
        if not api_key:
            raise LLMError("缺少 LLM API key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> StreamingLLM:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 主入口 -----------------------------------------------------------
    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_reasoning: ReasoningCallback | None = None,
        on_content: ContentCallback | None = None,
    ) -> LLMResponse:
        """发一轮请求，边收边回调，最后把**完整**的回复返回。

        ``on_reasoning`` / ``on_content`` 只负责把增量交给调用方去转发（发 SSE），
        累积、拼接工具调用参数这些事在这里做完——调用方拿到的
        :class:`LLMResponse` 永远是完整的。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            emitted = False

            def note_reasoning(delta: str) -> None:
                nonlocal emitted
                emitted = True
                if on_reasoning:
                    on_reasoning(delta)

            def note_content(delta: str) -> None:
                nonlocal emitted
                emitted = True
                if on_content:
                    on_content(delta)

            try:
                return self._stream_once(payload, note_reasoning, note_content)
            except LLMError as exc:
                if not _is_retryable(exc) or emitted:
                    raise
                last_error = exc
                self._backoff(attempt)
            except httpx.HTTPError as exc:
                if emitted:
                    raise LLMError(f"流式响应中断（{type(exc).__name__}）：{exc}") from exc
                last_error = LLMError(f"网络错误（{type(exc).__name__}）：{exc}")
                self._backoff(attempt)

        raise last_error or LLMError("LLM 调用失败")

    # -- 单次流式请求 -----------------------------------------------------
    def _stream_once(
        self,
        payload: dict[str, Any],
        on_reasoning: ReasoningCallback,
        on_content: ContentCallback,
    ) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # 工具调用是**按 index 分片**到达的：先来 id/name，再来一段段 arguments。
        # 用 dict 按 index 归并，最后统一解析。
        pending: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason = ""

        with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as resp:
            if resp.status_code >= 500:
                resp.read()
                raise LLMError(f"服务端 {resp.status_code}：{resp.text[:200]}")
            if resp.status_code >= 400:
                resp.read()
                raise LLMError(
                    f"请求被拒 {resp.status_code}：{resp.text[:500]}", status=resp.status_code
                )

            for chunk in resp.iter_lines():
                line = chunk.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    parsed = json.loads(data)
                except ValueError:
                    continue

                if isinstance(parsed.get("usage"), dict):
                    usage = _read_usage(parsed["usage"])
                choices = parsed.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta") or {}

                piece = _read_reasoning(delta)
                if piece:
                    reasoning_parts.append(piece)
                    on_reasoning(piece)
                text = delta.get("content")
                if isinstance(text, str) and text:
                    content_parts.append(text)
                    on_content(text)
                for piece_call in delta.get("tool_calls") or []:
                    _merge_tool_call(pending, piece_call)

        calls = [
            ToolCall(
                id=entry.get("id") or f"call_{index}",
                name=entry.get("name", ""),
                arguments_raw=entry.get("arguments", ""),
            )
            for index, entry in sorted(pending.items())
            if entry.get("name")
        ]
        return LLMResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=calls,
            usage=usage,
            finish_reason=finish_reason,
        )

    def _backoff(self, attempt: int) -> None:
        time.sleep(min(2.0 * attempt, 8.0))


# ---------------------------------------------------------------- 内部工具
def _read_reasoning(delta: dict[str, Any]) -> str:
    """不同厂商给思考过程起了不同的字段名，都认。"""
    for key in ("reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _merge_tool_call(pending: dict[int, dict[str, str]], piece: dict[str, Any]) -> None:
    index = piece.get("index")
    index = index if isinstance(index, int) else 0
    entry = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if piece.get("id"):
        entry["id"] = str(piece["id"])
    function = piece.get("function") or {}
    if function.get("name"):
        entry["name"] = str(function["name"])
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        entry["arguments"] += arguments


def _read_usage(raw: dict[str, Any]) -> Usage:
    details = raw.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        total_tokens=int(raw.get("total_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or 0),
    )


def _is_retryable(exc: LLMError) -> bool:
    """只有「对方的问题」值得重试；4xx（除了 429）都是我们的问题。"""
    return exc.status == 0 or exc.status >= 500 or exc.status == 429


def assistant_message(response: LLMResponse) -> dict[str, Any]:
    """把 LLM 的回复转成能塞回 messages 的 assistant 条目。

    ``reasoning_content`` 要原样回传：DeepSeek 的 compat 标记要求如此，
    缺了它多轮对话会被拒。
    """
    message: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
    if response.reasoning:
        message["reasoning_content"] = response.reasoning
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_raw or "{}"},
            }
            for call in response.tool_calls
        ]
    return message


def tool_message(call_id: str, payload: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": payload}


def describe_llm_error(exc: LLMError) -> str:
    """把一次调用的失败翻译成「到底哪儿的问题」。

    对一台无人值守的机器来说，**「key 错了」和「网断了」是两件事**：
    前者要人去换 key，后者什么都不用做。混成一句「调用失败」，
    看日志的人只能去猜。
    """
    by_status = {
        401: "key 无效（打错、被吊销，或不是这个端点的 key）",
        402: "余额不足",
        403: "这个 key 没有访问该模型的权限",
        404: "模型名或端点不对",
        429: "被限流（key 本身没问题，过一会儿再试）",
    }
    if exc.status in by_status:
        return by_status[exc.status]
    if exc.status >= 500:
        return f"对方服务端故障 {exc.status}（key 本身没问题）"
    return str(exc)
