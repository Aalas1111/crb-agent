"""工具注册表：agent 能做什么，边界就画在这里。

**这是本项目安全性的落点**（与 ``yqa`` 的 ``docs/principles.md`` 同一条纪律）：
「有没有这个工具」是粗粒度、可测的约束；「用它输出的话对不对」是语义判断，
不归程序管。所以这里只做两件事：

1. 把 ``crb`` / ``yqa`` 的命令封装成带 JSON schema 的工具，参数**逐字段校验后**
   以参数数组交给 ``subprocess``（**永远没有 shell**，LLM 拼不出命令）；
2. 把退出码与 stdout 翻译成 :class:`~crb_agent.events.ToolResult`。

至于「什么时候该存草稿、什么时候该提交」「这条申请该不该去重掉」——那是 LLM
看了事实之后自己判断的事，写在提示词里，不写在这儿。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings
from .events import RiskLevel, ToolResult

#: 一条命令最多跑多久。``crb plan`` 要给每一条活动各查一次空闲教室，
#: 一个 20 条活动的批次实测要好几分钟，所以这里的余量是刻意的。
TIMEOUT_READ = 120.0
TIMEOUT_PLAN = 900.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIOD_RE = re.compile(r"^\d{1,2}(-\d{1,2})?$")
#: 申请编号的形状（见 :func:`_require_sqbh` 的说明）。
#: 首字符必须是字母数字——不允许以 ``-`` 开头，否则它可能被当成一个选项
#: （``--submit`` 恰好也满足「字母数字加连字符」）。
_SQBH_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: RiskLevel
    handler: Callable[[dict[str, Any]], ToolResult]
    guidelines: tuple[str, ...] = field(default=())


class ToolError(RuntimeError):
    """参数不合法——是 LLM 用错了工具，把这句话原样回给它。"""


# ---------------------------------------------------------------- 命令执行
def _split_bin(value: str) -> list[str]:
    """``crb_bin`` 允许写成 ``uv tool run crb`` 这种带参数的形式。"""
    return shlex.split(value)


def _run(
    cmd: list[str], *, cwd: Path | None = None, timeout: float = TIMEOUT_READ
) -> tuple[int, str, str]:
    # PYTHONIOENCODING：子进程可能是 Python 写的（crb / yqa 都是），而它们在
    # Windows 上默认按控制台代码页（cp936）输出中文 —— 我们按 UTF-8 解码就会
    # 得到乱码，而且是**只在开发机上出现**的那种乱码（Linux 默认就是 UTF-8）。
    # 显式钉死，两边行为一致。真实事故：在开发机上 smoke test 时工具标题全成
    # 了 `????`，模型据此以为后端返回了坏数据。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"找不到命令 {cmd[0]!r}：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"命令超时（{timeout:.0f}s）：{' '.join(cmd[:3])} …") from exc
    return proc.returncode, proc.stdout, proc.stderr


def _extract_json(text: str) -> Any:
    """从输出里捞出 JSON。

    ``--json`` 的产物是干净的 JSON，但 ``crb`` 会先打一行进度提示（如
    「已有申请 3 条，已纳入防重合检测」），所以从第一个 ``{``/``[`` 起截。
    """
    stripped = text.strip()
    if not stripped:
        return None
    for index, char in enumerate(stripped):
        if char in "{[":
            try:
                return json.loads(stripped[index:])
            except ValueError:
                break
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _tail(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


#: crb 的退出码 → 我们内部的 ``kind``。**必须分开报**，因为处理方式完全不同：
#: 2 要人扫码；3 是学校拦（扫码没用）；4 是「本机的 crb 执行器没连上」
#: （要人去开电脑 / 起执行器，见 docs/deploy.md §0.1）。
#: 混成一句「登录态不可用」会让人对着解决不了的问题反复扫码 —— 实测踩过。
_KINDS = {0: "ok", 2: "not_logged_in", 3: "waf_blocked", 4: "executor_offline"}

_KIND_HINTS = {
    "waf_blocked": (
        "学校对办事大厅接口的判定不只看出口 IP（实测：换出口也拦）。"
        "出路是让请求从能过的那台机器发出，见 docs/deploy.md §0.1。"
    ),
    "not_logged_in": "需要重新扫码登录（打开 /agent 会跳到扫码页）。",
    "executor_offline": (
        "本机的 crb 执行器没连上 —— 请在你自己的电脑上跑 "
        "scripts/local_crb_executor.py 与 SSH 隧道，见 docs/deploy.md §0.1。"
    ),
}


def _kind_for(code: int) -> str:
    return _KINDS.get(code, "unknown")


def _failure(summary: str, code: int, raw: str) -> ToolResult:
    kind = _kind_for(code)
    return ToolResult(
        status="error",
        summary=summary,
        data={
            "exit_code": code,
            "kind": kind,
            "hint": _KIND_HINTS.get(kind, ""),
            "output": _tail(raw),
        },
    )


# ---------------------------------------------------------------- 参数校验
def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"缺少参数 {key}（必须是字符串）")
    return value.strip()


def _optional_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _require_date(payload: dict[str, Any], key: str) -> str:
    value = _require_str(payload, key)
    if not _DATE_RE.match(value):
        raise ToolError(f"{key} 必须是 YYYY-MM-DD，收到 {value!r}")
    return value


def _require_period(payload: dict[str, Any], key: str) -> str:
    value = _require_str(payload, key)
    if not _PERIOD_RE.match(value):
        raise ToolError(f'{key} 必须是节次区间（如 "1-2" 或 "7"），收到 {value!r}')
    return value


def _require_choice(payload: dict[str, Any], key: str, choices: tuple[str, ...]) -> str:
    value = _require_str(payload, key)
    if value not in choices:
        raise ToolError(f"{key} 只能是 {' / '.join(choices)}，收到 {value!r}")
    return value


def _require_sqbh(payload: dict[str, Any]) -> str:
    """申请编号。

    ⚠️ **不是纯数字**：学校给的是 32 位十六进制串
    （如 ``7e75e389f83d4aac95bdcc28b5521041``，见上游 handoff 的申请 JSON 例子）。
    所以这里按「一段安全标识符」校验：只放行字母数字与 ``_-``，长度 8~64。
    这挡住的不是注入（我们本来就没有 shell），而是误传进来的整句话——
    比如模型把「撤回第7条」整个塞进 sqbh。
    """
    value = payload.get("sqbh")
    text = str(value).strip() if value is not None else ""
    if not _SQBH_RE.match(text):
        raise ToolError(
            f"sqbh 必须是申请编号（字母数字，如 7e75e389f83d4aac95bdcc28b5521041），收到 {value!r}"
        )
    return text


def _resolve_within(base: Path, relative: str) -> Path:
    """把相对路径钉在 ``base`` 里。

    这是**唯一**接受 LLM 自由文本当路径的地方，所以必须挡住 ``..`` 与绝对路径。
    注意用 ``resolve()`` 之后比较：这样符号链接也骗不过去。
    """
    if not relative:
        raise ToolError("缺少路径参数")
    candidate = (base / relative).resolve()
    root = base.resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"路径越界：{relative!r} 不在 {root} 里")
    return candidate


# ---------------------------------------------------------------- 注册表
class Toolbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tools: dict[str, Tool] = {}
        self._register_all()

    # ---- 对外 ----------------------------------------------------------
    def manifest(self) -> list[dict[str, Any]]:
        """给 LLM 的工具清单（OpenAI function calling 格式）。

        ``description`` 里带上 ``guidelines``：工具的全部用法契约必须在模型看得见
        的地方，只在提示词里写一遍是不够的——模型选工具时读的是这里。
        """
        out = []
        for tool in self._tools.values():
            description = tool.description
            if tool.guidelines:
                description += "\n" + "\n".join(f"- {line}" for line in tool.guidelines)
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return out

    def names(self) -> list[str]:
        return list(self._tools)

    def risk_of(self, name: str) -> RiskLevel:
        tool = self._tools.get(name)
        return tool.risk if tool else "read"

    def execute(self, name: str, payload: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                status="error",
                summary=f"没有这个工具：{name}",
                data={"available": self.names()},
            )
        if "__parse_error__" in payload:
            return ToolResult(
                status="error",
                summary=f"{name} 的参数不是合法 JSON，请重新给一次。",
                data={"detail": payload.get("__parse_error__"), "raw": payload.get("__raw__")},
            )
        try:
            return tool.handler(payload)
        except ToolError as exc:
            return ToolResult(status="error", summary=str(exc))
        except Exception as exc:  # noqa: BLE001 - 工具炸了不该带着整个回合一起炸
            return ToolResult(status="error", summary=f"{name} 执行失败：{exc}")

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

    # ---- 命令封装 ------------------------------------------------------
    def _crb(self, args: list[str], *, timeout: float = TIMEOUT_READ) -> tuple[int, Any, str]:
        return self._invoke(self.settings.crb_bin, args, timeout=timeout)

    def _yqa(self, args: list[str], *, timeout: float = TIMEOUT_READ) -> tuple[int, Any, str]:
        return self._invoke(self.settings.yqa_bin, args, timeout=timeout)

    def _invoke(self, binary: str, args: list[str], *, timeout: float) -> tuple[int, Any, str]:
        cmd = [*_split_bin(binary), *args]
        code, out, err = _run(cmd, timeout=timeout)
        return code, _extract_json(out), err or out

    # ---- 工具定义 ------------------------------------------------------
    def _register_all(self) -> None:
        self.register(
            Tool(
                name="crb_status",
                description=(
                    "检查教室借用工具（crb）的自己：登录态还能不能用、当前学年学期、"
                    "借用开关、所在单位代码。**动手之前先调这个**。"
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                risk="read",
                handler=self._crb_status,
                guidelines=(
                    "登录态失效（返回 status=error）时不要去重试别的工具，"
                    "直接告诉用户需要重新扫码登录即可。",
                ),
            )
        )
        self.register(
            Tool(
                name="crb_free_rooms",
                description="查某天、某节次区间、某校区的**空闲教室**（学校权威接口，节次由服务端过滤）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "campus": {
                            "type": "string",
                            "description": "校区代码：1 鼓楼 / 2 浦口 / 3 仙林 / 4 苏州",
                        },
                        "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                        "period": {
                            "type": "string",
                            "description": '节次区间，如 "1-2"；单节写 "7"',
                        },
                        "building": {
                            "type": "string",
                            "description": "教学楼代码 JXLDM（可选，见 crb_buildings）",
                        },
                        "room_type": {
                            "type": "string",
                            "description": "教室类型代码 JASLXDM（可选）",
                        },
                    },
                    "required": ["campus", "date", "period"],
                },
                risk="read",
                handler=self._crb_free_rooms,
            )
        )
        self.register(
            Tool(
                name="crb_campus",
                description="列出校区代码（1 鼓楼 / 2 浦口 / 3 仙林 / 4 苏州）。",
                parameters={"type": "object", "properties": {}, "required": []},
                risk="read",
                handler=lambda _p: self._dictionary(["--campus"]),
                guidelines=("这个字典基本是固定的，不用每次都查。",),
            )
        )
        self.register(
            Tool(
                name="crb_buildings",
                description="列出某校区的教学楼及其代码（JXLDM）。填 activity.building 时要用这里的代码。",
                parameters={
                    "type": "object",
                    "properties": {"campus": {"type": "string", "description": "校区代码"}},
                    "required": ["campus"],
                },
                risk="read",
                handler=self._crb_buildings,
            )
        )
        self.register(
            Tool(
                name="crb_list_borrows",
                description=(
                    "查**我的**教室借用申请（含状态 SHZT：00 草稿 / 1 已撤回 / 65 等待审核 / 99 已通过）。"
                    "这是「已经提交过什么」的唯一权威来源。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": "学年学期，如 2026-2027-1；不填用当前学期",
                        }
                    },
                    "required": [],
                },
                risk="read",
                handler=self._crb_list_borrows,
                guidelines=("想回答「申请批下来没有 / 结果如何」时用它，按 SQBH 或标题对号入座。",),
            )
        )
        self.register(
            Tool(
                name="read_plan",
                description=(
                    "读**当前周期**的申请清单 plan.json（由语雀侧的 agent 产出）。"
                    "里面有 cycle（周期号）、defaults（借用人信息）、activities（待申请的活动）。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "相对路径，默认 plan.json；也可以读 applications/ 下的单个文件",
                        },
                    },
                    "required": [],
                },
                risk="read",
                handler=self._read_plan,
                guidelines=(
                    "**动手前先对一眼 cycle 字段**：它必须是本周的周期号，否则你拿到的是旧清单。",
                    "没有这个文件（或周期是空的）说明语雀侧这一周期还没有受理到申请。",
                ),
            )
        )
        self.register(
            Tool(
                name="list_outbox",
                description="列出语雀侧产出的文件（outbox 目录），用来找往期归档与通知事件。",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对路径，默认 . （outbox 根）"},
                    },
                    "required": [],
                },
                risk="read",
                handler=self._list_outbox,
            )
        )
        self.register(
            Tool(
                name="yqa_refresh_plan",
                description=(
                    "让语雀侧的 agent 重新汇总一次清单（`yqa export-plan`），"
                    "把 outbox/plan.json 刷成最新。语雀上刚受理的申请靠它才会进清单。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "defaults": {
                            "type": "string",
                            "description": (
                                '借用人信息 JSON，如 {"JYDWDM":"400760","JYRXM":"张三",'
                                '"JYRDH":"13800000000","JSJYLXDM":"02"}；不填沿用已保存的那份'
                            ),
                        }
                    },
                    "required": [],
                },
                risk="read",
                handler=self._yqa_refresh_plan,
                guidelines=(
                    "只读性质：它刷新的是我们自己工作区里的清单文件，不碰学校系统。",
                    "defaults 只需设一次（会被落盘保存），不是每次都要传。",
                ),
            )
        )
        self.register(
            Tool(
                name="approval_status",
                description=(
                    "读「审批结果」账本：**已经审批结束**的申请（通过 / 退回）都在这里。"
                    "用它回答「我那些申请批了没有 / 有没有被退回」。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "outcome": {
                            "type": "string",
                            "enum": ["all", "approved", "rejected"],
                            "description": "只看某一类；默认 all",
                        }
                    },
                    "required": [],
                },
                risk="read",
                handler=self._approval_status,
                guidelines=(
                    "这是**本地账本**，由 crb-agent-notify 轮询维护 —— 新鲜度取决于轮询间隔"
                    "（默认 10 分钟）。要「此刻最新的」，用 crb_list_borrows 直接问学校。",
                    "没有教室的「已通过」不会记进这里（那种记录会被归到 unmatched 等人看）。",
                ),
            )
        )
        self.register(
            Tool(
                name="yqa_refresh_approval",
                description=(
                    "让语雀侧的 agent 把《审批结果》文档重建成最新（`yqa refresh-approval`）。"
                    "审批结果变了之后由轮询调它；一般不用手调。"
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                risk="read",
                handler=self._yqa_refresh_approval,
                guidelines=(
                    "它写的是**语雀知识库里那篇《审批结果》**，内容取自我们本地产出的 "
                    "approval/notifications.json —— 所以要先有那一份。",
                ),
            )
        )
        self.register(
            Tool(
                name="crb_plan",
                description=(
                    "按 plan.json 批量出借用方案 / 存草稿 / 正式提交。"
                    "mode=preview 只出方案不写系统；mode=save 存成草稿；mode=submit 正式提交。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["preview", "save", "submit"]},
                        "path": {
                            "type": "string",
                            "description": "清单文件相对路径，默认 plan.json",
                        },
                        "allow_overlap": {
                            "type": "boolean",
                            "description": "允许与已有申请时间重叠（默认 false = 拦截重复）",
                        },
                    },
                    "required": ["mode"],
                },
                risk="dangerous",
                handler=self._crb_plan,
                guidelines=(
                    "**先 preview，把方案（含被跳过的重复条目）讲给用户听，得到明确同意再 save / submit。**",
                    "默认只到 save（草稿）；submit 会真的占用教室，必须用户明确说「提交」才做。",
                    "结果里 status=duplicate 的条目是**已经申请过**的（时间与已有申请重叠），"
                    "crb 已经替你跳过了，不要为了「让它跑完」去加 allow_overlap。",
                    "status=no_room / too_small 是选不到教室，把原因告诉用户，不要自己编一个教室填进去。",
                ),
            )
        )
        self.register(
            Tool(
                name="crb_borrow_action",
                description=(
                    "对**已知编号**的单条申请做操作：submit 正式提交草稿 / withdraw 撤回 / "
                    "delete 删除 / edit 修改。编号从 crb_list_borrows 拿。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "sqbh": {"type": "string", "description": "申请编号 SQBH（纯数字）"},
                        "action": {
                            "type": "string",
                            "enum": ["submit", "withdraw", "delete", "edit"],
                        },
                        "data": {
                            "type": "string",
                            "description": 'edit 时要改的字段 JSON，如 {"ZRS":"35","JYYTMS":"..."}',
                        },
                        "draft": {
                            "type": "boolean",
                            "description": "edit 时只存草稿不重新提交（默认 false = 改完重新提交）",
                        },
                    },
                    "required": ["sqbh", "action"],
                },
                risk="dangerous",
                handler=self._crb_borrow_action,
                guidelines=(
                    "delete / withdraw 是不可逆的，先说清楚要动哪一条、再等用户点头。",
                    "撤回（withdraw）之后状态变成 SHZT=1，可以再 submit 回去。",
                ),
            )
        )

    # ---- 各工具的实现 --------------------------------------------------
    def _crb_status(self, _payload: dict[str, Any]) -> ToolResult:
        code, data, raw = self._crb(["doctor", "--json"])
        if code != 0:
            # crb 的退出码是有语义的（见它的 README）：2 = 没登录，3 = 被风控拦。
            # **这两件事必须分开报**：一个要人扫码，另一个扫码**没有用**。
            # 混成一句「登录态不可用」会让人反复扫码，而问题在出口 IP 上。
            if code == 3:
                return _failure("被学校拦了（403）——**这不是登录态的问题，扫码没用**", code, raw)
            if code == 4:
                return _failure(
                    "本机的 crb 执行器没连上（那台电脑关机了，或没跑执行器）", code, raw
                )
            return _failure(
                "登录态不可用（需要重新扫码登录）"
                if code == 2
                else f"crb doctor 失败（退出码 {code}）",
                code,
                raw,
            )
        return ToolResult(
            status="ok",
            summary=f"登录态可用，当前学期 {data.get('term') or '未知'}",
            data=data,
        )

    def _dictionary(self, args: list[str]) -> ToolResult:
        code, data, raw = self._crb([*args, "--json"])
        if code != 0:
            return _failure(f"取字典失败（退出码 {code}）", code, raw)
        return ToolResult(status="ok", summary=f"共 {len(data or [])} 项", data=data)

    def _crb_buildings(self, payload: dict[str, Any]) -> ToolResult:
        campus = _require_str(payload, "campus")
        return self._dictionary(["buildings", "--campus", campus])

    def _crb_free_rooms(self, payload: dict[str, Any]) -> ToolResult:
        args = [
            "free",
            "--campus",
            _require_str(payload, "campus"),
            "--date",
            _require_date(payload, "date"),
            "--period",
            _require_period(payload, "period"),
        ]
        building = _optional_str(payload, "building")
        if building:
            args += ["--building", building]
        room_type = _optional_str(payload, "room_type")
        if room_type:
            args += ["--room-type", room_type]
        code, data, raw = self._crb([*args, "--json"])
        if code != 0:
            return _failure(f"查询失败（退出码 {code}）", code, raw)
        rooms = data or []
        if not rooms:
            return ToolResult(status="ok", summary="该时段没有空闲教室", data={"rooms": []})
        return ToolResult(
            status="ok", summary=f"查到 {len(rooms)} 间空闲教室", data={"rooms": rooms}
        )

    def _crb_list_borrows(self, payload: dict[str, Any]) -> ToolResult:
        args = ["borrow", "list"]
        term = _optional_str(payload, "term")
        if term:
            args += ["--term", term]
        code, data, raw = self._crb([*args, "--json"])
        if code != 0:
            return _failure(f"取申请列表失败（退出码 {code}）", code, raw)
        rows = data or []
        return ToolResult(
            status="ok",
            summary=f"共 {len(rows)} 条申请",
            data={"borrows": rows, "status_legend": _SHZT_LEGEND},
        )

    # ---- 语雀侧 --------------------------------------------------------
    def _outbox(self) -> Path:
        """产出目录。配不出来就回一句人话——「路径配错了」和「本周没有申请」
        是两件完全不同的事，不能让它们长得一样。"""
        try:
            return self.settings.outbox()
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

    def _read_plan(self, payload: dict[str, Any]) -> ToolResult:
        relative = _optional_str(payload, "path") or "plan.json"
        outbox = self._outbox()
        if not outbox.is_dir():
            return ToolResult(
                status="error",
                summary=f"语雀产出目录不存在：{outbox}（这是配置问题，不是「本周没有申请」）",
                data={
                    "outbox": str(outbox),
                    "hint": "检查 CRBA_YUQUE_WORKSPACE / YQA_REPO / CRBA_OUTBOX",
                },
            )
        path = _resolve_within(outbox, relative)
        if not path.is_file():
            return ToolResult(
                status="error",
                summary=f"{relative} 不存在（语雀侧这一周期可能还没有受理到申请）",
                data={"outbox": str(outbox)},
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return ToolResult(status="error", summary=f"{relative} 不是合法 JSON：{exc}")
        activities = data.get("activities") if isinstance(data, dict) else None
        if isinstance(activities, list):
            summary = f"周期 {data.get('cycle') or '?'}，{len(activities)} 条活动"
        else:
            summary = f"已读取 {relative}"
        return ToolResult(status="ok", summary=summary, data=data)

    def _list_outbox(self, payload: dict[str, Any]) -> ToolResult:
        relative = _optional_str(payload, "path") or "."
        path = _resolve_within(self._outbox(), relative)
        if not path.exists():
            return ToolResult(status="error", summary=f"没有这个目录：{relative}")
        if path.is_file():
            return ToolResult(status="ok", summary=f"{relative}（{path.stat().st_size} 字节）")
        entries = []
        for child in sorted(path.iterdir()):
            entries.append(
                {
                    "name": child.name,
                    "dir": child.is_dir(),
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        return ToolResult(status="ok", summary=f"{len(entries)} 项", data={"entries": entries})

    def _yqa_refresh_plan(self, payload: dict[str, Any]) -> ToolResult:
        args = ["export-plan", "--workspace", str(self.settings.yuque_workspace)]
        if self.settings.yqa_repo:
            args += ["--repo", self.settings.yqa_repo]
        defaults = _optional_str(payload, "defaults")
        if defaults:
            try:
                parsed = json.loads(defaults)
            except ValueError as exc:
                raise ToolError(f"defaults 不是合法 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise ToolError("defaults 必须是 JSON 对象")
            args += ["--defaults", defaults]
        code, data, raw = self._yqa(args, timeout=TIMEOUT_PLAN)
        if code != 0:
            return ToolResult(
                status="error",
                summary=f"刷新清单失败（退出码 {code}）：{_tail(raw)}",
                data={"exit_code": code},
            )
        activities = (data or {}).get("activities") if isinstance(data, dict) else None
        count = len(activities) if isinstance(activities, list) else "?"
        cycle = (data or {}).get("cycle") if isinstance(data, dict) else None
        return ToolResult(
            status="ok",
            summary=f"清单已刷新：周期 {cycle or '?'}，{count} 条活动",
            data=data,
        )

    def _yqa_refresh_approval(self, _payload: dict[str, Any]) -> ToolResult:
        args = ["refresh-approval", "--workspace", str(self.settings.yuque_workspace)]
        if self.settings.yqa_repo:
            args += ["--repo", self.settings.yqa_repo]
        code, data, raw = self._yqa(args, timeout=TIMEOUT_PLAN)
        if code != 0:
            return ToolResult(
                status="error",
                summary=f"刷新《审批结果》失败（退出码 {code}）：{_tail(raw)}",
                data={"exit_code": code, "output": _tail(raw)},
            )
        count = (data or {}).get("count") if isinstance(data, dict) else None
        return ToolResult(
            status="ok",
            summary=f"《审批结果》已刷新（{count if count is not None else '?'} 条）",
            data=data,
        )

    # ---- 审批结果（本地账本，只读）------------------------------------
    def _approval_status(self, payload: dict[str, Any]) -> ToolResult:
        from . import notify

        try:
            root = self.settings.approval_dir()
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        ledger = notify.read_ledger(root)
        if not ledger:
            return ToolResult(
                status="ok",
                summary="账本还是空的（还没有审批结束的申请）",
                data={"counts": {}, "entries": [], "ledger": str(root / "ledger.jsonl")},
            )
        want = _optional_str(payload, "outcome") or "all"
        counts: dict[str, int] = {}
        for entry in ledger:
            key = str(entry.get("outcome") or "?")
            counts[key] = counts.get(key, 0) + 1
        entries = []
        for entry in ledger:
            outcome = str(entry.get("outcome") or "?")
            if want != "all" and outcome != want:
                continue
            snapshot = entry.get("snapshot") or {}
            entries.append(
                {
                    "sqbh": entry.get("sqbh"),
                    "outcome": outcome,
                    "rooms": entry.get("rooms") or [],
                    "feedback": entry.get("feedback") or notify.text_of(snapshot, "feedback"),
                    "title": notify.normalize_title(notify.text_of(snapshot, "purpose")),
                    "date": notify.text_of(snapshot, "date"),
                    "slot": notify.text_of(snapshot, "period_start_text"),
                    "detected_at": entry.get("first_seen_ended"),
                }
            )
        label = {"approved": "已通过", "rejected": "已退回"}
        parts = [f"{label.get(k, k)} {v} 条" for k, v in sorted(counts.items())]
        return ToolResult(
            status="ok",
            summary="；".join(parts) or f"共 {len(ledger)} 条",
            data={"counts": counts, "entries": entries, "legend": label},
        )

    # ---- 写操作 --------------------------------------------------------
    def _crb_plan(self, payload: dict[str, Any]) -> ToolResult:
        mode = _require_choice(payload, "mode", ("preview", "save", "submit"))
        relative = _optional_str(payload, "path") or "plan.json"
        path = _resolve_within(self._outbox(), relative)
        if not path.is_file():
            return ToolResult(
                status="error",
                summary=f"找不到清单文件 {relative}；先用 yqa_refresh_plan 或让用户放一份进来。",
            )
        args = ["plan", "--file", str(path), "--json"]
        if payload.get("allow_overlap") is True:
            args.append("--allow-overlap")
        if mode == "save":
            args.append("--save")
        elif mode == "submit":
            args += ["--submit"]

        code, data, raw = self._crb(args, timeout=TIMEOUT_PLAN)
        if code != 0 and data is None:
            return _failure(f"crb plan 失败（退出码 {code}）：{_tail(raw)}", code, raw)
        payload_out = data if isinstance(data, dict) else {"assignments": data}
        assignments = payload_out.get("assignments") or []
        results = payload_out.get("results") or []
        blocked = [a for a in assignments if isinstance(a, dict) and a.get("status") != "ok"]
        ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
        if mode == "preview":
            summary = f"方案 {len(assignments)} 条，其中 {len(blocked)} 条没能排上（未写入系统）"
        else:
            verb = "存为草稿" if mode == "save" else "正式提交"
            summary = f"{len(assignments)} 条里 {ok_count} 条已{verb}，{len(blocked)} 条没排上"
        return ToolResult(
            status="ok" if code == 0 else "error",
            summary=summary,
            risk="dangerous",
            data={
                "mode": mode,
                "assignments": assignments,
                "results": results,
                "status_legend": _ASSIGNMENT_LEGEND,
            },
        )

    def _crb_borrow_action(self, payload: dict[str, Any]) -> ToolResult:
        sqbh = _require_sqbh(payload)
        action = _require_choice(payload, "action", ("submit", "withdraw", "delete", "edit"))
        args = ["borrow", action, "--sqbh", sqbh]
        if action == "edit":
            data = _require_str(payload, "data")
            try:
                if not isinstance(json.loads(data), dict):
                    raise ToolError("edit 的 data 必须是 JSON 对象")
            except ValueError as exc:
                raise ToolError(f"edit 的 data 不是合法 JSON：{exc}") from exc
            args += ["--data", data]
            if payload.get("draft") is True:
                args.append("--draft")
        code, data, raw = self._crb([*args, "--json"])
        label = {"submit": "提交", "withdraw": "撤回", "delete": "删除", "edit": "修改"}[action]
        if code != 0:
            return _failure(f"{sqbh} {label}失败（退出码 {code}）：{_tail(raw)}", code, raw)
        ok = bool((data or {}).get("ok")) if isinstance(data, dict) else code == 0
        message = (data or {}).get("msg") if isinstance(data, dict) else None
        return ToolResult(
            status="ok" if ok else "error",
            summary=f"{sqbh} {label}{'成功' if ok else '未成功'}：{message or ''}".strip("："),
            risk="dangerous",
            data=data,
        )


#: 给 LLM 的状态字典——它要能自己把 SHZT 读成人话，而不是回来问我们。
_SHZT_LEGEND = {
    "00": "草稿（可以提交 / 编辑）",
    "1": "已撤回（可以删除 / 重新提交 / 编辑）",
    "65": "待审核（等指导老师初审）",
    "99": "已通过",
}

_ASSIGNMENT_LEGEND = {
    "ok": "已排上教室",
    "duplicate": "与已有申请时间重叠，已跳过（这就是去重）",
    "no_room": "该时段没有可用教室",
    "too_small": "有教室但容量都不够",
    "error": "查询出错",
}
