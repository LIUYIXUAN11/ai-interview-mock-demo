"""追问审核：线上加一道"审稿"，不合格就重写一次。

对应报告里的 Agent C（检查追问是否合理）——只是放在**线上单次调用**，不是多智能体编排。
代价：每问多一次小调用（约 ¥0.003）。
"""

import json
import re

from .llm import LLMError, chat

REVIEW_PROMPT = """你是面试官团队的审稿人。判断下面这个追问**能不能发出去**。

【这段经历】{name}
【最近一轮问答】
问：{last_q}
答：{last_a}
【待审的问题】
{question}

【四条硬标准，任一条不满足就判不合格】
1. **问判断**：问的是"你怎么想的/凭什么/怎么知道的"，不是核对事实或常识；
2. **有抓手**：问题里带了对方说过的具体内容（词、数字、做法），不是泛泛而问；
3. **不越界**：没有对候选人没说过的事提要求（比如只改了设计，却要"拿指标证明"）；
4. **一问一事**：只问一个点，不是"……？还有……？"

只输出 JSON：
{{"ok": true, "reason": "", "fix_hint": ""}}"""


def check(question: str, name: str, last_q: str, last_a: str) -> dict:
    """返回 {ok, reason, fix_hint}。调用失败时默认放行（不阻塞主流程）。"""
    try:
        raw, _ = chat([{"role": "user", "content": REVIEW_PROMPT.format(
            name=name, last_q=last_q or "（无）", last_a=last_a or "（无）", question=question
        )}], temperature=0.0, json_mode=True)
    except LLMError:
        return {"ok": True, "reason": "", "fix_hint": ""}

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        m = text[text.find("{"): text.rfind("}") + 1]
        try:
            d = json.loads(m)
        except json.JSONDecodeError:
            return {"ok": True, "reason": "", "fix_hint": ""}

    return {
        "ok": bool(d.get("ok", True)),
        "reason": str(d.get("reason", ""))[:40],
        "fix_hint": str(d.get("fix_hint", ""))[:60],
    }
