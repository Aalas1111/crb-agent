"""守卫测试：盯住「契约与提示词漂没漂」。

照上游 ``yuque-agent`` 的 ``docs/principles.md`` §5：守卫测试**不动运行时行为**，
只检查提示词里那几条拿事故换来的规矩还在不在。它们不保证 LLM 一定守规矩
（那要测的是 LLM），但能保证**我们没把规矩删掉**。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from crb_agent.prompts import prompt_path, system_prompt


def test_prompt_forbids_submitting_without_explicit_consent():
    """正式提交会真的占掉教室，必须等用户明确要求。"""
    text = system_prompt()
    assert "submit" in text
    assert "明确" in text
    # 默认只到草稿这条也得在
    assert "默认只存草稿" in text


def test_prompt_explains_that_duplicate_is_not_a_failure():
    """``duplicate`` 是「已经交过了」，不是错误——这个语义必须写明。

    不写清楚，模型就会把它当成失败去「修复」（比如加 ``allow_overlap``），
    结果同一条申请在学校系统里出现两次。
    """
    text = system_prompt()
    assert "duplicate" in text
    assert "已经交过" in text
    assert "allow_overlap" in text
    # 并且明确禁止加它
    assert "不要" in text


def test_prompt_tells_the_model_to_check_the_cycle():
    """plan.json 会包含上周那份，cycle 是唯一一眼能看出来的标记。"""
    assert "cycle" in system_prompt()


def test_prompt_forbids_inventing_facts():
    assert "不要自己编" in system_prompt() or "绝对不要自己编" in system_prompt()


def test_prompt_forbids_claiming_completion():
    """只知道提交成功，不知道老师批不批——别声称「已办结」。"""
    text = system_prompt()
    assert "已办结" in text


def test_prompt_carries_the_dictionaries_the_model_needs():
    """省上下文不能拿「模型本来就该知道」当借口：校区码、节次、状态码都要给。"""
    text = system_prompt()
    assert "仙林" in text and "鼓楼" in text
    assert "08:00-08:50" in text  # 节次时间表
    assert "SHZT" in text
    assert "学生社团管理部" in text


def test_prompt_has_todays_date_in_beijing_time():
    """日期相关的判断几乎每条都要用到「今天几号」，模型自己没法知道。"""
    fixed = datetime(2026, 10, 1, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    text = system_prompt(fixed)
    assert "2026-10-01" in text
    assert "周四" in text


def test_prompt_file_exists_and_is_not_empty():
    path = prompt_path("system.md")
    assert path.is_file()
    assert len(path.read_text(encoding="utf-8").strip()) > 500


def test_prompt_has_no_placeholders():
    text = system_prompt()
    for marker in ("TODO", "TBD", "XXX", "{{", "}}"):
        assert marker not in text
