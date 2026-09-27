"""命令行入口。

crba serve          起服务（agent 界面 + SSE）
crba auth           在终端里走一次扫码登录（拿不到网页界面时用）
crba notify-poll    常驻轮询审批结果（systemd 单元，账本唯一写者）
crba notify-once    只跑一轮审批结果检查（人工排查；与常驻共用一把锁）
crba notify-show    打印审批结果账本概要
crba doctor         自检：密钥、LLM key、登录态、crb/yqa 能不能叫得动
crba version
"""

from __future__ import annotations

import random
import sys
import time
from contextlib import contextmanager
from typing import Annotated

import typer

from . import __version__, njuqr, notify, server
from .config import ConfigError, Settings, crb_auth_file
from .prompts import system_prompt
from .store import SessionStore
from .tools import Toolbox

# Windows 控制台默认 GBK，中文会乱码；强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="教室借用的下游 agent：把语雀侧的申请清单变成学校系统里的借用申请。",
)


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


@app.command("version")
def version_cmd() -> None:
    """显示版本。"""
    typer.echo(f"crba {__version__}")


@app.command("serve")
def serve_cmd(
    host: Annotated[
        str | None, typer.Option("--host", help="监听地址；默认 CRBA_HOST 或 0.0.0.0")
    ] = None,
    port: Annotated[int | None, typer.Option("--port", help="端口；默认 CRBA_PORT 或 8788")] = None,
) -> None:
    """起 HTTP 服务（`http://<地址>:<端口>/agent?key=<密钥>`）。"""
    import uvicorn

    settings = _settings()
    asgi = server.build_app(settings)
    bind_host = host or settings.host
    bind_port = port or settings.port
    typer.secho(
        f"crba 已启动：http://{bind_host}:{bind_port}/agent?key=…"
        f"（端口 {bind_port}；密钥见 CRBA_KEY，不打印）",
        fg=typer.colors.GREEN,
    )
    uvicorn.run(asgi, host=bind_host, port=bind_port, log_level="info", access_log=False)


