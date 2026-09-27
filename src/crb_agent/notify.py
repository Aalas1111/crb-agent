"""审批结果：把学校侧的「审核结束了」变成一份能分发的通知。

数据流：

```
crb borrow list --json    （学校只看得到这个）
        │  classify()  用 SHBZ / SHZT_DISPLAY 判「结束了没有、结束成什么」
        ▼
approval/ledger.jsonl     只追加的账本（记事实：什么时候第一次看到它结束了）
        │  build_notifications()
        ▼
approval/notifications.json   对外文档（每轮从账本**重生**）
approval/unmatched.json       认不出来的（等人看，不进对外文档）
```

两个刻意的设计（[`docs/proposal-approval-notifications.md`](../../docs/proposal-approval-notifications.md) §4）：

* **账本只追加、对外文档重生**：文档是账本的纯函数，所以重跑/重启/手抖都不会
  让内容漂；而 `notificationId` 稳定（见 :func:`notification_id`），下游按 id 去重，
  于是「重生」不会变成「重复通知」。
* **认不出就进 unmatched，绝不猜**：字段名会变、状态码是学校内部的。
  猜错会把 A 活动的结果安到 B 头上，比认不出严重得多。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "nova.classroom-borrow-notification.v1"

#: 判定结果。``pending`` / ``draft`` 都表示「还没结束」，不该出现在通知里。
OUTCOME_APPROVED = "approved"
OUTCOME_REJECTED = "rejected"
OUTCOME_DRAFT = "draft"
OUTCOME_PENDING = "pending"
OUTCOME_UNMATCHED = "unmatched"

#: 学校侧的字段名**只出现在这里**（实测后改这张表，逻辑不动）。
#: 样本来自一条真实的「审核不通过」记录（见 proposal §5）。
FIELD_MAP = {
    "sqbh": "SQBH",
    "status": "SHZT",
    "status_text": "SHZT_DISPLAY",
    "review_flag": "SHBZ",  # 审核标志：样本里是「不通过」
    "feedback": "SHYJ",  # 审核意见
    "room": "FJ",  # 分配到的教室
    "room_name": "JASMC",  # 有的记录只给这个
    "date": "KSRQ",
    "period_start_text": "KSJC_DISPLAY",  # 已经是「第4节(11:10-12:00)」这种写法
    "period_end_text": "JSJC_DISPLAY",
    "campus": "XXXQDM_DISPLAY",  # 已经是「仙林校区」
    "organizer": "JYRXM",  # 借用人姓名
    "purpose": "JYYTMS",  # 用途描述
    "requested_at": "SQRQ",
}

#: 状态文本里的关键词。**照抄同学那个浏览器插件的 ``recordLifecycle()``**——
#: 它在生产里跑着，而且码表（``SHZT``）是学校内部的、会变，中文描述更稳。
_REJECTED_WORDS = re.compile(r"撤回|不通过|驳回|拒绝|退回|取消")
_APPROVED_WORDS = re.compile(r"通过|批准|同意")
_PENDING_WORDS = re.compile(r"待|审核|提交|完成|暂存")
_DRAFT_WORDS = re.compile(r"暂存|草稿")

#: 「（意向：xxx）」是 crb 提交时写进用途描述的，匹配标题时要剥掉。
_INTENT_RE = re.compile(r"（意向：[^）]*）\s*$")


@dataclass
class Classification:
    """一条学校记录被判成了什么，以及**依据**。

    带上 ``reason`` 是有意的：判断错了要能一眼看出是哪条规则干的。
    """

    outcome: str
    reason: str
    rooms: list[str] = field(default_factory=list)
    feedback: str = ""

    @property
    def ended(self) -> bool:
        return self.outcome in (OUTCOME_APPROVED, OUTCOME_REJECTED)


def classify(record: dict[str, Any]) -> Classification:
    """把一条 ``crb borrow list`` 记录判成「结束了没有、结束成什么」。

    顺序很重要：**先看「不通过」**——因为「学生工作处审核不通过」里同时含
    「审核」（像进行中）和「不通过」（是结束）。先判否定词。
    """
    flag = text_of(record, "review_flag")
    status = text_of(record, "status")
    text = f"{flag} {text_of(record, 'status_text')} {status}"

    # FJ 与 JASMC 往往是同一个教室，**去重**（否则通知里会出现两遍同一个房间）。
    # 少数记录只给其中一个，所以两个都要看。
    rooms = list(
        dict.fromkeys(r for r in (text_of(record, "room"), text_of(record, "room_name")) if r)
    )
    feedback = text_of(record, "feedback")

    if _DRAFT_WORDS.search(text) or status == "00":
        return Classification(OUTCOME_DRAFT, f"是草稿（{text.strip() or status}）")
    if _REJECTED_WORDS.search(text):
        return Classification(OUTCOME_REJECTED, f"命中退回关键词：{text.strip()}", rooms, feedback)
    if _APPROVED_WORDS.search(text):
        if not rooms:
            # 负责人说这种情况「应该不存在」；真出现了就让人看一眼，
            # 而不是发一条没有教室的「已通过」。
            return Classification(
                OUTCOME_UNMATCHED, "判定为已通过，但读不到教室（FJ/JASMC 都为空）", rooms, feedback
            )
        return Classification(OUTCOME_APPROVED, f"命中通过关键词：{text.strip()}", rooms, feedback)
    if _PENDING_WORDS.search(text):
        return Classification(OUTCOME_PENDING, f"还在流程里：{text.strip()}")
    return Classification(OUTCOME_UNMATCHED, f"认不出的状态：{text.strip() or status or '(空)'}")


# ---------------------------------------------------------------- 账本
def ledger_path(root: Path) -> Path:
    return root / "ledger.jsonl"


def read_ledger(root: Path) -> list[dict[str, Any]]:
    """读账本。半行（写的时候被杀）跳过，不让整个账本读不出来。"""
    path = ledger_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("sqbh"):
            out.append(parsed)
    return out


def append_ledger(root: Path, entries: Iterable[dict[str, Any]]) -> int:
    """把新条目追加进账本，返回写了几条。**只追加，绝不重写**。"""
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    with ledger_path(root).open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            written += 1
    return written


def new_entries(
    records: list[dict[str, Any]], ledger: list[dict[str, Any]], now: float | None = None
) -> list[dict[str, Any]]:
    """挑出「结束了、但账本里还没有」的那些。

    判断依据是 **outcome 变了**才算新的：一条申请从「已通过但没有教室」变成
    「已通过且有教室」，那是新消息（社员想知道教室定了），该再发一次。
    """
    seen = {str(e.get("sqbh")): str(e.get("outcome")) for e in ledger}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now or time.time()))
    fresh: list[dict[str, Any]] = []
    for record in records:
        sqbh = str(record.get(FIELD_MAP["sqbh"]) or record.get("sqbh") or "").strip()
        if not sqbh:
            continue
        verdict = classify(record)
        if not verdict.ended:
            continue
        if seen.get(sqbh) == verdict.outcome:
            continue
        fresh.append(
            {
                "sqbh": sqbh,
                "first_seen_ended": stamp,
                "outcome": verdict.outcome,
                "reason": verdict.reason,
                "rooms": verdict.rooms,
                # 原始记录原样留着：字段名会变，这份快照是唯一能事后复盘的东西。
                "snapshot": record,
            }
        )
    return fresh


# ---------------------------------------------------------------- 关联
def normalize_title(value: str) -> str:
    """标题归一：剥掉 crb 写进去的「（意向：xxx）」再比。"""
    return _INTENT_RE.sub("", str(value or "").strip()).strip()


def load_sources(outbox: Path) -> list[dict[str, Any]]:
    """把语雀侧的 activity 收集起来（当前周期 + 归档），带上溯源信息。

    返回每项：``{date, period, title, application_id, doc_id, cycle, repo}``。
    归档也要扫 —— 可借窗口 9 天会跨周期，往期那份 plan.json 里的活动
    仍可能在这周被审批。
    """
    sources: list[dict[str, Any]] = []
    plans: list[Path] = []
    current = outbox / "plan.json"
    if current.is_file():
        plans.append(current)
    archive = outbox / "archive"
    if archive.is_dir():
        plans += sorted(archive.glob("*/plan.json"), reverse=True)

    for path in plans:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        cycle = str(data.get("cycle") or path.parent.name)
        defaults = data.get("defaults") or {}
        for activity in data.get("activities") or []:
            if not isinstance(activity, dict):
                continue
            sources.append(
                {
                    "date": str(activity.get("date") or ""),
                    "period": str(activity.get("period") or ""),
                    "title": normalize_title(str(activity.get("title") or "")),
                    "application_id": str(activity.get("_application_id") or ""),
                    "doc_id": activity.get("_doc_id"),
                    "cycle": cycle,
                    "organizer": str(activity.get("JYRXM") or defaults.get("JYRXM") or ""),
                }
            )
    return sources


def _period_bounds(period: str) -> tuple[int, int] | None:
    match = re.match(r"^(\d{1,2})(?:\s*[-~～至]\s*(\d{1,2}))?$", str(period or "").strip())
    if not match:
        return None
    start = int(match.group(1))
    return start, int(match.group(2) or start)


def period_of_slot_text(text: str) -> tuple[int, int] | None:
    """从「第4节(11:10-12:00)」里取出 ``(4, 4)``。"""
    match = re.search(r"第\s*(\d{1,2})\s*节", str(text or ""))
    if not match:
        return None
    number = int(match.group(1))
    return number, number


def match_source(record: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按（日期 + 节次 + 标题）把学校记录对回语雀的申请。

    节次的取法：记录给的是 ``KSJC_DISPLAY``（起）与 ``JSJC_DISPLAY``（止），
    从里面抽节号区间；语雀 activity 给的是 ``period``（如 ``"5-6"``）。
    两者都归一成 ``(起, 止)`` 再比。
    """
    day = text_of(record, "date")
    start = period_of_slot_text(text_of(record, "period_start_text"))
    end = period_of_slot_text(text_of(record, "period_end_text"))
    if not day or start is None:
        return None
    bounds = (start[0], (end or start)[0])
    title = normalize_title(text_of(record, "purpose"))

    for source in sources:
        if source["date"] != day:
            continue
        if _period_bounds(source["period"]) != bounds:
            continue
        if title and source["title"] and title != source["title"]:
            continue
        return source
    return None


