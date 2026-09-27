"""HTTP 服务：一个密钥门 + 一个 agent 界面 + 一条 SSE 流。

路由总览::

    GET  /                            → 去 /agent
    GET  /healthz                     → "ok"（公开；只回一个词，不泄露任何东西）
    GET  /agent                       → 界面（先过密钥，再过登录态）
    GET  /agent/auth                  → 扫码登录页
    GET  /agent/static/<file>         → 前端静态文件
    GET  /agent/api/status            → crb 登录态 / 学期（顶部状态条用）
    GET  /agent/api/sessions          → 会话列表（左侧栏）
    POST /agent/api/sessions          → 新建会话
    GET  /agent/api/sessions/<id>     → 一个会话的 feed（历史回放）
    DEL  /agent/api/sessions/<id>     → 删除会话
    POST /agent/api/sessions/<id>/messages → 发一条消息，SSE 流式返回这一轮
    POST /agent/api/auth/qr           → 开一张登录二维码
    GET  /agent/api/auth/qr/<uuid>    → 查二维码状态（扫到即完成登录）

**密钥**：``?key=…`` 或 ``crba_session`` Cookie（见 :func:`_cookie_for`）。用密钥
打开之后会立刻把 URL 里的 key 摘掉换成 Cookie —— 密钥待在地址栏里就会进浏览器
历史、进截图、进别人肩膀上的视线。密钥错了（或压根没给）只显示「密钥错误」，
不透露这个页面上有什么。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from . import events as ev
from . import njuqr
from .agent import Agent, read_feed
from .config import Settings
from .events import format_sse
from .llm import StreamingLLM
from .prompts import system_prompt
from .store import SessionStore
from .tools import Toolbox

WEB_DIR = Path(__file__).parent / "web"
#: 静态资源的根 = ``web/static/``。URL 前缀 ``/agent/static/`` 直接映射到这里，
#: 于是 HTML 里写 ``/agent/static/app.js`` 就能拿到 ``web/static/app.js``
#: （app.js 里的相对 import "./api.js" 也随之对齐）。
ASSETS_DIR = WEB_DIR / "static"
COOKIE_NAME = "crba_session"

#: 登录态检查的缓存时长。``crb doctor`` 要打三次学校接口，每次刷页面都真查一遍
#: 既慢又没礼貌；而登录失效是个分钟级的事件，缓存 90 秒完全够。
AUTH_TTL = 90.0

DENIED_TEXT = "密钥错误"


def _cookie_for(access_key: str) -> str:
    """把访问密钥换成一个 Cookie 值。

    用 HMAC 而不是「密钥本身」或「随机数 + 内存表」：前者会把密钥泄露到
    Cookie 里；后者一重启就全员掉线。HMAC 是确定的（重启不失效）、
    不可逆的（拿到 Cookie 反推不出密钥），而且换密钥等于换 Cookie。
    """
    return hmac.new(access_key.encode("utf-8"), b"crba-session-v1", hashlib.sha256).hexdigest()


class AuthGate:
    """管登录态：查一次、缓存一会儿、登录成功后作废。"""

    def __init__(self, toolbox: Toolbox) -> None:
        self._toolbox = toolbox
        self._lock = threading.Lock()
        self._checked_at = 0.0
        self._result: dict[str, Any] = {"ok": False, "detail": "还没查过"}

    def status(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            fresh = time.time() - self._checked_at < AUTH_TTL
            if fresh and not force and self._result.get("ok"):
                return self._result
        result = self._probe()
        with self._lock:
            self._result = result
            self._checked_at = time.time()
        return result

    def invalidate(self) -> None:
        with self._lock:
            self._checked_at = 0.0
            self._result = {"ok": False, "detail": "刚登录过，待复核"}

    def _probe(self) -> dict[str, Any]:
        outcome = self._toolbox.execute("crb_status", {})
        data = outcome.data or {}
        # kind 决定「下一步该做什么」：没登录 → 扫码；被风控拦 → 扫码**没用**。
        # 把这两件事混成一句「登录态不可用」会让人对着一个解决不了的问题反复扫码。
        if outcome.status == "ok":
            kind = "ok"
        else:
            kind = str(data.get("kind") or "unknown")
        return {
            "ok": outcome.status == "ok",
            "kind": kind,
            "detail": outcome.summary,
            "data": data,
        }


class App:
    """把各个零件装起来。字段都是显式的——省得在函数之间传一大包东西。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = SessionStore(settings.workspace)
        self.toolbox = Toolbox(settings)
        self.gate = AuthGate(self.toolbox)
        self.agent = Agent(
            store=self.store,
            toolbox=self.toolbox,
            system_prompt=system_prompt(),
            llm=StreamingLLM(
                base_url=settings.llm_base_url,
                api_key=settings.llm_key,
                model=settings.llm_model,
            ),
        )
        self._run_locks: dict[str, threading.Lock] = {}
        self._qr: njuqr.QrLogin | None = None
        self._qr_token: str = ""