@app.command("auth")
def auth_cmd() -> None:
    """在终端里扫码登录：打印二维码图片地址，扫完自动写入登录态。

    这条路子是为了「网页界面进不去但要把登录态修好」——二维码图片本身是
    authserver 的一个公开 GET，所以把地址抄到任何一台能上网的设备上打开都行，
    真正完成登录的是你手机上的那次确认。
    """
    settings = _settings()
    login = njuqr.QrLogin()
    try:
        ticket = login.start()
    except njuqr.AuthFlowError as exc:
        typer.secho(f"取二维码失败：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    url = f"{njuqr.AUTH_HOST}/authserver/qrCode/getCode?uuid={ticket.uuid}"
    typer.secho("用南京大学 APP / 微信扫这个二维码：", fg=typer.colors.GREEN)
    typer.echo(f"  {url}")
    typer.echo("  （用任何一台能上网的设备打开上面这个地址都行，码是同一张）")
    typer.echo(f"\n当前登录态落点：{crb_auth_file()}")

    last = ""
    try:
        while True:
            status = login.status(ticket.uuid)
            if status == njuqr.STATUS_SCANNED and last != status:
                typer.secho("已扫描，请在手机上点「确认登录」…", fg=typer.colors.YELLOW)
            elif status == njuqr.STATUS_EXPIRED:
                typer.secho("二维码已过期，请重新运行 crba auth。", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
            elif status == njuqr.STATUS_CONFIRMED:
                break
            last = status
            time.sleep(1.5)

        result = login.complete(ticket.uuid)
        path = njuqr.save_storage_state(result.cookies, settings.crb_auth_file)
        typer.secho(f"登录成功，登录态已写入 {path}", fg=typer.colors.GREEN)
        if not result.has_both():
            typer.secho(
                f"注意：只拿到 {sorted(result.names)}；如果命令仍提示登录失效，请再扫一次。",
                fg=typer.colors.YELLOW,
            )
    except njuqr.AuthFlowError as exc:
        typer.secho(f"登录失败：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        login.close()


@app.command("doctor")
def doctor_cmd() -> None:
    """自检：配置、登录态、crb / yqa 是否可用。"""
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        typer.secho(f"[FAIL] {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.secho("[ OK ] 配置", fg=typer.colors.GREEN)
    typer.echo(f"       监听 {settings.host}:{settings.port}")
    typer.echo(f"       模型 {settings.llm_model} @ {settings.llm_base_url}")
    typer.echo(f"       访问密钥 已设置（{len(settings.access_key)} 个字符，不回显）")
    typer.echo(f"       工作区 {settings.workspace}")
    typer.echo(f"       语雀工作区 {settings.yuque_workspace}")
    typer.echo(f"       语雀知识库 {settings.yqa_repo or '（未设置 YQA_REPO）'}")

    summary = njuqr.load_auth_summary(settings.crb_auth_file)
    if summary.get("exists"):
        names = ", ".join(summary.get("names") or []) or "（没有 cookie）"
        typer.secho(f"[ OK ] 登录态文件存在：{settings.crb_auth_file}", fg=typer.colors.GREEN)
        typer.echo(f"       cookie：{names}")
    else:
        typer.secho(f"[warn] 还没有登录态：{settings.crb_auth_file}", fg=typer.colors.YELLOW)
        typer.echo("       跑 `crba auth` 或打开 /agent/auth 扫一次码")

    toolbox = Toolbox(settings)
    tool_names = toolbox.names()
    typer.secho(f"[ OK ] 工具 {len(tool_names)} 个：{', '.join(tool_names)}", fg=typer.colors.GREEN)

    outcome = toolbox.execute("crb_status", {})
    if outcome.status == "ok":
        typer.secho(f"[ OK ] crb 可用：{outcome.summary}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"[FAIL] crb 不可用：{outcome.summary}", fg=typer.colors.RED)
        typer.echo(f"       CRBA_CRB_BIN = {settings.crb_bin}")

    outbox = settings.outbox()
    if outbox.is_dir():
        plan = outbox / "plan.json"
        typer.secho(f"[ OK ] 语雀产出目录可读：{outbox}", fg=typer.colors.GREEN)
        typer.echo(f"       plan.json：{'有' if plan.is_file() else '还没有'}")
    else:
        typer.secho(f"[warn] 语雀产出目录不存在：{outbox}", fg=typer.colors.YELLOW)

    store = SessionStore(settings.workspace)
    typer.secho(f"[ OK ] 会话留痕：{len(store.list())} 个", fg=typer.colors.GREEN)

    prompt = system_prompt()
    typer.echo(f"       系统提示词 {len(prompt)} 字符（含今天的日期）")
    typer.echo()
    typer.secho("自检结束。", fg=typer.colors.GREEN)


# ---------------------------------------------------------------- 审批结果
#: 轮询间隔的抖动上限（秒）。别让请求整点撞在一起。
JITTER = 60


class AlreadyRunning(RuntimeError):
    """另一个 notify 正在跑（账本只有一个写者）。"""


class NotifyBlocked(RuntimeError):
    """这一轮跑不了（登录态失效 / 被风控拦 / crb 叫不动）。``kind`` 说明是哪种。"""

    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


@contextmanager
def _single_writer(settings: Settings):
    """同一时刻只允许一个写者在跑（账本 / 文档都只有一个写者）。

    上游那次事故的教训：两个写者会互相覆盖产物 —— 快照回退、重复通知。
    所以常驻轮询和「手动跑一次」共用这把锁。
    """
    lock = settings.workspace / ".notify.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+")
    try:
        try:
            import fcntl
        except ImportError:
            # 非 POSIX（Windows 上本地开发）：不会并发，不锁。
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(f"另一个 notify 正在跑（拿不到 {lock}）") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _notify_round(settings: Settings, toolbox: Toolbox) -> dict:
    """跑一轮：查申请列表 → 比对记账 → 重生文档 → 有变化才让语雀那篇也跟着重生。

    **只读学校系统**：这里只调 ``borrow list``。别顺手加「自动重试提交」之类的东西。
    """
    outcome = toolbox.execute("crb_list_borrows", {})
    if outcome.status != "ok":
        raise NotifyBlocked(outcome.summary, str((outcome.data or {}).get("kind") or "unknown"))
    rows = (outcome.data or {}).get("borrows") or []
    result = notify.rebuild(
        settings.approval_dir(), settings.outbox(), rows, repo=settings.yqa_repo
    )
    if result["new"]:
        # 有变化才动语雀 —— 没变化时那边本来也不会变（approvaldoc 是幂等的），
        # 省掉一次网络往返。
        refreshed = toolbox.execute("yqa_refresh_approval", {})
        result["yqa"] = refreshed.summary
    return result


def _describe(settings: Settings, result: dict) -> str:
    parts = [
        f"查了 {result['checked']} 条",
        f"其中已结束 {result['ended_seen']} 条",
        f"新增 {result['new']} 条",
        f"通知文档共 {result['total']} 条",
    ]
    if result["unmatched"]:
        parts.append(
            f"认不出 {result['unmatched']} 条（见 {settings.approval_dir() / 'unmatched.json'}）"
        )
    if result.get("yqa"):
        parts.append(str(result["yqa"]))
    return "；".join(parts)


@app.command("notify-once")
def notify_once_cmd() -> None:
    """跑一轮审批结果检查（记账 + 重生文档），然后退出。

    人工排查用；常驻的那份是 `crba notify-poll`。两者共用一把锁。
    """
    settings = _settings()
    toolbox = Toolbox(settings)
    try:
        with _single_writer(settings):
            result = _notify_round(settings, toolbox)
    except AlreadyRunning as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(2) from exc
    except NotifyBlocked as exc:
        typer.secho(f"这一轮跑不了：{exc}", fg=typer.colors.RED, err=True)
        if exc.kind == "waf_blocked":
            typer.echo("  被学校风控拦（出口 IP 的问题）—— 见 docs/deploy.md §0。")
        elif exc.kind == "not_logged_in":
            typer.echo("  登录态失效：打开 /agent 扫码，或跑 crba auth。")
        raise typer.Exit(1) from exc
    typer.secho(f"✓ {_describe(settings, result)}", fg=typer.colors.GREEN)


@app.command("notify-poll")
def notify_poll_cmd() -> None:
    """常驻轮询审批结果（给 systemd；不要用 nohup 起）。"""
    settings = _settings()
    toolbox = Toolbox(settings)
    interval = max(60, settings.notify_interval)
    typer.secho(
        f"notify 轮询开始：间隔 {interval}s（±{JITTER}s），产物 {settings.approval_dir()}",
        fg=typer.colors.GREEN,
    )
    while True:
        try:
            with _single_writer(settings):
                result = _notify_round(settings, toolbox)
            typer.echo(_describe(settings, result))
        except AlreadyRunning as exc:
            typer.secho(str(exc), fg=typer.colors.YELLOW)
        except NotifyBlocked as exc:
            # 登录态失效/被拦**不是故障**：记一行，等下一轮。别刷屏、别退出。
            hint = {
                "waf_blocked": "（学校按出口 IP 拦，见 docs/deploy.md §0）",
                "not_logged_in": "（等扫码）",
            }.get(exc.kind, "")
            typer.secho(f"跳过这一轮：{exc}{hint}", fg=typer.colors.YELLOW)
        except Exception as exc:  # noqa: BLE001 - 常驻进程不能因为一轮出错就死
            typer.secho(f"这一轮出错：{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        time.sleep(interval + random.uniform(-JITTER, JITTER))


@app.command("notify-show")
def notify_show_cmd() -> None:
    """打印账本概要（不碰学校、不写文件）。"""
    settings = _settings()
    ledger = notify.read_ledger(settings.approval_dir())
    if not ledger:
        typer.echo(f"账本还是空的：{settings.approval_dir() / 'ledger.jsonl'}")
        return
    counts: dict[str, int] = {}
    for entry in ledger:
        key = str(entry.get("outcome") or "?")
        counts[key] = counts.get(key, 0) + 1
    label = {"approved": "已通过", "rejected": "已退回"}
    for key, value in sorted(counts.items()):
        typer.echo(f"  {label.get(key, key):6s} {value} 条")
    typer.echo()
    for entry in ledger:
        snapshot = entry.get("snapshot") or {}
        rooms = "、".join(entry.get("rooms") or [])
        tail = f" 教室 {rooms}" if rooms else ""
        typer.echo(
            f"  {entry.get('first_seen_ended', '')[:16]}  {label.get(entry.get('outcome'), entry.get('outcome'))}"
            f"  {notify.normalize_title(notify.text_of(snapshot, 'purpose'))}{tail}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
