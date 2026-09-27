"""把「调学校」这件事留在能过的那台机器上执行（**只绑 127.0.0.1**）。

背景（2026-09-27 实测）：学校对办事大厅接口的判定**不只看出口 IP** ——
同一份登录态、同一个出口 IP（用 SSH 隧道把服务器的出口换成校园网），
从本机发出就 200、从服务器发出就 403；换 TLS 版本、换浏览器 TLS 指纹、
换 HTTP 客户端都不行。**准确的机制没能定位**，但结论很清楚：
**请求必须从这台机器发出。**

所以：本机跑这个小执行器，服务器上的 crb-agent 把 `crb ...` 经 SSH 隧道
发过来执行。服务器侧只用一个转发脚本（`remote_crb.py`），
靠 `CRBA_CRB_BIN` 指过去 —— **不需要改 crb-agent 的任何 Python 代码**。

安全：
* 只绑 127.0.0.1，本机之外只能经 SSH 隧道到达；
* 只跑 `crb`，**不接受任意命令**；子命令还有白名单；
* 拒绝 `login`（那会弹浏览器，应该由人手动跑）；
* 可选的 `CRBA_EXEC_TOKEN`：两边都设上就校验，防止隧道那头被别的进程占用。

用法：
    python local_crb_executor.py 18890            # 起执行器（前台）
    # 服务器侧（见 docs/deploy.md §0.1）
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

#: 允许的子命令。`login` 刻意不在里面 —— 它要弹浏览器，得由人手动跑。
ALLOWED = {
    "doctor",
    "campus",
    "buildings",
    "free",
    "plan",
    "borrow",
    "profile",
    "skill",
    "version",
}

TIMEOUT = 900  # crb plan 要逐条查空闲教室，给足


def _crb_command() -> list[str]:
    """本机 crb 怎么调：默认 `crb`，可用 CRBA_LOCAL_CRB 覆盖（如 venv 里的路径）。"""
    raw = os.environ.get("CRBA_LOCAL_CRB", "crb")
    return shlex.split(raw)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "body 不是合法 JSON"})

        token = os.environ.get("CRBA_EXEC_TOKEN", "")
        if token and body.get("token") != token:
            return self._send(403, {"error": "token 不对"})

        argv = body.get("argv") or []
        if not isinstance(argv, list) or not argv:
            return self._send(400, {"error": "缺少 argv"})
        if argv[0] not in ALLOWED:
            return self._send(403, {"error": f"不允许的子命令：{argv[0]!r}"})

        try:
            proc = subprocess.run(
                [*_crb_command(), *[str(a) for a in argv]],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                check=False,
            )
        except FileNotFoundError as exc:
            return self._send(500, {"error": f"本机找不到 crb：{exc}"})
        except subprocess.TimeoutExpired:
            return self._send(504, {"error": f"crb 超时（{TIMEOUT}s）"})

        return self._send(
            200,
            {"code": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or ""},
        )

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        return


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18890
    print(
        f"crb 执行器已就绪：http://127.0.0.1:{port}\n"
        f"  crb 命令：{_crb_command()}\n"
        f"  允许的子命令：{'、'.join(sorted(ALLOWED))}\n"
        f"  auth 用本机的 CRB_AUTH_FILE（默认 ~/.crb/auth.json）\n"
        f"  下一步：ssh -N -R {port}:127.0.0.1:{port} lihe@<服务器>",
        flush=True,
    )
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
