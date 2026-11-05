"""追问决策：根据"哪条主张还没被证明"，决定下一刀问哪里。

这是把"按轮次追问"换成"按缺口追问"的那一层。
LLM 只负责：判断每轮回答证明了哪些主张（带引用）、指出还缺什么、把问题说成人话。
代码负责：主张清单、缺口筛选、终止条件、引用校验。
"""

import json
import re

from .exemplars import as_prompt
from .settings import temp as _t
from .llm import LLMError, chat

PROBE_PROMPT = """你是产品经理面试官，正在追问。

【这段经历】{name}
【当前方向】{direction}
【待验证的主张】
{claims}

【完整对话（按顺序，Q1/A1 是第一轮）】
{history}

{exemplars}

【候选人刚才的回答】
{last_answer}

【任务】
1. 逐条判断：刚才这段回答，**证明了哪几条主张**？
   - proven：说清了（给 evidence，逐字引用他的原话）
   - partial：沾边但不够
   - unproven：完全没涉及
   **注意**：先讲背景、表达啰嗦、暂时没讲到结果，都算 partial，**不算 unproven**。
2. 找出**最该补的一条缺口**（未证明且风险最高的那条）。
3. 针对这个缺口提一个追问（≤45 字）。**先接住他刚才说的话，再问**：
   - 开头用「你说…/你提到…」确认一句，再往下压；
   - 如果他已经明确承认"没有灰度 / 无法排除 / 没有对照组 / 记不清"，**不要再要求他"怎么排除"**，
     改为问"那你当时实际是怎么判断这个改动有效的"；
   - **只问"他当时实际怎么做的"，不问"应该怎么做 / 最佳实践 / 阈值多少才算对"**（那是考知识，不是验证经历）。

   - **只在「当前方向」内提问**：这个方向没聊完就别跳到别的事；
   - **换个角度问**：如果下面列出的角度已经问过两次以上，就换一个没问过的角度。
   已用角度：{used}
   可选角度：口径 / 归因 / 角色 / 取舍 / 边界 / 机制 / 验证 / 贡献

   **追问方向要跟着主张类型走**：
   - result（有数字的结果）→ 口径、基线、对照、归因
   - **design（设计/流程改动）→ 设计依据、当时的约束、取舍、什么情况下会失效；禁止问"拿什么指标证明"**
   - judgment（判断）→ 凭什么这么判断、排除了哪些其他解释
   - action（动作）→ 具体做了哪几件事、哪部分是你亲手做的
   - tradeoff（取舍）→ 备选方案、判断标准、代价
   - 必须指向缺口，不要换话题；
   - 可以引用他的原话（「」括起来），但引用后要指向判断；
   - 不要问行业默认做法（7 天窗口、A/B 各 50%），不要问简历上已写的事实。

只输出 JSON：
{{
  "statuses": [
    {{"index": 0, "status": "proven", "evidence": "对话成功率从72%提到89%"}},
    {{"index": 1, "status": "unproven", "evidence": ""}}
  ],
  "all_proven": false,
  "gap": {{"index": 1, "missing": "没说这个数怎么算的"}},
  "question": "你说成功率到 89%，这个数具体是怎么统计出来的？"
}}"""


def _norm(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", s or "").lower()


def next_probe(name: str, claims: list, history: list[dict], last_answer: str,
               used_angles: list | None = None, direction: str = "") -> dict:
    """返回 {statuses, all_proven, gap, question}；失败返回 {}。"""
    if not claims:
        return {}
    claim_lines = "\n".join(
        f'{i}. [{c.get("type","")}] {c.get("text","")}（风险：{c.get("risk","")}）'
        for i, c in enumerate(claims)
    )
    hist_lines = "\n".join(
        f'Q{i+1}: {h.get("q","")}\nA{i+1}: {h.get("a","")}' for i, h in enumerate(history)
    ) or "（还没有）"

    try:
        raw, _ = chat([{"role": "user", "content": PROBE_PROMPT.format(
            name=name, direction=direction or "（整段经历）",
            claims=claim_lines, history=hist_lines,
            exemplars=as_prompt(), last_answer=last_answer,
            used="、".join(used_angles or []) or "（还没有）"
        )}], temperature=_t('probe', 0.0), json_mode=True)
    except LLMError:
        return {}

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = text[text.find("{"): text.rfind("}") + 1]
        try:
            data = json.loads(m)
        except json.JSONDecodeError:
            return {}

    statuses = {}
    for s in (data.get("statuses") or []):
        if not isinstance(s, dict):
            continue
        try:
            idx = int(s.get("index"))
        except (TypeError, ValueError):
            continue
        st = str(s.get("status", "")).strip().lower()
        if st not in ("proven", "partial", "unproven"):
            continue
        ev = str(s.get("evidence", "")).strip()
        if ev and _norm(ev) not in _norm(last_answer):     # 证据查不到 → 不算 proven
            st = "partial" if st == "proven" else st
            ev = ""
        statuses[idx] = {"status": st, "evidence": ev}

    all_proven = bool(data.get("all_proven")) and all(
        statuses.get(i, {}).get("status") == "proven" for i in range(len(claims))
    )

    gap = data.get("gap") or {}
    try:
        gap_idx = int(gap.get("index"))
    except (TypeError, ValueError):
        gap_idx = -1
    if not (0 <= gap_idx < len(claims)):
        gap_idx = next((i for i in range(len(claims))
                        if statuses.get(i, {}).get("status") != "proven"), -1)

    # 连贯性：如果有一条主张已经聊到一半（partial），先把这条聊完，别跳题
    partials = [i for i in range(len(claims))
                if statuses.get(i, {}).get("status") == "partial"]
    if partials and gap_idx not in partials:
        gap_idx = partials[0]

    return {
        "statuses": statuses,
        "all_proven": all_proven,
        "gap": {"index": gap_idx, "missing": str(gap.get("missing", ""))[:40]} if gap_idx >= 0 else None,
        "question": str(data.get("question", "")).strip()[:60],
    }
