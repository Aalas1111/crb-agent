"""南大统一认证的**服务端扫码登录**。

为什么不是「把用户跳转到南大统一认证页，等他扫完再跳回来」
--------------------------------------------------------
因为那样**拿不到登录态**。CAS 登录成功后，`CASTGC` 与 `MOD_AUTH_CAS`
是下发到**扫码的那个浏览器**里的，而且都是 HttpOnly —— 用户扫完码，
票据在他自己的浏览器里，我们的服务器手上什么都没有。

（试过的死路：把 `service` 指向我们自己的回调。CAS 的 service ticket 是
**绑在 service 上**的，我们拿着一个为 `http://ours/...` 签发的 ticket 去请求
ehallapp，ehallapp 会拒掉；而如果 service 填 ehallapp，票据就落在他浏览器里了。
两头都不通。）

所以反过来做：**二维码由我们服务器自己取下来显示**。谁取的码，谁就是
「那个浏览器」——用户扫的就是我们这一张，`CASTGC` 自然就下到我们的
cookie jar 里。这也正是 ``crb`` 的 `auth.py` 里那句 TODO（「把二维码直接抓取到
应用内展示」）想要的形态，只是现在真的做到了。

流程（实测，2026-09-27）
------------------------
1. ``GET  /authserver/login?service=<ehallapp jsjy 入口>`` → 拿 ``execution`` / ``lt``
2. ``GET  /authserver/qrCode/getToken``                       → 拿到 uuid
3. ``GET  /authserver/qrCode/getCode?uuid=<uuid>``            → 400×400 PNG 二维码
4. ``GET  /authserver/qrCode/getStatus.htl?uuid=<uuid>``      → 轮询
   ``0`` 没人扫 / ``2`` 扫了待确认 / ``1`` 已确认 / ``3`` 已过期
5. ``GET``?状态=1 → ``POST /authserver/login?display=qrLogin&service=<…>``
   带上 ``uuid/lt/execution/cllt=qrLogin/dllt=generalLogin/_eventId=submit/rmShown=1``
   → CAS 下发 ``CASTGC`` 并 302 到 ehallapp，ehallapp 校验票据后下发 ``MOD_AUTH_CAS``
6. 把 cookie 落成 ``storage_state`` 形状写到 ``~/.crb/auth.json``（``crb`` 认这个格式）

**全程不接触用户密码**：我们只搬运二维码与票据。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import crb_auth_file

AUTH_HOST = "https://authserver.nju.edu.cn"
LOGIN_PATH = "/authserver/login"
SERVICE = "https://ehallapp.nju.edu.cn/jwapp/sys/jsjy/*default/index.do"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

#: 二维码状态。``2`` 是「扫到了，等手机点确认」——这一步要显示给用户看，
#: 否则他会以为没扫上、反复扫。
STATUS_IDLE = "0"
STATUS_CONFIRMED = "1"
STATUS_SCANNED = "2"
STATUS_EXPIRED = "3"


class AuthFlowError(RuntimeError):
    """扫码流程里的任何一步没按预期走。"""


@dataclass
class QRTicket:
    uuid: str
    png: bytes


@dataclass
class LoginResult:
    cookies: list[dict[str, Any]] = field(default_factory=list)

    @property
    def names(self) -> set[str]:
        return {c["name"] for c in self.cookies}

    def has_both(self) -> bool:
        return "CASTGC" in self.names and "MOD_AUTH_CAS" in self.names


class QrLogin:
    """一次扫码登录的现场。

    生命期就是「用户打开登录页 → 扫完码」这几分钟。**一个实例只能登录一次**：
    cookie jar 是有状态的，复用会让上一轮的票据混进下一轮。
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        base_url: str = AUTH_HOST,
    ) -> None:
        """``transport`` / ``base_url`` 只为测试而留（用 MockTransport 顶替学校）。

        生产路径永远不传它们——那样请求才会真的打到 authserver。
        """
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Referer": f"{AUTH_HOST}/authserver/login"},
            transport=transport,
            base_url=base_url,
        )
        self._execution = "e1s1"
        self._lt = ""
        self._prepared = False
        self._completed = False

    def close(self) -> None:
        self._client.close()

    # ---- 步骤 1~3：开一张二维码 -----------------------------------------
    def start(self) -> QRTicket:
        self._prepare()
        return self.refresh()

    def refresh(self) -> QRTicket:
        """换一张新码（旧码过期时用）。"""
        token = self._get_token()
        png = self._get_code(token)
        return QRTicket(uuid=token, png=png)

    def _prepare(self) -> None:
        if self._prepared:
            return
        try:
            resp = self._client.get(LOGIN_PATH, params={"service": SERVICE})
        except httpx.HTTPError as exc:
            raise AuthFlowError(f"打不开统一认证登录页：{exc}") from exc
        if resp.status_code != 200:
            raise AuthFlowError(f"登录页返回 HTTP {resp.status_code}")
        self._execution = _hidden(resp.text, "execution") or "e1s1"
        self._lt = _hidden(resp.text, "lt")
        self._prepared = True

    def _get_token(self) -> str:
        try:
            resp = self._client.get(
                "/authserver/qrCode/getToken",
                params={"ts": str(int(time.time() * 1000)), "uuid": ""},
            )
        except httpx.HTTPError as exc:
            raise AuthFlowError(f"取二维码失败：{exc}") from exc
        token = resp.text.strip()
        if not token:
            raise AuthFlowError("取二维码失败：getToken 返回空")
        return token

    def _get_code(self, token: str) -> bytes:
        try:
            resp = self._client.get("/authserver/qrCode/getCode", params={"uuid": token})
        except httpx.HTTPError as exc:
            raise AuthFlowError(f"取二维码图片失败：{exc}") from exc
        if resp.status_code != 200 or not resp.content:
            raise AuthFlowError(f"取二维码图片失败：HTTP {resp.status_code}")
        if not resp.headers.get("content-type", "").startswith("image/"):
            raise AuthFlowError(f"二维码不是图片：{resp.headers.get('content-type')}")
        return resp.content

    # ---- 步骤 4：轮询 ---------------------------------------------------
    def status(self, token: str) -> str:
        """查一次状态。超时/网络抖动**不抛异常**——返回空串，调用方继续轮询。

        扫码是人在操作，中途断一次网络不该把整个登录判死。
        """
        try:
            resp = self._client.get(
                "/authserver/qrCode/getStatus.htl",
                params={"ts": str(int(time.time() * 1000)), "uuid": token},
                timeout=8.0,
            )
        except httpx.HTTPError:
            return ""
        return resp.text.strip()

    # ---- 步骤 5~6：收网 -------------------------------------------------
    def complete(self, token: str) -> LoginResult:
        """用户已在手机上确认，用票据换登录态。"""
        if self._completed:
            raise AuthFlowError("这个登录现场已经用过了，请重新开一张二维码")
        self._prepare()
        body = {
            "lt": self._lt,
            "uuid": token,
            "cllt": "qrLogin",
            "dllt": "generalLogin",
            "execution": self._execution,
            "_eventId": "submit",
            "rmShown": "1",
        }
        try:
            resp = self._client.post(
                LOGIN_PATH,
                params={"display": "qrLogin", "service": SERVICE},
                data=body,
            )
        except httpx.HTTPError as exc:
            raise AuthFlowError(f"提交扫码结果失败：{exc}") from exc

        result = LoginResult(cookies=self._dump_cookies())
        if "CASTGC" not in result.names:
            # 票据没换成 CASTGC。CAS 认不出来时会回到登录页并带上错误提示，
            # 把那句提示捞出来——比一句「登录失败」有用得多。
            raise AuthFlowError(
                "扫码确认后没有拿到认证票据（CASTGC）。"
                f"{_error_hint(resp.text)}（HTTP {resp.status_code}，落在 {resp.url}）"
            )
        if "MOD_AUTH_CAS" not in result.names:
            # 有了 CAS 票据但应用会话还没建：再走一次应用入口，让它发应用 Cookie。
            try:
                self._client.get(SERVICE)
            except httpx.HTTPError:
                pass
            result = LoginResult(cookies=self._dump_cookies())
        self._completed = True
        return result

    def _dump_cookies(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for cookie in self._client.cookies.jar:
            out.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                    "expires": cookie.expires if cookie.expires is not None else -1,
                    "httpOnly": bool(cookie.get_nonstandard_attr("HttpOnly")),
                    "secure": bool(cookie.secure),
                    "sameSite": "Lax",
                }
            )
        return out


