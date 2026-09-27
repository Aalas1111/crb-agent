"""审批结果：判定、账本、关联、对外文档。

夹具里的那条记录是**真实样本**（负责人 2026-09-27 转来的「审核不通过」记录，
只把借用人姓名与手机号留着 —— 它本来就是给我们看字段形状的）。
不要把它改成「看起来更干净」的假数据：`SHZT=-68` / `SHBZ=不通过` /
`SHYJ=null` 这些恰恰是判定规则要面对的东西。
"""

from __future__ import annotations

import json
from pathlib import Path

from crb_agent import notify

#: 真实的「审核不通过」记录（字段名与取值都未改动）。
REJECTED = {
    "SQBH": "6ded436268ee478cb4b03e3f0b9d2788",
    "SHZT": "-68",
    "SHZT_DISPLAY": "学生工作处审核不通过",
    "SHBZ": "不通过",
    "SHYJ": None,
    "FJ": None,
    "JASMC": None,
    "KSJC_DISPLAY": "第4节(11:10-12:00)",
    "JSJC_DISPLAY": "第4节(11:10-12:00)",
    "KSRQ": "2026-09-10",
    "JYRXM": "谷和平",
    "JYDWDM_DISPLAY": "电子科学与工程学院",
    "JYYTMS": "GHP",
    "JASJYLXDM_DISPLAY": "学生工作处",
    "XXXQDM_DISPLAY": "仙林校区",
    "SQRQ": "2026-09-08 15:17:23.0",
    "KSJC": "4",
    "JSJC": "4",
    "ZRS": "10",
}

#: 一条「已通过」记录。**这是我按样本的形状推的**（样本里没有通过态）——
#: 真实取值要等现实里出现第一条通过，届时用账本里的 snapshot 校准。
APPROVED = {
    **REJECTED,
    "SQBH": "7e75e389f83d4aac95bdcc28b5521041",
    "SHZT": "99",
    "SHZT_DISPLAY": "已通过",
    "SHBZ": "通过",
    "SHYJ": None,
    "FJ": "新教404",
    "JASMC": "新教404",
    "JYYTMS": "GHP（意向：新教404）",
}


def _plan(tmp_path: Path, activities: list[dict], cycle: str = "0905-0911") -> Path:
    outbox = tmp_path / "yq" / "ghxd00_jsjysq" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / "plan.json").write_text(
        json.dumps({"cycle": cycle, "defaults": {}, "activities": activities}, ensure_ascii=False),
        encoding="utf-8",
    )
    return outbox


# ---------------------------------------------------------------- 判定
def test_rejected_sample_is_recognised():
    """真实样本里「学生工作处审核不通过」同时含「审核」（像进行中）与「不通过」——
    必须先判否定词，否则会被当成「还在流程里」。
    """
    verdict = notify.classify(REJECTED)
    assert verdict.outcome == notify.OUTCOME_REJECTED
    assert verdict.ended
    assert "不通过" in verdict.reason


def test_approved_needs_a_room():
    verdict = notify.classify(APPROVED)
    assert verdict.outcome == notify.OUTCOME_APPROVED
    assert verdict.rooms == ["新教404"]


def test_approved_without_a_room_is_not_silently_announced():
    """负责人说这种情况「应该不存在」；真出现了就让人看一眼，
    而不是发一条没有教室的「已通过」。"""
    blind = {**APPROVED, "FJ": None, "JASMC": None}
    verdict = notify.classify(blind)
    assert verdict.outcome == notify.OUTCOME_UNMATCHED
    assert not verdict.ended


def test_draft_and_pending_do_not_end():
    assert notify.classify(
        {**REJECTED, "SHZT": "00", "SHBZ": "", "SHZT_DISPLAY": "草稿"}
    ).outcome == (notify.OUTCOME_DRAFT)
    assert (
        notify.classify({**REJECTED, "SHZT": "65", "SHBZ": "", "SHZT_DISPLAY": "待审核"}).outcome
        == notify.OUTCOME_PENDING
    )
    assert notify.classify(
        {**REJECTED, "SHZT": "1", "SHBZ": "", "SHZT_DISPLAY": "已撤回"}
    ).outcome == (notify.OUTCOME_REJECTED)


