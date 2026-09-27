"""提示词的装载。

提示词写在 ``prompts/*.md`` 里，不写成 Python 字符串：它是**规矩**，
要有版本、要能被 diff 出来、要能被守卫测试盯着（见 AGENTS.md 的「交付前自检」）。
"""

from __future__ import annotations

from datetime import datetime
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

_DIR = Path(__file__).parent / "prompts"

#: 服务器在别的时区也不影响：「今天是几号」「这周是哪一周」必须按北京时间算，
#: 否则跨零点时会算错一天（申请周期就是按周切的）。
TZ = ZoneInfo("Asia/Shanghai")


@cache
def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8").strip()


def system_prompt(now: datetime | None = None) -> str:
    """拼出这一轮的 system prompt。

    动态部分只有一行「今天是几号、周几」——模型没法自己知道这个，
    而几乎每个日期相关的判断（「下周三」「本周之内」）都要用到它。
    """
    today = (now or datetime.now(TZ)).astimezone(TZ)
    weekday = "一二三四五六日"[today.weekday()]
    header = f"今天是 {today.date().isoformat()}（周{weekday}），时区 Asia/Shanghai。"
    return f"{header}\n\n{_read('system.md')}"


def prompt_path(name: str = "system.md") -> Path:
    return _DIR / name