# ---------------------------------------------------------------- 路由
def build_app(settings: Settings) -> Starlette:
    """建好 :class:`App`（零件）与路由，返回 ASGI 应用。``crba serve`` 走这里。"""
    state = App(settings)
    routes = [
        Route("/", _root),
        Route("/healthz", _healthz),
        Route("/agent", _page),
        Route("/agent/", _page),
        Route("/agent/auth", _auth_page),
        Route("/agent/api/status", _api_status),
        Route("/agent/api/sessions", _api_sessions, methods=["GET", "POST"]),
        Route("/agent/api/sessions/{session_id}", _api_session, methods=["GET", "DELETE"]),
        Route("/agent/api/sessions/{session_id}/messages", _api_messages, methods=["POST"]),
        Route("/agent/api/auth/qr", _api_qr_start, methods=["POST"]),
        Route("/agent/api/auth/qr/{token}", _api_qr_status),
        # 静态文件放最后：它带 {path:path} 的兜底匹配，别把 /agent/api/* 吃掉。
        Route("/agent/static/{path:path}", _static),
    ]
    app = Starlette(routes=routes, debug=False)
    app.state.crba = state
    return app


# ---------------------------------------------------------------- 密钥门
def _state(request: Request) -> App:
    return request.app.state.crba  # type: ignore[no-any-return]


def _denied() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "denied.html").read_text(encoding="utf-8"), status_code=403)


def _key_ok(app: App, request: Request) -> bool:
    given = request.query_params.get("key") or ""
    if given and hmac.compare_digest(given, app.settings.access_key):
        return True
    cookie = request.cookies.get(COOKIE_NAME) or ""
    expected = _cookie_for(app.settings.access_key)
    return bool(cookie) and hmac.compare_digest(cookie, expected)


def _clean_url(request: Request) -> str:
    """把 URL 里的 ``key`` 摘掉后剩下的地址（其它参数原样保留）。"""
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k != "key"]
    query = "&".join(f"{k}={v}" for k, v in pairs)
    return f"{request.url.path}?{query}" if query else request.url.path


def _set_cookie(response: Response, app: App) -> Response:
    response.set_cookie(
        COOKIE_NAME,
        _cookie_for(app.settings.access_key),
        httponly=True,
        samesite="lax",
        # 服务器是明文 HTTP（没有域名、没有证书），所以这里**不能**设 Secure：
        # 设了浏览器就不会回传，等于把自己锁在门外。这个取舍写在 docs/deploy.md。
        secure=False,
        max_age=30 * 24 * 3600,
        path="/",
    )
    return response


def _guard(request: Request, *, page: bool) -> Response | None:
    """过密钥门。返回 None 表示放行。

    ``page=True`` 时会顺带把 URL 里的 key 换成 Cookie 并重定向 —— 只对页面做，
    因为 API 请求重定向会破坏 fetch 的语义。
    """
    app = _state(request)
    if not _key_ok(app, request):
        return _denied()
    if page and request.query_params.get("key"):
        return _set_cookie(RedirectResponse(_clean_url(request), status_code=302), app)
    return None


# ---------------------------------------------------------------- 页面
async def _root(request: Request) -> Response:
    return RedirectResponse("/agent", status_code=302)


async def _healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


async def _page(request: Request) -> Response:
    blocked = _guard(request, page=True)
    if blocked is not None:
        return blocked
    app = _state(request)
    status = await asyncio.to_thread(app.gate.status)
    if status.get("ok"):
        return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))
    # 三种「用不了」**不要**都往扫码页送：扫码只在「服务器自己能直连学校」
    # 的部署形态下有用；而现在 `crb` 是交给本机执行器跑的（见 docs/deploy.md §0.1），
    # 登录态也只能在那台机器上维护。统统交给 blocked 页按 kind 说清出路。
    # （`/agent/auth` 这个路由保留：换成能直连的部署时它仍然可用。）
    return HTMLResponse((WEB_DIR / "blocked.html").read_text(encoding="utf-8"))


async def _auth_page(request: Request) -> Response:
    blocked = _guard(request, page=True)
    if blocked is not None:
        return blocked
    return HTMLResponse((WEB_DIR / "auth.html").read_text(encoding="utf-8"))


async def _static(request: Request) -> Response:
    blocked = _guard(request, page=False)
    if blocked is not None:
        return blocked
    relative = request.path_params["path"]
    # 路径不可越狱：resolve 之后必须仍在 ASSETS_DIR 里（符号链接也骗不过去）。
    target = (ASSETS_DIR / relative).resolve()
    if ASSETS_DIR.resolve() not in target.parents or not target.is_file():
        return PlainTextResponse("not found", status_code=404)
    return Response(
        target.read_bytes(),
        media_type=_MEDIA.get(target.suffix, "application/octet-stream"),
    )


