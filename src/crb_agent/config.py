"""配置与凭证解析。

约定：

* 凭证只从**环境变量**或**已有的凭证文件**读；本模块永不写凭证、永不打印凭证；
* 访问密钥 ``CRBA_KEY`` **没有默认值**——没配就拒绝启动（fail closed）。
  这不是「进来看看」的公开页面：它能提交教室借用申请、能花掉 LLM 的 token；
* LLM key 优先级：``CRBA_LLM_KEY`` > ``DEEPSEEK_API_KEY``——后者复用
  ``yqa`` 已经放在 ``~/.yuque/agent.env`` 里的那一份，不新增凭证。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

DEFAULT_PORT = 8788
"""8787 是 ``yqa serve-plan`` 的下载口（公开、无鉴权），别再占它。"""

DEFAULT_NOTIFY_INTERVAL = 600
"""审批结果轮询间隔（秒）。审批是老师手动点的，分钟级足够；学校接口又有风控，
没必要更密。"""


class ConfigError(RuntimeError):
    """配置缺失/不合法——比任何网络错误都更早一步。"""


def _first(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def crb_auth_file() -> Path:
    """crb 的登录态落点。

    与 ``crb`` 自己的 ``config.state_path()`` 保持一致：
    认 ``CRB_AUTH_FILE``，否则 ``~/.crb/auth.json``。写错地方就等于没登录。
    """
    return _env_path("CRB_AUTH_FILE", Path.home() / ".crb" / "auth.json")


def crb_profile_file() -> Path:
    return _env_path("CRB_PROFILE_FILE", Path.home() / ".crb" / "profile.json")


@dataclass(frozen=True)
class Settings:
    # ---- 访问控制 ----
    access_key: str
    port: int
    host: str

    # ---- LLM ----
    llm_key: str
    llm_base_url: str
    llm_model: str

    # ---- 工具链 ----
    crb_bin: str
    yqa_bin: str
    yqa_repo: str

    # ---- 文件系统 ----
    workspace: Path
    """本项目的会话留痕目录（每一个会话一个 jsonl）。"""

    yuque_workspace: Path
    """``yqa`` 的工作区。agent 只从这里**读**产物（plan.json / applications / notify）。"""

    outbox_override: Path | None = None
    """直接指定 outbox 目录（``CRBA_OUTBOX``）。留空就按 ``yqa`` 的目录约定推。"""

    notify_interval: int = DEFAULT_NOTIFY_INTERVAL
    """审批结果轮询间隔（秒）。审批是老师手动点的，分钟级足够；学校接口又有风控。"""

    @property
    def crb_auth_file(self) -> Path:
        return crb_auth_file()

    @property
    def repo_slug(self) -> str:
        """``YQA_REPO`` 在**工作区里的目录名**。

        上游 ``yqa`` 的约定是 ``self.repo.replace("/", "_")``（见它的
        ``config.Settings.slug``），所以 ``ghxd00/jsjysq`` → ``ghxd00_jsjysq``。
        ⚠️ 不是取末段：取末段会得到 ``jsjysq``，那个目录根本不存在 ——
        而失败的表现是「产出目录不存在」，看起来像语雀侧还没干活。
        （实测踩到过；测试夹具当时用的是不含斜杠的 repo，把这个 bug 盖住了。）
        """
        return self.yqa_repo.replace("/", "_").strip()

    def outbox(self) -> Path:
        """``yqa`` 的产出目录：``<yuque_workspace>/<repo slug>/outbox``。

        推不出来就**明确报错**，不要悄悄退化成 ``<workspace>/outbox`` ——
        那会指向一个空目录，让「本周没有申请」和「你路径配错了」看起来一模一样，
        而这两种情况的处理方式完全相反（一种是等，一种是改配置）。
        """
        if self.outbox_override is not None:
            return self.outbox_override
        if not self.repo_slug:
            raise ConfigError(
                "推不出语雀产出目录：没有配置知识库。\n"
                "设 YQA_REPO=<group>/<repo>（就是语雀地址里那两段），"
                "或用 CRBA_OUTBOX 直接指向 outbox 目录。"
            )
        return self.yuque_workspace / self.repo_slug / "outbox"

    def approval_dir(self) -> Path:
        """审批结果的产物目录（账本 / 对外通知 / unmatched 都在这儿）。"""
        return self.outbox() / "approval"

    @classmethod
    def from_env(cls) -> Settings:
        key = os.environ.get("CRBA_KEY", "").strip()
        if not key:
            raise ConfigError(
                "没有配置访问密钥：请设置 CRBA_KEY（见 docs/deploy.md §3）。\n"
                "这个页面能提交借用申请，不能裸奔——没配密钥就拒绝启动。"
            )
        llm_key = _first(os.environ.get("CRBA_LLM_KEY"), os.environ.get("DEEPSEEK_API_KEY"))
        if not llm_key:
            raise ConfigError(
                "没有配置 LLM key：请设置 CRBA_LLM_KEY 或 DEEPSEEK_API_KEY。\n"
                "服务器上这一份就在 /home/yuque/.yuque/agent.env 里，systemd 用 EnvironmentFile 引进来。"
            )
        return cls(
            access_key=key,
            port=int(_first(os.environ.get("CRBA_PORT")) or DEFAULT_PORT),
            host=_first(os.environ.get("CRBA_HOST")) or "0.0.0.0",
            llm_key=llm_key,
            llm_base_url=_first(os.environ.get("CRBA_LLM_BASE"), DEFAULT_API_BASE).rstrip("/"),
            llm_model=_first(os.environ.get("CRBA_LLM_MODEL"), DEFAULT_MODEL),
            crb_bin=_first(os.environ.get("CRBA_CRB_BIN"), "crb"),
            yqa_bin=_first(os.environ.get("CRBA_YQA_BIN"), "yqa"),
            yqa_repo=_first(os.environ.get("YQA_REPO")),
            notify_interval=int(
                _first(os.environ.get("CRBA_NOTIFY_INTERVAL")) or DEFAULT_NOTIFY_INTERVAL
            ),
            workspace=_env_path("CRBA_WORKSPACE", Path.cwd() / "workspace"),
            yuque_workspace=_env_path(
                "CRBA_YUQUE_WORKSPACE", Path("/var/lib/yuque-agent/workspace")
            ),
            outbox_override=(
                _env_path("CRBA_OUTBOX", Path())
                if os.environ.get("CRBA_OUTBOX", "").strip()
                else None
            ),
        )


@dataclass
class SessionState:
    """一次会话的运行期状态（持久化在 workspace/<id>.jsonl）。"""

    id: str
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    messages: list[dict] = field(default_factory=list)
