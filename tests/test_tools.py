"""工具层：参数校验、路径沙箱、退出码翻译、命令行拼装。

这一层是**能力边界**，所以测试重点不是「好不好用」而是「拦不拦得住」：
LLM 传进来的每一个值都必须逐字段校验过才允许拼进命令行（而且永远没有 shell）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from crb_agent.tools import (
    Toolbox,
    ToolError,
    _extract_json,
    _require_choice,
    _require_date,
    _require_period,
    _require_sqbh,
    _resolve_within,
)

#: 真实的申请编号长这样（32 位十六进制，来自上游 handoff 的申请 JSON 例子）。
REAL_SQBH = "7e75e389f83d4aac95bdcc28b5521041"


# ---------------------------------------------------------------- 参数校验
def test_date_must_be_iso():
    assert _require_date({"date": "2026-10-01"}, "date") == "2026-10-01"
    for bad in ("2026/10/01", "10-01", "明天", "2026-1-1", ""):
        with pytest.raises(ToolError):
            _require_date({"date": bad}, "date")


def test_period_accepts_range_and_single():
    assert _require_period({"period": "1-2"}, "period") == "1-2"
    assert _require_period({"period": "7"}, "period") == "7"
    for bad in ("1~2", "第7节", "1-", "-2", "abc"):
        with pytest.raises(ToolError):
            _require_period({"period": bad}, "period")


def test_choice_is_whitelisted():
    assert _require_choice({"mode": "save"}, "mode", ("preview", "save", "submit")) == "save"
    with pytest.raises(ToolError):
        _require_choice({"mode": "delete"}, "mode", ("preview", "save", "submit"))


def test_sqbh_accepts_real_school_ids_and_rejects_sentences():
    """真实的 SQBH 是 32 位十六进制串（见上游 handoff 的申请 JSON 例子）。"""
    real = "7e75e389f83d4aac95bdcc28b5521041"
    assert _require_sqbh({"sqbh": real}) == real
    assert _require_sqbh({"sqbh": "12345678"}) == "12345678"
    assert _require_sqbh({"sqbh": 12345678}) == "12345678"
    for bad in ("", "撤回第7条", "12;rm -rf /", "--submit", "short", "a b", "带/斜杠的"):
        with pytest.raises(ToolError):
            _require_sqbh({"sqbh": bad})


# ---------------------------------------------------------------- 路径沙箱
def test_resolve_within_allows_nested_paths(tmp_path: Path):
    (tmp_path / "applications").mkdir()
    (tmp_path / "applications" / "a.json").write_text("{}", encoding="utf-8")
    assert _resolve_within(tmp_path, "applications/a.json").is_file()
    assert _resolve_within(tmp_path, ".") == tmp_path.resolve()


def test_resolve_within_blocks_traversal(tmp_path: Path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (tmp_path / "plan.defaults.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ToolError):
        _resolve_within(outbox, "../plan.defaults.json")
    with pytest.raises(ToolError):
        _resolve_within(outbox, "applications/../../plan.defaults.json")
    with pytest.raises(ToolError):
        _resolve_within(outbox, "")


def test_resolve_within_blocks_absolute_paths(tmp_path: Path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    with pytest.raises(ToolError):
        _resolve_within(outbox, str(tmp_path / "plan.defaults.json"))
    # Windows 上没有盘符的绝对路径也要挡住（/etc/passwd）
    with pytest.raises(ToolError):
        _resolve_within(outbox, "/etc/passwd")


# ---------------------------------------------------------------- 输出解析
def test_extract_json_skips_progress_output():
    """``crb plan`` 会先打一行「已有申请 3 条」，JSON 从后面才开始。"""
    noisy = '已有申请 3 条，已纳入防重合检测\n[{"status": "ok"}]\n'
    assert _extract_json(noisy) == [{"status": "ok"}]


def test_extract_json_handles_non_json_output():
    assert _extract_json("") is None
    assert _extract_json("登录态已失效，请重新运行 crb login。") is None


def test_extract_json_parses_plain_object():
    assert _extract_json('{"ok": true}') == {"ok": True}


# ---------------------------------------------------------------- 注册表
def test_manifest_is_openai_shaped_and_unique(settings):
    box = Toolbox(settings)
    manifest = box.manifest()
    names = [entry["function"]["name"] for entry in manifest]
    assert len(names) == len(set(names))
    for entry in manifest:
        assert entry["type"] == "function"
        assert entry["function"]["description"]
        assert entry["function"]["parameters"]["type"] == "object"


def test_manifest_description_carries_the_guidelines(settings):
    """用法契约必须在模型看得见的地方——只在提示词里写一遍是不够的。"""
    box = Toolbox(settings)
    plan = next(entry for entry in box.manifest() if entry["function"]["name"] == "crb_plan")
    description = plan["function"]["description"]
    assert "先 preview" in description
    assert "duplicate" in description


def test_write_tools_are_marked_dangerous(settings):
    box = Toolbox(settings)
    assert box.risk_of("crb_plan") == "dangerous"
    assert box.risk_of("crb_borrow_action") == "dangerous"
    assert box.risk_of("crb_free_rooms") == "read"
    assert box.risk_of("read_plan") == "read"


def test_unknown_tool_lists_what_exists(settings):
    box = Toolbox(settings)
    result = box.execute("nope", {})
    assert result.status == "error"
    assert "crb_status" in result.data["available"]


def test_broken_json_arguments_are_explained(settings):
    box = Toolbox(settings)
    result = box.execute("crb_free_rooms", {"__parse_error__": "参数不是合法 JSON", "__raw__": "{"})
    assert result.status == "error"
    assert "不是合法 JSON" in result.summary


# ---------------------------------------------------------------- 命令拼装
def test_free_room_command_arguments(settings, crb_args):
    result = Toolbox(settings).execute(
        "crb_free_rooms",
        {"campus": "3", "date": "2026-10-01", "period": "7-8", "building": "11"},
    )
    assert result.status == "ok"
    argv = crb_args()[-1]
    assert argv[:1] == ["crb"]
    assert argv[1:3] == ["free", "--campus"]
    assert "--building" in argv and "11" in argv
    assert argv[-1] == "--json"


def test_plan_preview_does_not_write(settings, crb_args):
    result = Toolbox(settings).execute("crb_plan", {"mode": "preview"})
    assert result.status == "ok"
    argv = crb_args()[-1]
    assert "--save" not in argv and "--submit" not in argv
    assert "--json" in argv
    assert "--file" in argv


def test_plan_save_and_submit_flags(settings, crb_args):
    Toolbox(settings).execute("crb_plan", {"mode": "save"})
    assert "--save" in crb_args()[-1]
    Toolbox(settings).execute("crb_plan", {"mode": "submit"})
    argv = crb_args()[-1]
    assert "--submit" in argv and "--save" not in argv


def test_plan_duplicates_are_reported_as_such(settings):
    """去重结果要原样交给 LLM 去解释——它在提示词里被告知了这个语义。"""
    result = Toolbox(settings).execute("crb_plan", {"mode": "preview"})
    statuses = [a["status"] for a in result.data["assignments"]]
    assert "duplicate" in statuses
    assert result.data["status_legend"]["duplicate"].startswith("与已有申请时间重叠")
    assert "duplicate" not in result.summary  # 人话里不该冒英文状态码


def test_no_login_is_reported_as_such(settings, monkeypatch):
    monkeypatch.setenv("FAKE_CRB_MODE", "noauth")
    result = Toolbox(settings).execute("crb_status", {})
    assert result.status == "error"
    assert "登录" in result.summary


def test_missing_binary_gives_a_readable_error(settings):
    broken = replace(settings, crb_bin="definitely-not-a-real-binary")
    result = Toolbox(broken).execute("crb_status", {})
    assert result.status == "error"
    assert "找不到命令" in result.summary


def test_borrow_action_requires_edit_data(settings):
    result = Toolbox(settings).execute("crb_borrow_action", {"sqbh": REAL_SQBH, "action": "edit"})
    assert result.status == "error"
    assert "data" in result.summary


def test_borrow_action_rejects_bad_edit_data(settings):
    result = Toolbox(settings).execute(
        "crb_borrow_action", {"sqbh": REAL_SQBH, "action": "edit", "data": "[1,2]"}
    )
    assert result.status == "error"


def test_borrow_action_edit_arguments(settings, crb_args):
    Toolbox(settings).execute(
        "crb_borrow_action",
        {"sqbh": REAL_SQBH, "action": "edit", "data": '{"ZRS":"35"}', "draft": True},
    )
    argv = crb_args()[-1]
    assert argv[:3] == ["crb", "borrow", "edit"]
    assert "--sqbh" in argv and REAL_SQBH in argv
    assert "--draft" in argv


# ---------------------------------------------------------------- 语雀侧
def test_read_plan_returns_activities(settings):
    result = Toolbox(settings).execute("read_plan", {})
    assert result.status == "ok"
    assert "0927-1003" in result.summary
    assert result.data["activities"][0]["title"] == "新生见面会"


def test_read_plan_reports_missing_file(settings):
    result = Toolbox(settings).execute("read_plan", {"path": "applications/nope.json"})
    assert result.status == "error"
    assert "不存在" in result.summary
    assert "还没" in result.summary  # 讲清是「这一周期还没有申请」


def test_missing_outbox_is_reported_as_a_config_problem(settings):
    """「路径配错了」和「本周没有申请」必须分得开：一个要改配置，一个要等。"""
    broken = replace(settings, yqa_repo="", outbox_override=None)
    result = Toolbox(broken).execute("read_plan", {})
    assert result.status == "error"
    assert "配置" in result.summary
    assert "YQA_REPO" in result.summary


def test_read_plan_cannot_escape_the_outbox(settings):
    """`plan.defaults.json` 里是借用人姓名与手机号，绝不能被 LLM 顺手读出去。"""
    result = Toolbox(settings).execute("read_plan", {"path": "../../../etc/passwd"})
    assert result.status == "error"
    assert "越界" in result.summary


def test_list_outbox(settings):
    result = Toolbox(settings).execute("list_outbox", {})
    assert result.status == "ok"
    assert any(entry["name"] == "plan.json" for entry in result.data["entries"])


def test_yqa_refresh_plan_passes_workspace_and_repo(settings, crb_args):
    result = Toolbox(settings).execute("yqa_refresh_plan", {})
    assert result.status == "ok"
    argv = crb_args()[-1]
    assert argv[0] == "yqa"
    assert argv[1] == "export-plan"
    assert "--workspace" in argv
    assert "--repo" in argv and "ghxd00_jsjysq" in argv


def test_yqa_refresh_plan_rejects_bad_defaults(settings):
    result = Toolbox(settings).execute("yqa_refresh_plan", {"defaults": "not json"})
    assert result.status == "error"
    assert "defaults" in result.summary


def test_yqa_failure_is_reported(settings, monkeypatch):
    monkeypatch.setenv("FAKE_YQA_FAIL", "1")
    result = Toolbox(settings).execute("yqa_refresh_plan", {})
    assert result.status == "error"
    assert "刷新清单失败" in result.summary
