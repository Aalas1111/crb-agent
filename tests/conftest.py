"""测试共用的夹具。

两条规矩（都是拿事故换来的，见 AGENTS.md）：

1. **测试绝不碰真实凭证。** ``HOME`` / ``USERPROFILE`` 在 :func:`_isolated_home`
   里被指到临时目录，于是 ``~/.crb/auth.json`` 这类默认路径落在沙箱里，
   测试就算误调也写不到真东西上。
2. **测试绝不联网。** ``crb`` / ``yqa`` 由 ``tests/fake_cli.py`` 顶替，
   authserver 的响应由 ``httpx.MockTransport`` 顶替。

假 ``crb`` 的行为由环境变量 ``FAKE_CRB_MODE`` 决定（``ok`` / ``noauth`` /
``waf``），收到的参数追加进 ``FAKE_CRB_ARGS_FILE``，用来验证命令行没拼错。
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from crb_agent.config import Settings

FAKE_CLI = Path(__file__).parent / "fake_cli.py"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """把 HOME 关进临时目录——测试里任何「默认路径」都落在沙箱里。"""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CRB_AUTH_FILE", raising=False)
    monkeypatch.delenv("CRB_PROFILE_FILE", raising=False)
    monkeypatch.setenv("FAKE_CRB_MODE", "ok")
    monkeypatch.delenv("FAKE_CRB_ARGS_FILE", raising=False)
    monkeypatch.delenv("FAKE_YQA_FAIL", raising=False)
    return home


def _fake(binary: str) -> str:
    """把 ``fake_cli.py`` 拼成一条能被 shlex 还原的命令。"""
    return shlex.join([sys.executable, str(FAKE_CLI), binary])


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "crba"
    root.mkdir()
    return root


@pytest.fixture
def yuque_workspace(tmp_path: Path) -> Path:
    """造一个像 ``yqa`` 那样的产出目录（含一个 plan.json）。

    目录名必须用 ``ghxd00_jsjysq`` 这种**下划线**形式，而 ``YQA_REPO`` 是
    ``ghxd00/jsjysq``（斜杠）—— 这正是生产上的真实形状。
    早先夹具用不带斜杠的 repo，把「按末段推目录名」的 bug 盖住了。
    """
    outbox = tmp_path / "yq" / "ghxd00_jsjysq" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "plan.json").write_text(
        json.dumps(
            {
                "cycle": "0927-1003",
                "generated_at": "2026-09-27T10:18:40+08:00",
                "defaults": {
                    "JYDWDM": "400760",
                    "JYRXM": "张三",
                    "JYRDH": "13800000000",
                    "JSJYLXDM": "02",
                },
                "activities": [
                    {
                        "title": "新生见面会",
                        "date": "2026-10-01",
                        "period": "7-8",
                        "people": 25,
                        "campus": "3",
                        "_application_id": "2026-10-01-1234567",
                        "_doc_id": 1234567,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp_path / "yq"


@pytest.fixture
def settings(workspace: Path, yuque_workspace: Path) -> Settings:
    return Settings(
        access_key="test-key-123456",
        port=8788,
        host="127.0.0.1",
        llm_key="fake-llm-key-for-tests",
        llm_base_url="https://api.example.invalid",
        llm_model="test-model",
        crb_bin=_fake("crb"),
        yqa_bin=_fake("yqa"),
        yqa_repo="ghxd00/jsjysq",
        workspace=workspace,
        yuque_workspace=yuque_workspace,
    )


@pytest.fixture
def crb_args(monkeypatch, tmp_path: Path):
    """记录假 crb 收到的参数，供断言命令行用。"""
    log = tmp_path / "crb-args.jsonl"
    monkeypatch.setenv("FAKE_CRB_ARGS_FILE", str(log))

    def read() -> list[list[str]]:
        if not log.is_file():
            return []
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    return read


def pytest_report_header(config) -> str:  # pragma: no cover - 只是让输出更好读
    return f"crb-agent tests (python {sys.version.split()[0]}, {os.name})"
