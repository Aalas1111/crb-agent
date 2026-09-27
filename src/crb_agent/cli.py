"""命令行入口。

crba serve      起服务（agent 界面 + SSE）
crba auth       在终端里走一次扫码登录（拿不到网页界面时用）
crba doctor     自检：密钥、LLM key、登录态、crb/yqa 能不能叫得动
crba version
"""

from __future__ import annotations

import sys
import time
from typing import Annotated

import typer

from . import __version__, njuqr, server
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
