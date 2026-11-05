"""纯提示词版（A/B 对比用）：不拆主张、不算缺口，只靠一段提示词出题。

和现有版本的区别：
  - 不给"待验证主张"，不给"要素清单"；
  - 只给：这段经历原文 + JD + 最近几轮对话 + 已经问过的问题；
  - 模型自己决定问什么，以及什么时候该停（should_stop）。

代码只做两件事：拼上下文、把超长历史截断。
"""

from .settings import temp as _t
from .llm import LLMError, chat

PROMPT = """你是面过几百个产品经理的资深面试官，正在深挖候选人简历里的这一段经历。

【这段经历】
{name}
{context}

【目标岗位（可能为空）】
{jd}

【刚才的对话】
{history}

【已经问过的问题｜不要重复】
{asked}

你的任务：**像真人一样**，问出你现在最想问的那一个问题。

一位资深面试官的做法：
- 他说完，你心里会有一个"我不太信"的点——问题就指向那里；
- 只问一件他刚才没讲清、而且**会影响你判断**的事；
- 如果他讲清楚了，就往下走一层，或者换个角度（他做了什么 / 怎么判断 / 怎么验证 / 边界在哪）；
- 不问简历上已经写明的事实，不问行业默认做法，不问"能详细说说吗"这种空话；
- 语气口语，一句话，25–45 字。

如果你认为这段经历已经问透了，把 should_stop 设为 true，question 留空。

只输出 JSON：
{{"question": "你的问题", "why": "你为什么想问这个（25 字内）", "should_stop": false}}"""


def next_question(name: str, context: str, jd: str, history: list[dict], asked: list[str],
                  variant: str = "v1") -> dict:
    """返回 {question, why, should_stop}；失败返回 {}。"""
    hist = "\n".join(
        f"Q{i+1}: {h.get('q','')}\nA{i+1}: {h.get('a','')}" for i, h in enumerate(history[-3:])
    ) or "（这是第一个问题）"
    ask = "\n".join(f"- {q}" for q in asked[-8:]) or "（还没有）"

    try:
        from .simple_variants import VARIANTS
        tpl = VARIANTS.get(variant, PROMPT)
        raw, _ = chat([{"role": "user", "content": tpl.format(
            name=name, context=(context or "")[:1200], jd=(jd or "（未提供）")[:600],
            history=hist, asked=ask)}], temperature=_t("question", 0.7), json_mode=True)
    except LLMError:
        return {}

    import json
    import re
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        m = text[text.find("{"): text.rfind("}") + 1]
        try:
            d = json.loads(m)
        except json.JSONDecodeError:
            return {}
    return {"question": str(d.get("question", "")).strip()[:60],
            "why": str(d.get("why", ""))[:40],
            "should_stop": bool(d.get("should_stop", False))}
