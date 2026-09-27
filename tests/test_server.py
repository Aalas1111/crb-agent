"""HTTP 层：密钥门、登录态门、会话接口、SSE。

密钥门是这个服务唯一的防线（它能提交借用申请、能花 LLM 的 token），
所以这里既测「对的密钥放行」，也测「错密钥不透露任何东西」。
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from crb_agent.agent import Agent
from crb_agent.llm import LLMResponse, ToolCall
from crb_agent.server import COOKIE_NAME, _cookie_for, build_app

KEY = "test-key-123456"


@pytest.fixture
def client(settings):
    app = build_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _scripted(agent: Agent, steps: list[dict]) -> None:
    """把 agent 的模型换掉——SSE 测试不该真的去打 LLM。"""

    def generate(messages, tools, on_reasoning, on_content) -> LLMResponse:
        step = steps.pop(0)
        for piece in step.get("reasoning", []):
            on_reasoning(piece)
        for piece in step.get("content", []):
            on_content(piece)
        return LLMResponse(
            content="".join(step.get("content", [])),
            reasoning="".join(step.get("reasoning", [])),
            tool_calls=[
                ToolCall(id=c["id"], name=c["name"], arguments_raw=json.dumps(c.get("input", {})))
                for c in step.get("tools", [])
            ],
        )

    agent._generate = generate  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 密钥门
def test_missing_key_shows_only_the_denied_page(client):
    response = client.get("/agent", follow_redirects=False)
    assert response.status_code == 403
    assert "密钥错误" in response.text
    # 不透露页面上有什么
    assert "教室借用" not in response.text
    assert "session" not in response.text


def test_wrong_key_shows_only_the_denied_page(client):
    response = client.get("/agent?key=wrong-key", follow_redirects=False)
    assert response.status_code == 403
    assert "密钥错误" in response.text


def test_correct_key_sets_cookie_and_strips_it_from_the_url(client):
    response = client.get(f"/agent?key={KEY}&keep=1", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "key=" not in location  # 密钥不能继续待在地址栏里
    assert location == "/agent?keep=1"
    assert COOKIE_NAME in response.cookies

    # Cookie 之后就能直接进门了
    page = client.get("/agent")
    assert page.status_code == 200
    assert "教室借用" in page.text


def test_api_requires_key_too(client):
    assert client.get("/agent/api/sessions").status_code == 403
    assert client.post("/agent/api/sessions").status_code == 403


def test_static_requires_key(client):
    assert client.get("/agent/static/style.css").status_code == 403


def test_static_serves_the_frontend_once_authorized(client):
    client.get(f"/agent?key={KEY}")
    response = client.get("/agent/static/app.js")
    assert response.status_code == 200
    assert "streamMessage" in response.text


def test_static_cannot_escape_the_web_directory(client):
    client.get(f"/agent?key={KEY}")
    response = client.get("/agent/static/..%2f..%2fconfig.py")
    assert response.status_code in (403, 404)


def test_healthz_is_public_and_says_nothing(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_cookie_value_is_not_the_key(client):
    response = client.get(f"/agent?key={KEY}", follow_redirects=False)
    cookie = response.cookies[COOKIE_NAME]
    assert KEY not in cookie
    assert cookie == _cookie_for(KEY)


def test_changing_the_key_invalidates_old_cookies(settings):
    """换密钥等于换 Cookie——老 Cookie 立刻失效。"""
    from dataclasses import replace

    app = build_app(replace(settings, access_key="a-new-key"))
    with TestClient(app) as rotated:
        rotated.cookies.set(COOKIE_NAME, _cookie_for(KEY))
        assert rotated.get("/agent").status_code == 403


# ---------------------------------------------------------------- 登录态门
def test_valid_auth_leads_to_the_app(client):
    # 带密钥进来 → 换成 Cookie 并回到干净地址
    bootstrap = client.get(f"/agent?key={KEY}", follow_redirects=False)
    assert bootstrap.status_code == 302
    # 之后就是正常打开界面
    page = client.get("/agent")
    assert page.status_code == 200
    assert "把语雀上的申请，落成学校里的借用" in page.text


def test_invalid_auth_redirects_to_the_qr_page(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CRB_MODE", "noauth")
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        response = client.get("/agent", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/agent/auth"

        page = client.get("/agent/auth")
        assert page.status_code == 200
        assert "扫码" in page.text


def test_status_api_reports_auth_state(settings, monkeypatch):
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        payload = client.get("/agent/api/status").json()
        assert payload["auth_ok"] is True
        assert payload["term"] == "2026-2027-1"

    monkeypatch.setenv("FAKE_CRB_MODE", "noauth")
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        assert client.get("/agent/api/status").json()["auth_ok"] is False


def test_waf_block_explains_instead_of_bouncing_to_the_qr_page(settings, monkeypatch):
    """被风控拦时**不能**跳扫码页 —— 扫码解决不了它（登录态本来就是好的）。

    实测踩到：扫码成功、登录态写好了，但学校按出口 IP 拦接口调用（403）。
    旧行为是把用户弹回扫码页 → 扫了又弹回来 → 死循环，而用户完全不知道为什么。
    """
    monkeypatch.setenv("FAKE_CRB_MODE", "waf")
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        response = client.get("/agent", follow_redirects=False)
        assert response.status_code == 200, "不该重定向"
        assert "风控" in response.text
        assert "扫码没有用" in response.text


def test_status_api_distinguishes_waf_from_not_logged_in(settings, monkeypatch):
    monkeypatch.setenv("FAKE_CRB_MODE", "waf")
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        payload = client.get("/agent/api/status").json()
        assert payload["auth_ok"] is False
        assert payload["kind"] == "waf_blocked"
        assert "扫码没用" in payload["detail"]

    monkeypatch.setenv("FAKE_CRB_MODE", "noauth")
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        payload = client.get("/agent/api/status").json()
        assert payload["kind"] == "not_logged_in"


def test_not_logged_in_still_goes_to_the_qr_page(settings, monkeypatch):
    monkeypatch.setenv("FAKE_CRB_MODE", "noauth")
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        response = client.get("/agent", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/agent/auth"


def test_qr_page_is_reachable_with_key(settings, monkeypatch):
    monkeypatch.setenv("FAKE_CRB_MODE", "noauth")
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.get(f"/agent/auth?key={KEY}", follow_redirects=False)
        assert response.status_code == 302  # 换成 Cookie 再回到干净地址
        assert client.get("/agent/auth").status_code == 200


# ---------------------------------------------------------------- 会话接口
def test_session_lifecycle(client):
    client.get(f"/agent?key={KEY}")
    created = client.post("/agent/api/sessions")
    assert created.status_code == 201
    session_id = created.json()["id"]

    assert client.get("/agent/api/sessions").json()["sessions"][0]["id"] == session_id

    detail = client.get(f"/agent/api/sessions/{session_id}").json()
    assert detail["id"] == session_id
    assert detail["feed"] == []

    assert client.delete(f"/agent/api/sessions/{session_id}").status_code == 200
    assert client.get(f"/agent/api/sessions/{session_id}").status_code == 404


def test_unknown_session_is_404(client):
    client.get(f"/agent?key={KEY}")
    assert client.get("/agent/api/sessions/deadbeef").status_code == 404
    assert client.get("/agent/api/sessions/../../etc/passwd").status_code == 404


def test_empty_message_is_rejected(client):
    client.get(f"/agent?key={KEY}")
    session_id = client.post("/agent/api/sessions").json()["id"]
    response = client.post(f"/agent/api/sessions/{session_id}/messages", json={"text": "   "})
    assert response.status_code == 400


# ---------------------------------------------------------------- SSE
def test_message_streams_events_in_order(settings):
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        session_id = client.post("/agent/api/sessions").json()["id"]
        _scripted(
            app.state.crba.agent,
            [
                {
                    "reasoning": ["先看看"],
                    "tools": [{"id": "c1", "name": "crb_status", "input": {}}],
                },
                {"content": ["好了。"]},
            ],
        )
        response = client.post(
            f"/agent/api/sessions/{session_id}/messages", json={"text": "现在什么情况"}
        )
        assert response.status_code == 200
        body = response.text
        assert "event: reasoning_delta" in body
        assert "event: tool_call" in body
        assert "event: tool_result" in body
        assert "event: assistant_message" in body
        assert "event: done" in body

        # 顺序就是时间顺序
        order = [line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]
        assert order.index("tool_call") < order.index("tool_result")
        assert order.index("tool_result") < order.index("assistant_message")
        assert order[-1] == "done"


def test_history_is_replayable_after_a_run(settings):
    """发完消息之后拉 feed，应该能原样重放出同一批事件（历史 = 现场）。"""
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        session_id = client.post("/agent/api/sessions").json()["id"]
        _scripted(app.state.crba.agent, [{"content": ["结论"]}])
        client.post(f"/agent/api/sessions/{session_id}/messages", json={"text": "问题"})

        feed = client.get(f"/agent/api/sessions/{session_id}").json()["feed"]
        roles = [record["role"] for record in feed]
        assert roles[0] == "user"
        assert feed[0]["content"] == "问题"
        assert "run_start" in roles and "run_end" in roles
        event_types = [r["event"]["type"] for r in feed if r["role"] == "event"]
        assert event_types[-1] == "done"


def test_second_message_is_rejected_while_running(settings):
    """同一个会话同时只允许一轮——两个写者会互相盖掉。"""
    import threading

    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        session_id = client.post("/agent/api/sessions").json()["id"]
        state = app.state.crba
        lock = state._run_locks.setdefault(session_id, threading.Lock())
        lock.acquire()
        try:
            response = client.post(
                f"/agent/api/sessions/{session_id}/messages", json={"text": "挤进去"}
            )
            assert response.status_code == 409
        finally:
            lock.release()


def test_run_is_recorded_even_when_the_model_fails(settings):
    """模型挂了也要留痕：状态是 failed，并且有一条 error 事件。"""
    app = build_app(settings)
    with TestClient(app) as client:
        client.get(f"/agent?key={KEY}")
        session_id = client.post("/agent/api/sessions").json()["id"]

        def explode(messages, tools, on_reasoning, on_content):
            raise RuntimeError("模型不可用")

        app.state.crba.agent._generate = explode  # type: ignore[attr-defined]
        response = client.post(f"/agent/api/sessions/{session_id}/messages", json={"text": "试试"})
        assert "event: error" in response.text
        assert "event: done" in response.text

        feed = client.get(f"/agent/api/sessions/{session_id}").json()["feed"]
        endings = [record for record in feed if record["role"] == "run_end"]
        assert len(endings) == 1
        assert endings[0]["status"] == "failed"