_MEDIA = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}


# ---------------------------------------------------------------- API
async def _api_status(request: Request) -> Response:
    blocked = _guard(request, page=False)
    if blocked is not None:
        return blocked
    app = _state(request)
    status = await asyncio.to_thread(app.gate.status, force=True)
    return JSONResponse(
        {
            "auth_ok": bool(status.get("ok")),
            "kind": status.get("kind", "unknown"),
            "detail": status.get("detail", ""),
            "term": (status.get("data") or {}).get("term", ""),
            "model": app.settings.llm_model,
        }
    )


async def _api_sessions(request: Request) -> Response:
    blocked = _guard(request, page=False)
    if blocked is not None:
        return blocked
    app = _state(request)
    if request.method == "POST":
        session_id = app.store.create()
        return JSONResponse({"id": session_id}, status_code=201)
    return JSONResponse({"sessions": app.store.list()})


async def _api_session(request: Request) -> Response:
    blocked = _guard(request, page=False)
    if blocked is not None:
        return blocked
    app = _state(request)
    session_id = request.path_params["session_id"]
    try:
        exists = app.store.exists(session_id)
    except ValueError:
        exists = False
    if not exists:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    if request.method == "DELETE":
        app.store.delete(session_id)
        return JSONResponse({"ok": True})
    return JSONResponse({"id": session_id, "feed": read_feed(app.store, session_id)})


async def _api_messages(request: Request) -> Response:
    blocked = _guard(request, page=False)
    if blocked is not None:
        return blocked
    app = _state(request)
    session_id = request.path_params["session_id"]
    if not app.store.exists(session_id):
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "消息是空的"}, status_code=400)

    lock = app._run_locks.setdefault(session_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "这个会话正在跑，等它结束再发"}, status_code=409)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    cancel = threading.Event()

    def emit(event: ev.Event) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("event", event))

    def worker() -> None:
        try:
            app.agent.run(session_id, text, emit=emit, cancel=cancel)
        finally:
            lock.release()
            loop.call_soon_threadsafe(queue.put_nowait, ("end", None))

    threading.Thread(target=worker, name=f"crba-run-{session_id[:8]}", daemon=True).start()

    async def stream() -> Any:
        # 首字节先来一个注释行：让代理/浏览器确认「这条流已经开了」，
        # 也让用户立刻看到「已发出」而不是等模型第一个 token。
        yield ": open\n\n"
        while True:
            if await request.is_disconnected():
                cancel.set()
                return
            try:
                kind, item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                # 工具调用可能跑好几分钟（crb plan 要逐条查空闲教室）。
                # 沉默的连接会被中间设备掐掉，所以定期吐一个心跳注释。
                yield ": ping\n\n"
                continue
            if kind == "end":
                return
            yield format_sse(item)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- 扫码登录
async def _api_qr_start(request: Request) -> Response:
    blocked = _guard(request, page=False)
    if blocked is not None:
        return blocked
    app = _state(request)
    app._qr = njuqr.QrLogin()
    try:
        ticket = await asyncio.to_thread(app._qr.start)
    except njuqr.AuthFlowError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    app._qr_token = ticket.uuid
    return JSONResponse({"token": ticket.uuid, "png": _data_url(ticket.png)})


async def _api_qr_status(request: Request) -> Response:
    blocked = _guard(request, page=False)
    if blocked is not None:
        return blocked
    app = _state(request)
    token = request.path_params["token"]
    if app._qr is None or token != app._qr_token:
        return JSONResponse({"state": "expired", "detail": "二维码已失效，请刷新。"})

    raw = await asyncio.to_thread(app._qr.status, token)
    if raw == njuqr.STATUS_SCANNED:
        return JSONResponse({"state": "scanned", "detail": "已扫描，请在手机上确认登录。"})
    if raw == njuqr.STATUS_EXPIRED:
        return JSONResponse({"state": "expired", "detail": "二维码已过期，请刷新。"})
    if raw != njuqr.STATUS_CONFIRMED:
        return JSONResponse({"state": "idle", "detail": "等待扫描…"})

    # 状态 1：手机上点了确认，把票据换成登录态并落盘。
    try:
        result = await asyncio.to_thread(app._qr.complete, token)
    except njuqr.AuthFlowError as exc:
        return JSONResponse({"state": "error", "detail": str(exc)})
    path = njuqr.save_storage_state(result.cookies, app.settings.crb_auth_file)
    app.gate.invalidate()
    detail = f"登录成功，登录态已写入 {path}"
    if not result.has_both():
        detail += f"（只拿到 {sorted(result.names)}，如果界面提示登录失效，请再扫一次）"
    return JSONResponse({"state": "done", "detail": detail})


def _data_url(png: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