def test_unknown_status_goes_to_unmatched_not_guessed():
    verdict = notify.classify({**REJECTED, "SHZT": "??", "SHBZ": "", "SHZT_DISPLAY": "某种新状态"})
    assert verdict.outcome == notify.OUTCOME_UNMATCHED
    assert not verdict.ended


# ---------------------------------------------------------------- notificationId
def test_notification_id_is_stable_and_has_no_timestamp():
    """含时间戳就每轮都变，下游按 id 去重立刻失效 —— 这是最容易犯的错。"""
    first = notify.notification_id("sqbh1", "approved", ["A"])
    assert first == notify.notification_id("sqbh1", "approved", ["A"])
    assert first.startswith("notify-")
    assert len(first) == len("notify-") + 8
    # 教室变了 = 新消息
    assert first != notify.notification_id("sqbh1", "approved", ["B"])
    # 结果变了 = 新消息
    assert first != notify.notification_id("sqbh1", "rejected", ["A"])


# ---------------------------------------------------------------- 账本
def test_ledger_append_is_idempotent(tmp_path: Path):
    root = tmp_path / "approval"
    assert notify.new_entries([REJECTED], []) != []
    notify.append_ledger(root, notify.new_entries([REJECTED], []))

    ledger = notify.read_ledger(root)
    assert len(ledger) == 1
    assert ledger[0]["sqbh"] == REJECTED["SQBH"]
    assert ledger[0]["snapshot"]["SHZT"] == "-68"  # 原始记录原样留着
    # 再跑一遍：没有新条目
    assert notify.new_entries([REJECTED], ledger) == []


