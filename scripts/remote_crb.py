"""服务器侧的 `crb` 转发器：把命令经 SSH 隧道交给**本机**执行。

为什么需要它（2026-09-27 实测）：学校对办事大厅接口的判定**不只看出口 IP** ——
同一份登录态、同一个出口 IP（用隧道把服务器出口换成校园网）、同一组 header，
**从本机发出就 200、从服务器发出就 403**。换 TLS 版本（1.2/1.3）、换浏览器
TLS 指纹（chrome/safari impersonate）、换 HTTP 客户端（curl/httpx）都不行。
准确的机制没能定位，但结论很清楚：**请求必须从能过的那台机器发出**。

所以：服务器不再自己调 `crb`，而是把它交给本机上的执行器
（`scripts/local_crb_executor.py`，只绑 127.0.0.1，经 `ssh -R` 暴露过来）。

接法是**零代码改动**：crb-agent 的 `CRBA_CRB_BIN` 本来就允许带参数的命令
（`tools._split_bin` 用 shlex 拆），指到这个脚本即可：

    Environment="CRBA_CRB_BIN=python /opt/crb-agent/scripts/remote_crb.py"

退出码：原样转发 crb 的；**4 表示「连不上本机执行器」**（与 crb 自己的
0/1/2/3 区分开，见 `tools._kind_for`）—— 那种情况的提示是「你的电脑没开机 /
没跑执行器」，而不是「被风控拦」，两者的处理方式完全不同。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

#: 「连不上执行器」的专用退出码（crb 自己只用 0/1/2/3）。
EXIT_EXECUTOR_OFFLINE = 4

HINT = (
    "连不上本机的 crb 执行器。\n"
    "  → 需要在**你自己的电脑**上跑起这两个：\n"
    "      python scripts/local_crb_executor.py 18890\n"
    "      ssh -N -R 18890:127.0.0.1:18890 lihe@<服务器>\n"
    "    （或者用 scripts/start_local_egress.sh 一键起；见 docs/deploy.md §0.1）\n"
    "  电脑没开机时，界面仍然能用，只是调学校的工具会走到这里。"
)


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        sys.stderr.write("用法：remote_crb.py <crb 的参数...>\n")
        return 2

    port = os.environ.get("CRBA_EXEC_PORT", "18890")
    payload = json.dumps(
        {"argv": argv, "token": os.environ.get("CRBA_EXEC_TOKEN", "")}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=payload,
        headers={"content-type": "application/json"},
    )

    try:
        with urllib.request.urlopen(
            request, timeout=float(os.environ.get("CRBA_EXEC_TIMEOUT", "900"))
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.stderr.write(f"{HINT}\n（具体错误：{type(exc).__name__}: {exc}）\n")
        return EXIT_EXECUTOR_OFFLINE

    if "error" in result:
        sys.stderr.write(f"执行器拒绝了这次调用：{result['error']}\n")
        return 2

    # 原样把子进程的输出交回去：crb-agent 按 stdout 解析 JSON、按退出码判成败。
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
    return int(result.get("code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
