"""评分守则：不靠模型自觉，用代码兜底。

参考北森"准确/全面/细致"的可判思路 + 开源项目 ai-interview-coach 的 guards：
模型给出的判定，要先过一遍这些硬规则，才允许进入分数。
"""

import re

MIN_CHARS_FULL = 25          # 少于这个字数，最多只能给"沾边"（中文 25 字已经很少）
DETAIL_MARKERS = ("%", "pp", "倍", "万", "千", "天", "周", "月", "元", "人", "次")


def specificity(answer: str) -> float:
    """细致度（0–1，纯代码算）：有没有数字、口径词、对照词。

    这是"可判"的部分——不依赖模型，同一段话永远算同一个分。
    """
    a = answer or ""
    score = 0.0
    if re.search(r"\d+(?:\.\d+)?\s*%", a):
        score += 0.4                                   # 有百分比
    elif re.search(r"\d", a):
        score += 0.2                                   # 至少有个数
    if any(m in a for m in DETAIL_MARKERS):
        score += 0.2
    if re.search(r"(基线|之前|上线前|对照|分组|A/B|环比|同比)", a):
        score += 0.2                                   # 有对比意识
    if re.search(r"(分母|时间窗|样本|口径|定义)", a):
        score += 0.2                                   # 有口径意识
    return round(min(score, 1.0), 2)


def has_number(answer: str) -> bool:
    return bool(re.search(r"\d", answer or ""))


def apply_leakage(question: str, elements: list[dict]) -> tuple[list[dict], list[str]]:
    """泄露答案：数字是面试官在提问里给的，候选人复述它不能算证据。

    例：问"为什么从 15% 到 19%"，候选人答"从 15% 提升到 19%"，不能因为提到数字就给命中。
    """
    notes = []
    q = question or ""
    out = []
    for e in elements:
        e = dict(e)
        quote = e.get("quote") or ""
        nums = [n for n in re.findall(r"\d+(?:\.\d+)?%?", quote) if len(n) >= 2]
        leaked = [n for n in nums if n in q]
        if leaked and e["verdict"] == "hit":
            e["verdict"] = "partial"
            e["why"] = (e.get("why") or "") + f"（{leaked[0]} 是提问里给的，复述不算证据）"
            if "泄露答案" not in notes:
                notes.append("泄露答案")
        out.append(e)
    return out, notes


def apply_guards(answer: str, elements: list[dict], total_hint: int = 0) -> tuple[list[dict], list[str]]:
    """对模型的要素判定做封顶。返回 (修正后的要素, 触发了哪些守则)。

    规则：
    1. 回答 < 25 字 → 任何 hit 降为 partial（短答不能拿满分）
    2. 表述里带"数字/口径"要求的要素，回答里没数字 → hit 降为 partial
    """
    notes = []
    short = len(re.sub(r"\s+", "", answer or "")) < MIN_CHARS_FULL
    has_num = has_number(answer)
    out = []
    for e in elements:
        e = dict(e)
        if e["verdict"] == "hit":
            if short:
                e["verdict"] = "partial"
                e["why"] = (e.get("why") or "") + "（回答偏短，封顶为沾边）"
                if "短答封顶" not in notes:
                    notes.append("短答封顶")
            elif not has_num and any(k in (e.get("name", "") + e.get("hint", ""))
                                     for k in ("数字", "口径", "基线", "结果", "指标")):
                e["verdict"] = "partial"
                e["why"] = (e.get("why") or "") + "（没有数字支撑，封顶为沾边）"
                if "无数字封顶" not in notes:
                    notes.append("无数字封顶")
        out.append(e)
    return out, notes