def test_ledger_skips_half_written_lines(tmp_path: Path):
    root = tmp_path / "approval"
    notify.append_ledger(root, notify.new_entries([REJECTED], []))
    with (root / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"sqbh": "half')
    assert len(notify.read_ledger(root)) == 1


# ---------------------------------------------------------------- 关联
def test_match_strips_the_intent_suffix():
    assert notify.normalize_title("GHP（意向：新教404）") == "GHP"
    assert notify.normalize_title("GHP") == "GHP"


def test_period_extraction_from_display_text():
    assert notify.period_of_slot_text("第4节(11:10-12:00)") == (4, 4)
    assert notify.period_of_slot_text("第5节(14:00-14:50)") == (5, 5)
    assert notify.period_of_slot_text("") is None


def test_sources_come_from_plan_and_archive(tmp_path: Path):
    outbox = _plan(
        tmp_path,
        [
            {
                "title": "GHP",
                "date": "2026-09-10",
                "period": "4",
                "_application_id": "a-1",
                "_doc_id": 11,
            }
        ],
    )
    archived = outbox / "archive" / "0829-0904"
    archived.mkdir(parents=True)
    (archived / "plan.json").write_text(
        json.dumps(
            {
                "cycle": "0829-0904",
                "activities": [
                    {
                        "title": "旧活动",
                        "date": "2026-09-02",
                        "period": "1-2",
                        "_application_id": "a-0",
                        "_doc_id": 10,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    sources = notify.load_sources(outbox)
    assert {s["application_id"] for s in sources} == {"a-1", "a-0"}
    assert {s["cycle"] for s in sources} == {"0905-0911", "0829-0904"}


def test_record_matches_activity_by_date_period_title(tmp_path: Path):
    outbox = _plan(
        tmp_path,
        [
            {
                "title": "GHP",
                "date": "2026-09-10",
                "period": "4",
                "_application_id": "2026-09-10-1234567",
                "_doc_id": 1234567,
            },
            {"title": "别的活动", "date": "2026-09-10", "period": "4", "_application_id": "other"},
        ],
    )
    source = notify.match_source(REJECTED, notify.load_sources(outbox))
    assert source is not None
    assert source["application_id"] == "2026-09-10-1234567"
    assert source["doc_id"] == 1234567


def test_no_match_returns_none(tmp_path: Path):
    outbox = _plan(tmp_path, [{"title": "别的", "date": "2026-01-01", "period": "1-2"}])
    assert notify.match_source(REJECTED, notify.load_sources(outbox)) is None


# ---------------------------------------------------------------- 对外文档
def _seeded(tmp_path: Path):
    outbox = _plan(
        tmp_path,
        [
            {
                "title": "GHP",
                "date": "2026-09-10",
                "period": "4",
                "_application_id": "2026-09-10-1234567",
                "_doc_id": 1234567,
            }
        ],
    )
    return outbox


def test_document_matches_the_agreed_schema(tmp_path: Path):
    """形状要跟负责人给的那份 `nova.classroom-borrow-notification.v1` 对齐。"""
    outbox = _seeded(tmp_path)
    result = notify.rebuild(outbox / "approval", outbox, [REJECTED, APPROVED], repo="ghxd00/jsjysq")
    assert result["new"] == 2

    document = json.loads((outbox / "approval" / "notifications.json").read_text(encoding="utf-8"))
    assert document["schemaVersion"] == "nova.classroom-borrow-notification.v1"
    assert document["batchId"].startswith("notification-")
    assert len(document["notifications"]) == 2

    rejected = next(n for n in document["notifications"] if n["type"] == "rejected")
    assert rejected["sqbh"] == REJECTED["SQBH"]
    assert rejected["applicationId"] == "2026-09-10-1234567"
    assert rejected["result"]["status"] == "rejected"
    assert rejected["result"]["actualRooms"] == []
    assert rejected["activity"]["slotStart"] == "第4节(11:10-12:00)"
    assert rejected["activity"]["campus"] == "仙林校区"
    assert rejected["activity"]["sourceDoc"]["dir"] == "0905-0911"
    assert rejected["activity"]["sourceDoc"]["repo"] == "ghxd00/jsjysq"

    approved = next(n for n in document["notifications"] if n["type"] == "approved")
    assert approved["result"]["status"] == "approved_assigned"
    assert approved["result"]["actualRooms"] == ["新教404"]
    assert approved["result"]["feedback"] == ""
    # 标题里的「（意向：…）」要剥掉
    assert approved["activity"]["title"] == "GHP"


def test_unmatched_goes_to_its_own_file(tmp_path: Path):
    """认不出来**不进对外文档** —— 宁可让人多看一眼，不可让下游收到猜出来的东西。"""
    outbox = _plan(tmp_path, [{"title": "毫不相干", "date": "2030-01-01", "period": "1-2"}])
    result = notify.rebuild(outbox / "approval", outbox, [REJECTED], repo="r")
    assert result["new"] == 1
    assert result["total"] == 0
    assert result["unmatched"] == 1

    document = json.loads((outbox / "approval" / "notifications.json").read_text(encoding="utf-8"))
    assert document["notifications"] == []
    bucket = json.loads((outbox / "approval" / "unmatched.json").read_text(encoding="utf-8"))
    assert bucket["unmatched"][0]["sqbh"] == REJECTED["SQBH"]
    assert "找不到" in bucket["unmatched"][0]["why"]


def test_rebuild_is_idempotent_but_the_document_is_regenerated(tmp_path: Path):
    """账本只追加、文档每轮重生：跑两次，通知条数不变、id 不变。"""
    outbox = _seeded(tmp_path)
    first = notify.rebuild(outbox / "approval", outbox, [REJECTED], repo="r")
    second = notify.rebuild(outbox / "approval", outbox, [REJECTED], repo="r")
    assert first["new"] == 1
    assert second["new"] == 0  # 账本没涨

    document = json.loads((outbox / "approval" / "notifications.json").read_text(encoding="utf-8"))
    assert len(document["notifications"]) == 1 == second["total"]
    assert len(notify.read_ledger(outbox / "approval")) == 1


def test_readable_markdown_says_the_result_in_plain_words(tmp_path: Path):
    outbox = _seeded(tmp_path)
    notify.rebuild(outbox / "approval", outbox, [APPROVED, REJECTED], repo="r")
    document = json.loads((outbox / "approval" / "notifications.json").read_text(encoding="utf-8"))
    text = notify.summarize_readable(document)
    assert "已通过" in text and "已退回" in text
    assert "新教404" in text
    assert "GHP" in text
    # 程序维护的正文不该出现内部批号
    assert "batchId" not in text
