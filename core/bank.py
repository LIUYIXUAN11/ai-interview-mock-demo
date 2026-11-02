"""通用面题库（来自 AIPM-Wiki，CC BY-NC-SA 4.0，仅非商业使用）。

设计：题库是**数据**，不是代码里的字符串。
每题自带考察点（checkpoints）和预埋追问（followups），所以出题时不需要模型现编标准。
"""

import json
import os
import random

ROOT = os.environ.get("INTERVIEW_DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANK = os.path.join(ROOT, "question_bank", "bank.json")

CATEGORY_LABEL = {
    "concept": "概念题",
    "design": "设计题",
    "case": "案例题",
    "behavioral": "行为面",
}


def load() -> dict:
    if not os.path.exists(BANK):
        return {"items": [], "count": 0}
    with open(BANK, encoding="utf-8") as f:
        return json.load(f)


def items(category: str = "") -> list[dict]:
    data = load().get("items", [])
    return [i for i in data if not category or i["category"] == category]


def pick(n: int = 3, category: str = "", seed: int | None = None) -> list[dict]:
    """挑 n 道题；同一张试卷内不重复。"""
    pool = items(category)
    if not pool:
        return []
    rng = random.Random(seed) if seed is not None else random
    return rng.sample(pool, min(n, len(pool)))


def attribution() -> str:
    d = load()
    return d.get("attribution", "")
