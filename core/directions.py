"""方向层：把一整段实习/项目经历，切成几个"可独立练习的方向"。

为什么要切：
  一段 6 个月的实习里通常有 3–5 件不同的事（每次有自己的问题、动作、结果）。
  整段当成一个话题 → 追问会在大经历里跳来跳去，像"翻简历"。

方向 = 一个小经历（有自己的问题 + 动作 + 结果），用户可以随时换方向。
"""

import json
import re

from .settings import temp as _t
from .llm import LLMError, chat

PROMPT = """你在拆解一段简历经历，目的是让候选人可以**分方向练习面试**。

【经历】{name}
【简历原文】
{context}

【什么是一个方向】
一个方向 = 一件可以独立讲清楚的事，必须同时有：问题、动作、结果。

【数量与合并规则（最重要）】
- 先判断这些内容**彼此有没有关系**：
  · 共用同一个结果/指标/改版 → **合并成一个方向**；
  · 内容之间没有明显关系（比如既做了判责、又做了完全另一条业务）→ **拆成不同方向**；
- 数量 **1–4 个，最多 4 个**，不要为了凑数硬拆；
- 只有 1 件完整的事，就只给 1 个方向，这是允许且常见的；
- 只是"负责 X 模块"这类职责描述、或"参与/协助"没有独立结果的，不要单独成方向。

【硬规则】
- 每个方向必须给 source：简历原文里**逐字存在**的一句话（8–40 字），系统会回查，查不到就丢弃；
- 只用简历里出现过的词和数字，不许编；
- 只输出 JSON。

{{
  "directions": [
    {{"name": "注册转化提升", "problem": "存量用户误入注册流程 12.8%",
      "action": "重构入口 + 调整弹窗时机", "result": "登录转化 30%→40%",
      "source": "推动登录转化率由 30%提升至 40%"}}
  ]
}}"""

def _norm(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", s or "").lower()


def split(name: str, context: str) -> list[dict]:
    """返回方向列表；失败返回空列表（调用方退回"整段一个方向"）。"""
    context = (context or "").strip()
    if not context:
        return []
    try:
        raw, _ = chat([{"role": "user", "content": PROMPT.format(name=name, context=context[:1500])}],
                      temperature=_t("directions", 0.0), json_mode=True)
    except LLMError:
        return []

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = text[text.find("{"): text.rfind("}") + 1]
        try:
            data = json.loads(m)
        except json.JSONDecodeError:
            return []

    out = []
    for d in (data.get("directions") or [])[:4]:
        if not isinstance(d, dict):
            continue
        nm = str(d.get("name", "")).strip()
        src = str(d.get("source", "")).strip()
        if not nm or not src:
            continue
        if _norm(src) not in _norm(context):       # 原文依据查不到 → 丢弃
            continue
        out.append({
            "name": nm[:20],
            "problem": str(d.get("problem", ""))[:60],
            "action": str(d.get("action", ""))[:60],
            "result": str(d.get("result", ""))[:60],
            "source": src[:60],
            "rounds": 0,
        })
    return out