# ---------------------------------------------------------------- 对外文档
def notification_id(sqbh: str, outcome: str, rooms: list[str]) -> str:
    """稳定生成：同一个结果永远得到同一个 id。

    **不含时间戳** —— 含了就每轮都变，下游按 id 去重立刻失效。
    含教室列表：教室后来定了是一条**新**消息（``approved_assigned`` 才带教室）。
    """
    raw = f"{sqbh}|{outcome}|{','.join(sorted(rooms))}"
    return "notify-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def build_notifications(
    ledger: list[dict[str, Any]], sources: list[dict[str, Any]], repo: str = ""
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """从账本重生对外文档，同时给出认不出来的那些。

    返回 ``(通知文档, unmatched 列表)``。
    """
    notifications: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for entry in ledger:
        outcome = str(entry.get("outcome") or "")
        if outcome not in (OUTCOME_APPROVED, OUTCOME_REJECTED):
            continue
        snapshot = entry.get("snapshot") or {}
        source = match_source(snapshot, sources)
        if source is None:
            unmatched.append(
                {
                    "sqbh": entry.get("sqbh"),
                    "outcome": outcome,
                    "why": "在 plan.json 与归档里找不到对应的活动（日期+节次+标题 都对不上）",
                    "detected_at": entry.get("first_seen_ended"),
                    "snapshot": snapshot,
                }
            )
            continue
        if not source.get("application_id"):
            unmatched.append(
                {
                    "sqbh": entry.get("sqbh"),
                    "outcome": outcome,
                    "why": "对上了活动，但那份 plan.json 里没有 _application_id（溯源键缺失）",
                    "detected_at": entry.get("first_seen_ended"),
                    "snapshot": snapshot,
                }
            )
            continue

        rooms = [str(r) for r in (entry.get("rooms") or [])]
        notifications.append(
            {
                "notificationId": notification_id(str(entry.get("sqbh")), outcome, rooms),
                "applicationId": source["application_id"],
                "sqbh": str(entry.get("sqbh")),
                "type": outcome,
                "activity": {
                    "title": normalize_title(text_of(snapshot, "purpose")) or source["title"],
                    "date": text_of(snapshot, "date") or source["date"],
                    "slotStart": text_of(snapshot, "period_start_text"),
                    "slotEnd": text_of(snapshot, "period_end_text"),
                    "campus": text_of(snapshot, "campus"),
                    # 借用人姓名：schema 要 organizer。这是**人名**，写进文档请知悉。
                    "organizer": text_of(snapshot, "organizer") or source.get("organizer", ""),
                    "sourceDoc": {
                        "id": str(source.get("doc_id") or ""),
                        "title": source["title"],
                        "repo": repo,
                        "dir": source["cycle"],
                    },
                },
                "result": {
                    "status": "approved_assigned" if outcome == OUTCOME_APPROVED else "rejected",
                    "actualRooms": rooms if outcome == OUTCOME_APPROVED else [],
                    "feedback": str(entry.get("feedback") or "")
                    if outcome == OUTCOME_REJECTED
                    else "",
                },
                "detectedAt": str(entry.get("first_seen_ended") or ""),
            }
        )

    notifications.sort(key=lambda item: item["detectedAt"])
    batch = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "batchId": f"notification-{batch}",
        "generatedAt": batch,
        "notifications": notifications,
    }
    return document, unmatched