# ---------------------------------------------------------------- 落盘
def save_storage_state(cookies: list[dict[str, Any]], path: Path | None = None) -> Path:
    """写成 ``crb`` 认的 ``storage_state``。

    ``crb.Session.load()`` 只读 ``cookies[].{name,value,domain,path}``——
    **刻意不看 expires**，所以就算学校下的是会话级 Cookie，我们这份文件
    也能在服务进程活着的时候一直用。
    """
    target = path or crb_auth_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookies": cookies,
        "origins": [],
        "saved_at": time.time(),
        "saved_by": "crb-agent qr",
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:  # 尽量收紧权限（Windows 上可能无效，忽略即可）
        target.chmod(0o600)
    except OSError:
        pass
    return target


def load_auth_summary(path: Path | None = None) -> dict[str, Any]:
    """看一眼现有登录态长什么样（**不返回 cookie 值**）。"""
    target = path or crb_auth_file()
    if not target.is_file():
        return {"exists": False, "names": [], "saved_at": None}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exists": True, "names": [], "saved_at": None, "broken": True}
    names = sorted({c.get("name", "") for c in payload.get("cookies", []) if c.get("name")})
    return {"exists": True, "names": names, "saved_at": payload.get("saved_at")}


# ---------------------------------------------------------------- 小工具
_VALUE_RE = re.compile(r'value="([^"]*)"')


def _hidden(html: str, name: str) -> str:
    """从登录页里取一个隐藏域的 value。

    直接正则抓 `<input ... name="lt" ...>`：这个页面是学校自己的模板、结构稳定，
    为它引入 HTML 解析器不划算（也没有别的消费者）。页面里同名隐藏域会出现多次
    （每个登录方式一份），它们**各自的 value 一样**，所以取第一个即可。
    """
    pattern = re.compile(rf'<input[^>]*name="{re.escape(name)}"[^>]*>', re.IGNORECASE)
    for tag in pattern.finditer(html):
        found = _VALUE_RE.search(tag.group(0))
        if found:
            return found.group(1)
    return ""


_ERROR_RE = re.compile(r'id="showErrorTip"[^>]*>([^<]{2,200})<')


def _error_hint(html: str) -> str:
    found = _ERROR_RE.search(html)
    return f"学校返回：{found.group(1).strip()}" if found else ""
