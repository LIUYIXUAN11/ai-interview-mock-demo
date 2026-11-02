"""单题体检：拿**同一套五维信号**去对照一句话。

和报告的区别只有一个：粒度。
- 这里评「这句话命中了哪几个维度的信号」；
- 报告评「整场表现落在这五维的什么位置」。

每个维度给三种判定：
- hit  命中该维度的高分信号
- miss 踩到该维度的扣分信号，或该答的没答
- na   这句话根本评不了这一维（比如一句话谈不上"学习敏锐度"）
"""

import json
import re

from . import guards, rubric
from .llm import LLMError, chat

PROMPT = """你是面试教练，正在给候选人的**一句话回答**做体检。

注意：这里**不打能力维度分**（那是整场面试的结论），只检查一件事——
**针对这道题，他该说到的点，说到了没有。**

【面试官的问题】
{question}

【候选人的回答】
{answer}

【这道题该覆盖的要素】（[S/T/A/R] 是 STAR 里的位置，★ 是这道题的专属考点）
{elements}

【任务】
0. **先抽事实**：把回答里所有**具体事实**列出来（数字、约束、做法、结果、角色、时间），
   写进 facts 数组。这一步只摘取，不评价。
1. 然后**对照 facts** 逐个要素判定，三档：
   - hit：说清了
   - partial：提了但不够（例如给了数字但没给口径）
   - miss：完全没提
   **判定要基于 facts，不要要求原话字面出现**：
   - 回答里说了"开发周期两个月、我们只有三周"→ 这就是「当时的约束」，算命中；
   - 回答里说了"改成先用现有埋点做一版"→ 这就是「代价与兜底」，算命中；
   - 能明确推断出来的给 partial；要写清"由 X 推断"。
**对齐要求**：elements 必须与上面清单**逐条一一对应**——name 原样照抄，数量一致，
不许增、删、改名或换顺序（系统按名字对齐，改了就对不上）。

2. **每个要素必须给 quote**：从候选人回答里**逐字复制**支撑这个判定的片段（6–30 字）；
   判定 miss 时 quote 留空字符串；
3. 从回答里挑出**最该改的一小段原文**（8–40 字，逐字），并给两件事：
   - problem：这句话**具体错在哪**（20 字内）
   - fix：**怎么改**（30 字内，可执行，不要"加强逻辑"这种空话）
4. 从固定根因库里选**一个**：{causes}；
5. 给一版骨架（3–5 步的说话顺序）和一句示范开头。

【硬规则】
- quote 和标黄片段必须逐字来自候选人回答，系统会回查，编的会被作废；
- 如果所有要素都 hit，root_cause 留空字符串，one_line 写"这一版没毛病"；
- 不评价院校、性别、年龄、人格；说人话。

只输出 JSON：
{{
  "one_line": "一句话点评，20 字内",
  "facts": ["回答里的具体事实，逐条摘，不评价"],
  "elements": [
    {{"name": "情境与影响谁", "verdict": "hit", "why": "说清了谁受影响", "quote": "客服一天要改两百份", "fix": ""}},
    {{"name": "目标与优先级", "verdict": "miss", "why": "没讲为什么先做这个", "quote": "", "fix": "补一句：这块卡点占了售后的 33%"}}
  ],
  "highlight": {{"text": "回答里的逐字片段", "problem": "只说了结果，没给基线", "fix": "先报基線 12.3%，再说现在的 15.1%"}},
  "root_cause": "数据缺失",
  "skeleton": ["先给结论", "再给基线", "然后给变化", "最后给归因"],
  "demo_opening": "上线前是 12.3%，上线后 15.1%——"
}}
"""

def rubric_norm(t: str) -> str:
    return re.sub(r"[\s，。、；：？！“”‘’（）《》【】,.!?;:\"'()\[\]{}—…·]", "", t or "").lower()


_VERDICTS = ("hit", "partial", "miss")

_KEEP = re.compile(r"[^\s，。、；：？！“”‘’（）《》【】,.!?;:\"'()\[\]{}<>—\-…·%]")


def _skeleton_map(text: str):
    chars, idx = [], []
    for i, ch in enumerate(text or ""):
        if _KEEP.match(ch):
            chars.append(ch.lower())
            idx.append(i)
    return "".join(chars), idx


def locate(text: str, fragment: str):
    """在原文里定位片段（精确优先，其次忽略空白/标点），返回 (start, end) 或 None。"""
    if not text or not fragment:
        return None
    i = text.find(fragment)
    if i >= 0:
        return i, i + len(fragment)

    flat_text, idx = _skeleton_map(text)
    flat_frag, _ = _skeleton_map(fragment)
    if not flat_frag or len(flat_frag) < 6:
        return None
    j = flat_text.find(flat_frag)
    if j < 0:
        return None
    return idx[j], idx[j + len(flat_frag) - 1] + 1


