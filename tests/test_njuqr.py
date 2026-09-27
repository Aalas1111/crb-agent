"""扫码登录：页面字段解析、状态机、cookie 落盘。

全程用 ``httpx.MockTransport`` 顶替 authserver —— 这里一个真实请求都不发。
``tests/fixtures/auth_login_excerpt.html`` 是**真实登录页**的一段（生效的那张
qrLoginForm 及其前面的隐藏域），不是手写的假 HTML：解析器的价值就在于
扛得住学校模板的真实结构。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from crb_agent import njuqr

FIXTURE = Path(__file__).parent / "fixtures" / "auth_login_excerpt.html"


@pytest.fixture
def login_page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_hidden_field_parsing_from_real_page(login_page):
    """从真实页面里取出 execution；lt 在真实页面里就是空值，也该如实返回空。"""
    assert njuqr._hidden(login_page, "execution") == "e1s1"
    assert njuqr._hidden(login_page, "lt") == ""
    assert njuqr._hidden(login_page, "uuid") == ""
    assert njuqr._hidden(login_page, "不存在的字段") == ""


def test_error_hint_extraction():
    assert "账号或密码错误" in njuqr._error_hint(
        '<span id="showErrorTip" class="form-error">账号或密码错误</span>'
    )
    assert njuqr._error_hint("<html>没有提示</html>") == ""


# ---------------------------------------------------------------- 状态机
def _transport(
    *,
    status_sequence: list[str],
    set_castgc: bool = True,
    app_cookie_after: int = 1,
) -> httpx.MockTransport:
    """造一个会按剧本走的 authserver。

    刻意照真实链路搭：``authserver`` 只下发 ``CASTGC``，然后 **302 到 ehallapp**，
    ``MOD_AUTH_CAS`` 由 ehallapp 那一次响应下发（跨域 set-cookie 在浏览器和
    httpx 里都会被丢掉，所以这里不能图省事把两个 cookie 塞在同一个响应上）。

    ``app_cookie_after=2`` 用来模拟「第一次进应用页还没给应用会话」——
    这时 :meth:`QrLogin.complete` 应该自己再走一次应用入口把它补上。
    """
    visits = {"app": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "ehallapp" in request.url.host:
            visits["app"] += 1
            headers = []
            if visits["app"] >= app_cookie_after:
                headers.append(("set-cookie", "MOD_AUTH_CAS=abc; Path=/"))
            return httpx.Response(200, headers=headers, text="<html>app</html>")
        if path.endswith("/authserver/login"):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    text="""<form><input type="hidden" name="execution" value="e1s1" />
                    <input type="hidden" name="lt" value="" /></form>""",
                )
            headers = []
            if set_castgc:
                headers.append(("set-cookie", "CASTGC=TGT-12345; Path=/"))
            headers.append(
                (
                    "location",
                    "https://ehallapp.nju.edu.cn/jwapp/sys/jsjy/*default/index.do?ticket=ST-1",
                )
            )
            return httpx.Response(302, headers=headers, text="")
        if path.endswith("/qrCode/getToken"):
            return httpx.Response(200, text="QR-abc123")
        if path.endswith("/qrCode/getCode"):
            return httpx.Response(
                200, content=b"\x89PNG\r\n\x1a\nfake", headers={"content-type": "image/png"}
            )
        if path.endswith("/qrCode/getStatus.htl"):
            index = min(visits.setdefault("status", 0), len(status_sequence) - 1)
            visits["status"] += 1
            return httpx.Response(200, text=status_sequence[index])
        return httpx.Response(404, text="unexpected " + str(request.url))

    return httpx.MockTransport(handler)


def _login(transport: httpx.MockTransport) -> njuqr.QrLogin:
    return njuqr.QrLogin(transport=transport, base_url=njuqr.AUTH_HOST)


def test_start_returns_a_real_png_and_token():
    login = _login(_transport(status_sequence=["0"]))
    ticket = login.start()
    assert ticket.uuid == "QR-abc123"
    assert ticket.png.startswith(b"\x89PNG")
    login.close()


def test_status_polling_reports_scanned_then_confirmed():
    login = _login(_transport(status_sequence=["0", "2", "1"]))
    ticket = login.start()
    assert login.status(ticket.uuid) == "0"
    assert login.status(ticket.uuid) == njuqr.STATUS_SCANNED
    assert login.status(ticket.uuid) == njuqr.STATUS_CONFIRMED
    login.close()


def test_complete_captures_both_cookies():
    login = _login(_transport(status_sequence=["1"]))
    ticket = login.start()
    result = login.complete(ticket.uuid)
    assert "CASTGC" in result.names
    assert "MOD_AUTH_CAS" in result.names
    assert result.has_both()
    login.close()


def test_complete_fails_loudly_when_no_ticket_came_back():
    """没拿到 CASTGC 时不能假装成功——把学校的错误提示捞出来一起报。"""
    login = _login(_transport(status_sequence=["1"], set_castgc=False))
    ticket = login.start()
    with pytest.raises(njuqr.AuthFlowError) as excinfo:
        login.complete(ticket.uuid)
    assert "CASTGC" in str(excinfo.value)
    login.close()


def test_complete_retries_app_entry_when_only_castgc_arrived():
    """有 CAS 票据、应用会话还没建：再走一次应用入口把它补上。"""
    login = _login(_transport(status_sequence=["1"], app_cookie_after=2))
    ticket = login.start()
    result = login.complete(ticket.uuid)
    assert "CASTGC" in result.names
    assert "MOD_AUTH_CAS" in result.names
    login.close()


def test_one_shot_login_cannot_be_reused():
    login = _login(_transport(status_sequence=["1"]))
    ticket = login.start()
    login.complete(ticket.uuid)
    with pytest.raises(njuqr.AuthFlowError):
        login.complete(ticket.uuid)
    login.close()


def test_status_swallows_network_errors():
    """扫码是人在操作，网络抖一下不该把整个登录判死。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/qrCode/getStatus.htl"):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(404)

    login = _login(httpx.MockTransport(handler))
    assert login.status("QR-x") == ""
    login.close()


