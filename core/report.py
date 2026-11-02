"""评估报告：结构化打分 + 逐条溯源校验。

这是整个作品集里最值钱的一个功能点，原因很直接：
LLM 写评语最大的问题是「脑补」——它会替候选人补上没说过的经历，然后给出很漂亮的评价。
产品的解法不是「prompt 里写不许编」，而是**让每条结论都必须指回原文，再用代码验证这条引用真的存在**。
验证不过的依据，直接标红作废。这就是「事前约束 + 事后核验」在工程上的落地。
"""

import json
import re

from . import prompts
from .llm import LLMError, chat

_PUNCT = "，。、；：？！“”‘’（）《》【】,.!?;:\"'()[]{}<>—-…·% "
_ELLIPSIS = re.compile(r"…{1,}|\.{3,}")


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch not in _PUNCT).lower()


def _extract_json(raw: str):
    """模型偶尔会包 markdown 代码块或加解释，这里做容错提取。"""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def quote_exists(quote: str, user_texts: list[str]) -> bool:
    """引用必须是候选人原话的逐字片段；支持「……这样」两段式引用。"""
    if not quote or len(_norm(quote)) < 6:
        return False
    haystack = _norm("".join(user_texts))
    segments = [s for s in _ELLIPSIS.split(quote) if len(_norm(s)) >= 4]
    if not segments:
        return False
    return all(_norm(s) in haystack for s in segments)


def _clamp_score(v, default=60):
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def verify(report: dict, user_texts: list[str]) -> dict:
    """给报告打上「哪条依据可溯源、哪条是编的」的标记，并统计溯源率。"""
    total = valid = 0

    def check(items, key="quote"):
        nonlocal total, valid
        for item in items or []:
            if not isinstance(item, dict):
                continue
            q = item.get(key, "")
            ok = quote_exists(q, user_texts)
            item["verified"] = ok
            total += 1
            valid += 1 if ok else 0

    for dim in report.get("dimensions") or []:
        quotes = dim.get("quotes") or []
        checked = []
        for q in quotes:
            ok = quote_exists(q, user_texts)
            checked.append({"text": q, "verified": ok})
            total += 1
            valid += 1 if ok else 0
        dim["quotes"] = checked

    check(report.get("strengths"))
    check(report.get("gaps"))

    report["trace"] = {
        "total": total,
        "valid": valid,
        "rate": round(valid / total, 2) if total else 0.0,
    }
    return report


def _normalize(report: dict) -> dict:
    """补齐缺失字段，保证 UI 不会因为模型少写一个 key 就崩。"""
    report.setdefault("overall_score", 60)
    report.setdefault("verdict", "边缘")
    report.setdefault("summary", "")
    report.setdefault("dimensions", [])
    report.setdefault("strengths", [])
    report.setdefault("gaps", [])
    report.setdefault("next_actions", [])

    report["overall_score"] = _clamp_score(report["overall_score"])

    # 按 schema 里的 5 个维度对齐：模型少给的补 0，多给的丢掉
    by_name = {d.get("name", ""): d for d in report["dimensions"] if isinstance(d, dict)}
    dims = []
    for meta in prompts.REPORT_DIMENSIONS:
        name = meta["name"]
        d = by_name.get(name, {})
        dims.append(
            {
                "name": name,
                "weight": meta["weight"],
                "score": _clamp_score(d.get("score"), 60),
                "comment": d.get("comment", "（本维度未给出评价）"),
                "hit": [str(x)[:40] for x in (d.get("hit") or [])][:3],
                "miss": [str(x)[:40] for x in (d.get("miss") or [])][:3],
                "quotes": d.get("quotes", []) or [],
            }
        )
    report["dimensions"] = dims
    return report


def generate_report(messages: list[dict]) -> dict:
    """生成报告。返回 {"ok": bool, "report": dict|None, "raw": str, "error": str}"""
    user_texts = [
        m["content"] for m in messages
        if m["role"] == "user" and not m["content"].startswith("〔")
    ]
    if len(user_texts) < 2:
        return {"ok": False, "report": None, "raw": "", "error": "对话内容太少，无法生成有依据的报告（至少需要 2 轮回答）"}

    def _label(m):
        # 〔…〕开头的不是候选人说的，是系统动作（如「跳过本环节」），别算到候选人头上
        if m["content"].startswith("〔"):
            return "系统"
        return "候选人" if m["role"] == "user" else "面试官"

    transcript = "\n".join(f"{_label(m)}：{m['content']}" for m in messages)
    convo = f"{prompts.REPORT_INSTRUCTION}\n\n【面试全文】\n{transcript}"

    raw = ""
    for attempt in range(2):
        try:
            # 第一次高温度让评价有信息量，第二次调到 0 保证格式
            raw, _ = chat(
                [{"role": "user", "content": convo}],
                temperature=0.4 if attempt == 0 else 0.0,
                json_mode=True,
            )
        except LLMError as e:
            return {"ok": False, "report": None, "raw": raw, "error": str(e)}

        data = _extract_json(raw)
        if data and isinstance(data, dict):
            return {"ok": True, "report": verify(_normalize(data), user_texts), "raw": raw, "error": ""}

    return {"ok": False, "report": None, "raw": raw, "error": "模型没有返回合法的 JSON 报告"}