def _extract_json(raw: str):
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
    for c in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        if not c:
            continue
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


def check(question: str, answer: str, stage: str = "project", intent: str = "",
          checkpoints: list[str] | None = None):
    """跑一次体检。失败返回 None（界面降级：提示重试，不阻塞）。

    返回：
      elements: [{name, star, verdict, why, quote, verified}]
      score/total：三档折算后的要素命中数
      highlight: {text, span, problem, fix, verified}
    """
    answer = (answer or "").strip()
    if len(answer) < 10:
        return None

    if checkpoints:                    # 题自带考察点：直接用它，不按类型查表（根治"贴错标签"）
        spec = {"label": "题目自带", "elements": [{"name": str(c)[:20], "star": "", "hint": ""}
                                                  for c in checkpoints[:6]]}
    else:
        spec = rubric.elements_for(intent)
    prompt = PROMPT.format(
        question=question or "（上一轮提问）",
        answer=answer,
        elements=rubric.describe_elements(intent),
        causes=" / ".join(rubric.ROOT_CAUSES),
    )
    try:
        raw, _ = chat([{"role": "user", "content": prompt}], temperature=0.0, json_mode=True)
    except LLMError:
        return None

    data = _extract_json(raw)
    if not isinstance(data, dict):
        return None

    returned = [e for e in (data.get("elements") or []) if isinstance(e, dict)]
    used = set()

    def _match(name: str):
        """先精确匹配名字，再按包含关系匹配，最后按顺序兜底——模型可能换说法或加一条。"""
        n = rubric_norm(name)
        for i, e in enumerate(returned):
            if i in used:
                continue
            en = rubric_norm(str(e.get("name", "")))
            if en == n:
                used.add(i)
                return e
        for i, e in enumerate(returned):
            if i in used:
                continue
            en = rubric_norm(str(e.get("name", "")))
            if len(n) >= 4 and (n in en or en in n):
                used.add(i)
                return e
        for i, e in enumerate(returned):
            if i not in used:
                used.add(i)
                return e
        return {}

    elements = []
    for meta in spec["elements"]:
        e = _match(meta["name"])
        verdict = str(e.get("verdict", "")).strip().lower()
        if verdict not in _VERDICTS:
            verdict = "miss"
        quote = str(e.get("quote", "")).strip()
        span = locate(answer, quote) if quote else None
        elements.append({
            "name": meta["name"],
            "star": meta.get("star", ""),
            "hint": meta.get("hint", ""),
            "verdict": verdict,
            "why": str(e.get("why", ""))[:40],
            "quote": answer[span[0]:span[1]] if span else (quote if verdict == "miss" else ""),
            "span": span,
            "fix": str(e.get("fix", ""))[:40],
            "verified": span is not None,
        })

    elements, leak_notes = guards.apply_leakage(question, elements)
    elements, guard_notes = guards.apply_guards(answer, elements)
    guard_notes = leak_notes + guard_notes
    score = rubric.score_of([e["verdict"] for e in elements])
    detail = guards.specificity(answer)          # 细致度：纯代码算，可复现

    hl = data.get("highlight") or {}
    frag = str(hl.get("text", "")).strip()
    span = locate(answer, frag)
    highlight = {
        "text": answer[span[0]:span[1]] if span else frag,
        "span": span,
        "problem": str(hl.get("problem") or hl.get("why") or "")[:50],
        "fix": str(hl.get("fix", ""))[:60],
        "verified": span is not None,
    }

    tag = str(data.get("root_cause", "")).strip()
    missed = [e for e in elements if e["verdict"] == "miss"]
    if missed and tag not in rubric.ROOT_CAUSES:
        tag = "结构缺失"
    if not missed and not any(e["verdict"] == "partial" for e in elements):
        tag = ""

    return {
        "one_line": str(data.get("one_line", ""))[:40],
        "facts": [str(f)[:60] for f in (data.get("facts") or [])][:8],
        "elements": elements,
        "score": score,
        "total": len(elements),
        "detail": detail,                        # 可判：细致（0–1）
        "coverage": round(score / len(elements), 2) if elements else 0.0,   # 可判：全面（命中率）
        "guards": guard_notes,
        "highlight": highlight,
        "root_cause": {"tag": tag, "failed_dimension": spec["label"]},
        "skeleton": [str(x)[:40] for x in (data.get("skeleton") or [])][:5],
        "demo_opening": str(data.get("demo_opening", ""))[:60],
    }