# ---------------------------------------------------------------- 落盘
def test_save_storage_state_matches_crb_expectations(tmp_path: Path):
    """``crb.Session.load()`` 读的是 ``cookies[].{name,value,domain,path}``。"""
    target = tmp_path / "auth.json"
    cookies = [
        {"name": "CASTGC", "value": "TGT-1", "domain": "authserver.nju.edu.cn", "path": "/"},
        {"name": "MOD_AUTH_CAS", "value": "m1", "domain": "ehallapp.nju.edu.cn", "path": "/"},
    ]
    path = njuqr.save_storage_state(cookies, target)
    assert path == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["cookies"] == cookies
    assert payload["origins"] == []
    assert payload["saved_by"] == "crb-agent qr"


def test_load_auth_summary_never_leaks_values(tmp_path: Path):
    target = tmp_path / "auth.json"
    njuqr.save_storage_state(
        [{"name": "CASTGC", "value": "超级机密的票据", "domain": "x", "path": "/"}], target
    )
    summary = njuqr.load_auth_summary(target)
    assert summary["exists"] is True
    assert summary["names"] == ["CASTGC"]
    assert "超级机密的票据" not in json.dumps(summary, ensure_ascii=False)


def test_load_auth_summary_handles_missing_and_broken(tmp_path: Path):
    missing = njuqr.load_auth_summary(tmp_path / "nope.json")
    assert missing["exists"] is False
    broken = tmp_path / "broken.json"
    broken.write_text("{{{", encoding="utf-8")
    assert njuqr.load_auth_summary(broken)["broken"] is True


def test_default_auth_path_matches_crb(monkeypatch, isolated_home):
    """写错地方就等于没登录，所以默认落点必须跟 crb 一致。"""
    path = njuqr.crb_auth_file()
    assert path.name == "auth.json"
    assert path.parent.name == ".crb"
    monkeypatch.setenv("CRB_AUTH_FILE", str(isolated_home / "elsewhere.json"))
    assert njuqr.crb_auth_file() == isolated_home / "elsewhere.json"