# ---------------------------------------------------------------- 落盘
def rebuild(
    approval_dir: Path, outbox: Path, records: list[dict[str, Any]], *, repo: str = ""
) -> dict[str, Any]:
    """跑一轮「比对 → 记账 → 重生文档」。返回这一轮干了什么（给 CLI 打印）。

    ``approval_dir`` 是产物目录（``<outbox>/approval``），``outbox`` 是语雀侧的
    产出目录（找 ``plan.json`` 与归档用）。两个都显式传，不从路径猜 ——
    猜错会让「找不到对应活动」看起来像数据问题。
    """
    ledger = read_ledger(approval_dir)
    fresh = new_entries(records, ledger)
    if fresh:
        append_ledger(approval_dir, fresh)
        ledger += fresh

    document, unmatched = build_notifications(ledger, load_sources(outbox), repo=repo)

    approval_dir.mkdir(parents=True, exist_ok=True)
    _write_json(approval_dir / "notifications.json", document)
    _write_json(
        approval_dir / "unmatched.json",
        {"generatedAt": document["generatedAt"], "unmatched": unmatched},
    )
    return {
        "new": len(fresh),
        "total": len(document["notifications"]),
        "unmatched": len(unmatched),
        "ended_seen": sum(1 for r in records if classify(r).ended),
        "checked": len(records),
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def text_of(record: dict[str, Any], key: str) -> str:
    """按 FIELD_MAP 取字段，并把 None 归一成空串（学校大量字段是 null）。"""
    field_name = FIELD_MAP.get(key, key)
    value = record.get(field_name)
    if value is None:
        value = record.get(key)
    return "" if value is None else str(value).strip()


def summarize_readable(document: dict[str, Any]) -> str:
    """给「审批结果」文档渲染 Markdown（人看的；**程序维护的正文**）。

    正文里不含工具调用与内部批号，只讲「哪个活动、结果是什么、教室在哪」。
    """
    lines = [
        "> 教室借用申请的审批结果，**最新的在最下面**。",
        "> 由程序自动维护（[crb-agent](https://github.com/Aalas1111/crb-agent)），每轮重建；请勿手工编辑。",
        "",
    ]
    items = document.get("notifications") or []
    if not items:
        lines += ["（还没有审批结束的申请。）", ""]
        return "\n".join(lines)
    for item in items:
        activity = item.get("activity") or {}
        result = item.get("result") or {}
        approved = item.get("type") == "approved"
        head = "已通过" if approved else "已退回"
        lines.append(f"## {head} · {activity.get('title') or '(无标题)'}")
        lines.append(
            f"{activity.get('date') or ''} {activity.get('slotStart') or ''}"
            f"（{activity.get('campus') or ''}）"
        )
        if approved:
            rooms = "、".join(result.get("actualRooms") or []) or "（教室待定）"
            lines.append(f"教室：**{rooms}**")
        else:
            lines.append(f"原因：{result.get('feedback') or '（学校未给原因）'}")
        if activity.get("organizer"):
            lines.append(f"借用人：{activity['organizer']}")
        lines += ["", "---", ""]
    return "\n".join(lines)
