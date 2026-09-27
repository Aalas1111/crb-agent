"""假 ``crb`` / 假 ``yqa``。

存在的理由：测试里**不能**真的叫到 ``crb``（它会去打学校接口、还可能真的写申请），
但「命令行拼得对不对」「退出码怎么翻译」又是最容易出 bug 的地方。所以这个替身
只做两件事：把收到的参数记下来，按 ``FAKE_CRB_MODE`` 回一段固定的 JSON。

用法：``python fake_cli.py crb doctor --json``（参数原样透传，含 argv[0] 的角色名）。
"""

from __future__ import annotations

import json
import os
import sys

# 真 ``crb`` 也做了同一件事（见它的 cli.py 开头）：Windows 控制台默认 GBK，
# 中文会乱码。替身要和真身行为一致，否则夹具会掩盖真实的编码问题。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _log(argv: list[str]) -> None:
    path = os.environ.get("FAKE_CRB_ARGS_FILE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv, ensure_ascii=False) + "\n")


def _emit(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- crb
BORROWS = [
    {
        "SQBH": "7e75e389f83d4aac95bdcc28b5521041",
        "SHZT": "99",
        "SHZT_DISPLAY": "已通过",
        "JYYTMS": "新生见面会（意向：仙I-201）",
        "XXXQDM": "3",
        "KSRQ": "2026-10-01",
        "KSJC": "7",
        "JSJC": "8",
        "FJ": "仙I-201",
    },
    {
        "SQBH": "18e33250b39847bbb17821afbd49e522",
        "SHZT": "65",
        "SHZT_DISPLAY": "待审核",
        "JYYTMS": "读书会",
        "XXXQDM": "3",
        "KSRQ": "2026-10-02",
        "KSJC": "9",
        "JSJC": "10",
    },
]

ASSIGNMENTS_PREVIEW = [
    {
        "activity": {"title": "新生见面会", "date": "2026-10-01", "period": "7-8", "people": 25},
        "room": {"JASMC": "仙I-201", "SKZWS": 30},
        "status": "ok",
        "note": "",
    },
    {
        "activity": {"title": "读书会", "date": "2026-10-02", "period": "9-10", "people": 50},
        "room": None,
        "status": "duplicate",
        "note": "与已有申请时间重叠：18e33250(待审核)",
    },
]


def crb(argv: list[str]) -> int:
    mode = os.environ.get("FAKE_CRB_MODE", "ok")
    if mode == "noauth":
        sys.stderr.write("登录态已失效，请重新运行 `crb login`。\n")
        return 2
    if mode == "waf":
        sys.stderr.write("请求被学校 WAF 拦截（403）。\n")
        return 3
    if mode == "missing":
        sys.stderr.write("找不到命令\n")
        return 127

    if not argv:
        return 2

    # 去掉全局选项，找到子命令
    rest = [a for a in argv if a != "--json"]
    command = rest[0]

    if command == "doctor":
        _emit({"ok": True, "term": "2026-2027-1", "JSJYSFKT": "1", "org": "400760"})
        return 0
    if command == "campus":
        _emit([{"id": "1", "name": "鼓楼校区"}, {"id": "3", "name": "仙林校区"}])
        return 0
    if command == "buildings":
        _emit([{"JXLDM": "11", "JXLMC": "仙林教学楼一区", "XXXQDM": "3"}])
        return 0
    if command == "free":
        _emit(
            [
                {
                    "JASMC": "仙I-201",
                    "SKZWS": 30,
                    "KSZWS": 30,
                    "JXLDM_DISPLAY": "仙林教学楼一区",
                    "JASLXDM_DISPLAY": "多媒体教室",
                    "KXSJ": "08:00-09:50",
                }
            ]
        )
        return 0
    if command == "borrow":
        action = rest[1] if len(rest) > 1 else ""
        if action == "list":
            _emit(BORROWS)
            return 0
        _emit({"ok": True, "code": 1, "msg": "操作成功"})
        return 0
    if command == "plan":
        mode_arg = "preview"
        if "--save" in rest:
            mode_arg = "save"
        elif "--submit" in rest:
            mode_arg = "submit"
        if mode_arg == "preview":
            _emit(ASSIGNMENTS_PREVIEW)
        else:
            _emit(
                {
                    "assignments": ASSIGNMENTS_PREVIEW,
                    "results": [
                        {"title": "新生见面会", "ok": True, "msg": "保存成功"},
                        {
                            "title": "读书会",
                            "ok": False,
                            "msg": "与已有申请时间重叠：18e33250(待审核)",
                        },
                    ],
                }
            )
        return 0

    sys.stderr.write(f"未知命令：{command}\n")
    return 2


# ---------------------------------------------------------------- yqa
def yqa(argv: list[str]) -> int:
    if os.environ.get("FAKE_YQA_FAIL"):
        sys.stderr.write("没有配置知识库\n")
        return 1
    rest = [a for a in argv if a not in ("--json",)]
    if rest and rest[0] == "export-plan":
        _emit(
            {
                "cycle": "0927-1003",
                "activities": [{"title": "新生见面会", "date": "2026-10-01", "period": "7-8"}],
            }
        )
        return 0
    _emit({"ok": True})
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        sys.stderr.write("fake_cli.py <crb|yqa> <args...>\n")
        return 2
    role, rest = argv[0], argv[1:]
    _log([role, *rest])
    if role == "crb":
        return crb(rest)
    if role == "yqa":
        return yqa(rest)
    sys.stderr.write(f"未知角色：{role}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
