"""守卫测试：盯住 systemd 单元、部署文档、脚本三者不漂。

`AGENTS.md` §5 要求「碰了 `deploy/*.service` 就同步 `docs/deploy.md`」——
靠人记着一定会忘，所以钉成断言。这里**不检查运行时行为**，只检查
那些「写错了就会在半夜出问题」的事实：端口、唯一写者、凭证路径、fail-closed。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = (ROOT / "deploy" / "crb-agent.service").read_text(encoding="utf-8")
DEPLOY_DOC = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
DEPLOY_SH = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
SYNC_SH = (ROOT / "scripts" / "sync-server.sh").read_text(encoding="utf-8")


def test_unit_uses_8788_and_never_steals_8787():
    """8787 是 yuque-agent-plan 的下载口。占它会让那边的取件口失效。"""
    assert "CRBA_PORT=8788" in UNIT
    # 只允许在**注释里**提 8787（提醒后来人别占），不许真去设它。
    for line in UNIT.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("Description="):
            continue
        assert "8787" not in line, f"单元里真的设了 8787：{line}"
    assert "8787" in UNIT  # 那句提醒还在（挪走了就补回来）


def test_deploy_doc_documents_the_port_and_the_unit():
    assert "8788" in DEPLOY_DOC
    assert "crb-agent.service" in DEPLOY_DOC


def test_unit_restarts_on_failure():
    """常驻服务必须自愈——没人会半夜上去手工重启它。"""
    assert "Restart=always" in UNIT
    # systemctl stop 时 Python 以 143 退出，别把它当故障
    assert "SuccessExitStatus=143" in UNIT


def test_unit_has_both_environment_files():
    assert "EnvironmentFile=/home/yuque/.yuque/agent.env" in UNIT  # 复用 LLM key
    assert "EnvironmentFile=-/home/yuque/.crb-agent/env" in UNIT  # 本项目自己的


def test_unit_pins_a_sane_path():
    """crb / yqa 是 uv tool 装的，落在 ~/.local/bin —— 不能靠 systemd 的默认 PATH。"""
    assert "Environment=PATH=" in UNIT
    assert "/home/yuque/.local/bin" in UNIT


def test_unit_keeps_the_workspace_path_in_sync_with_the_deploy_doc():
    workspace = re.search(r"Environment=CRBA_WORKSPACE=(\S+)", UNIT)
    assert workspace is not None
    assert workspace.group(1) in DEPLOY_DOC


def test_unit_does_not_hardcode_the_school_repo_slug():
    """语雀知识库名属于部署配置，不该写死在仓库里的单元里（会跟 yqa 漂）。"""
    assert "CRBA_OUTBOX=" not in UNIT
    assert "ghxd00" not in UNIT


def test_deploy_sh_takes_a_lock_and_only_fast_forwards():
    """一次只能有一个写者；生产机禁止 rebase / 手工 merge。"""
    assert "flock -n" in DEPLOY_SH
    assert "--ff-only" in DEPLOY_SH


def test_deploy_sh_runs_tests_in_a_sandbox_home():
    """事故：测试碰到默认路径下的真凭证 = 重扫一次码。"""
    assert 'HOME="$SANDBOX"' in DEPLOY_SH or "HOME=$SANDBOX" in DEPLOY_SH


def test_deploy_sh_verifies_the_key_gate_actually_blocks():
    """「进程活着」不等于「门在拦」——验收必须验 403。"""
    assert "403" in DEPLOY_SH
    assert "/agent" in DEPLOY_SH


def test_deploy_sh_writes_an_ops_log():
    assert "ops.log" in DEPLOY_SH


def test_unit_quotes_environment_values_that_contain_spaces():
    """systemd 在 ``Environment=`` 里把空格当**多个赋值**的分隔符。

    所以 `Environment=VAR=uv run ...` 会被拆成 `VAR=uv` 加一串
    `Invalid environment assignment, ignoring: run/...` —— 告警刷屏，
    而那个变量根本没设上。首次部署时实测踩到这个：`CRBA_YQA_BIN` 丢了，
    agent 一调 yqa 就报「找不到命令」。值里有空格就必须用引号包住整个赋值。
    """
    for line in UNIT.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        value = stripped[len("Environment=") :].strip()
        if value.startswith('"') and value.endswith('"'):
            continue
        assert " " not in value, (
            f"`{stripped}` 的值里有空格但没加引号 —— systemd 会把它当成多个赋值。"
            f'改成 Environment="…" 的形式。'
        )


def test_readonly_paths_tolerate_paths_that_appear_later():
    """外部项目的路径要带 ``-`` 前缀（不存在就跳过）。

    `~/.crb/` 在第一次扫码之前不存在，而 ReadOnlyPaths 遇到不存在的路径会让
    systemd 建命名空间失败（226/NAMESPACE），服务根本起不来 —— 那三条路径都
    归别的项目管，不该因为它们的出现时序把我们的服务挡在门外。
    """
    readonly = [
        line.strip() for line in UNIT.splitlines() if line.strip().startswith("ReadOnlyPaths=")
    ]
    assert readonly, "单元里应该有 ReadOnlyPaths（收紧文件系统视野）"
    for line in readonly:
        for path in line[len("ReadOnlyPaths=") :].split():
            assert path.startswith("-") or path.startswith("/var/lib/crb-agent"), (
                f"{path} 是别的项目管的路径，前面要加 `-`（不存在就跳过）"
            )


def test_deploy_sh_creates_the_uv_cache_dir():
    """单元把 UV_CACHE_DIR 指到 state 目录，deploy.sh 得先把它建出来。"""
    assert ".uv-cache" in DEPLOY_SH
    assert ".uv-cache" in UNIT


def _code_lines(text: str) -> str:
    """去掉注释与空行，只留会被执行的部分。

    守卫测试要盯的是**行为**，不是解释。注释里常常要写出被否决的做法
    （「不要改走 git push ssh://…」），那不是违规。
    """
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


def test_sync_server_has_an_offline_transfer_fallback():
    """传输 commit 不能**只**依赖生产机出网。

    实测：这台生产机到 GitHub 的 **HTTPS 被阻断**（`git fetch` 真实退出码
    124 = 超时），而 SSH 是通的。所以「让生产机自己 fetch」会时不时不成立；
    必须有 bundle + scp 这条不依赖它出网、也不依赖它在检出里写权限的路。
    """
    code = _code_lines(SYNC_SH)
    assert "git bundle create" in code
    assert "scp" in code
    # 这条路在当前权限模型下不成立：检出归 yuque，ssh 进来的是 lihe。
    assert "git push ssh://" not in code


def test_deploy_sh_chowns_the_state_dir_to_the_service_user():
    """以 root 建的 state 目录不 chown，服务就写不进去。

    实测：服务每 15s 重启一次，日志是
    `PermissionError: [Errno 13] Permission denied: /var/lib/crb-agent/workspace`。
    """
    assert "chown yuque:yuque" in DEPLOY_SH


def test_deploy_sh_survives_a_nonzero_is_active():
    """`systemctl is-active` 在服务没起来时返回非 0，而它在 `$(…)` 里赋值 ——
    裸着写会被 `set -e` 直接杀掉脚本，验收结果一行都打不出来（实测：退出码 3）。"""
    assert 'systemctl is-active "$UNIT" || true' in DEPLOY_SH


def test_git_runs_as_the_repo_owner():
    """服务器上的 git 必须以**仓库所有者（yuque）**的身份跑。

    `deploy.sh` 是 root 执行的，但检出归 yuque、部署密钥也在 `/home/yuque/.ssh`。
    root 直接跑 git 会先撞 dubious ownership，再撞 `Host key verification failed`
    —— 于是 `fetch` 失败、脚本退回「按当前 HEAD 继续」，**部署的其实是旧 commit**。
    实测踩到过：测试跑的是旧代码、装的是旧单元，而脚本一路说「完成」。
    """
    for script, name in ((DEPLOY_SH, "deploy.sh"), (SYNC_SH, "sync-server.sh")):
        for line in _code_lines(script).splitlines():
            if "git -C" not in line:
                continue
            assert "sudo -u yuque" in line, f"{name} 里有不以 yuque 身份跑的 git：{line.strip()}"


def test_deploy_sh_refuses_to_run_on_a_dirty_tree_before_anything_else():
    """干净检查必须在**动任何东西之前**——否则半路失败会留下不一致的状态。"""
    code = _code_lines(DEPLOY_SH)
    dirty_pos = code.find("status --porcelain")
    unit_pos = code.find("install -m 644")
    assert 0 < dirty_pos < unit_pos


def test_sync_server_refuses_to_run_on_the_production_checkout():
    assert "/opt/crb-agent" in SYNC_SH
    assert "生产机的检出" in SYNC_SH


def test_sync_server_does_not_hardcode_a_host():
    """服务器地址不进仓库。"""
    assert "CRBA_SERVER" in SYNC_SH
    for pattern in (r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", r"@[\w.-]+\.(com|cn|net)"):
        assert not re.search(pattern, SYNC_SH), f"sync-server.sh 里出现了疑似地址：{pattern}"


def test_deploy_doc_has_the_required_exposure_writeup():
    """AGENTS.md §2.2：绑 0.0.0.0 必须写清暴露什么 / 靠什么鉴权 / 为什么接受。"""
    for phrase in ("暴露面", "密钥", "0.0.0.0", "残余风险"):
        assert phrase in DEPLOY_DOC


def test_deploy_doc_admits_the_plan_pii():
    """与 agent 的对话里会出现 plan.json 带来的姓名与手机号——别装作没有。"""
    assert "手机号" in DEPLOY_DOC


def test_deploy_doc_forbids_running_tests_on_the_production_box():
    assert "不要这样" in DEPLOY_DOC
    assert "临时目录" in DEPLOY_DOC


#: 不是「地址」的四种字面量，所以不算违规：
#: 前三个是绑定规则里的回环与不限（文档必须能写它们）；
#: 最后一个是浏览器 User-Agent 里的版本号（``Chrome/136.0.0.0``），
#: 形状和 IP 一样。往这里加东西要停顿一下——这份名单是**刻意维护**的例外。
_NOT_AN_ADDRESS = {
    "127.0.0.1",
    "0.0.0.0",
    "255.255.255.255",
    "136.0.0.0",
}


def test_repo_does_not_contain_an_address_or_a_secret():
    """仓库里不许出现服务器地址与密钥（靠人来守不如靠一句话的断言）。"""
    checked = [p for p in ROOT.rglob("*") if p.is_file() and ".venv" not in p.parts]
    for path in checked:
        # 跳过本文件：它自己就写着这两个「要找的东西」，否则会自己举报自己。
        if path == Path(__file__).resolve():
            continue
        if path.suffix not in {".md", ".py", ".sh", ".service", ".toml", ".js", ".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)
        bad = [ip for ip in found if ip not in _NOT_AN_ADDRESS]
        assert not bad, f"{path} 里有 IP：{bad}"
        assert "sk-" not in text, f"{path} 里疑似有 LLM key"
