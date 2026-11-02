"""体检用的「要素清单」，按问题类型给，每条挂 STAR 标签。

为什么不在这里评四维：
维度（产品思维/逻辑/沟通/数据）是**整场**属性——需要跨轮证据（怎么回应追问、有没有自相矛盾）。
拿一句话判维度，等于用一行代码评价一个系统。单句能判的只有一件事：
**这道题该说到的点，你说到了没有。**

四维只出现在整场层（mini.score）。
"""

import re

ROOT_CAUSES = ("结构缺失", "数据缺失", "归因模糊", "角色不清", "逻辑跳跃", "表达冗余")

DIMENSION_TO_CAUSE = {
    "产品思维": "结构缺失",
    "逻辑分析": "逻辑跳跃",
    "沟通表达": "表达冗余",
    "数据驱动": "数据缺失",
}

# 三档：hit 命中 / partial 沾边 / miss 缺失
ELEMENT_SETS = {
    "why_do": {
        "label": "为什么做这个",
        "elements": [
            {"name": "情境与影响谁", "star": "S", "hint": "问题发生在什么场景、影响到谁"},
            {"name": "目标与优先级", "star": "T", "hint": "当时定的目标，为什么先做这个而不是别的"},
            {"name": "怎么验证是真需求", "star": "A", "hint": "数据/访谈/反馈，怎么确认不是拍脑袋"},
            {"name": "判断被验证了吗", "star": "R", "hint": "后来有没有数据或事实证明判断是对的"},
            {"name": "排除了其他解释", "star": "★", "hint": "有没有排除别的可能（不是不会用，是入口太深）"},
        ],
    },
    "why_this": {
        "label": "为什么选这个方案",
        "elements": [
            {"name": "当时的约束", "star": "S", "hint": "时间、人力、技术上的限制"},
            {"name": "方案要满足什么", "star": "T", "hint": "这个方案要达成的目标"},
            {"name": "方案对比与理由", "star": "A", "hint": "至少说得出一个被考虑过的其他做法，以及按什么判断选的它"},
            {"name": "代价与兜底", "star": "R", "hint": "选它的代价/风险，以及怎么兜"},
        ],
    },
    "how_verify": {
        "label": "结果怎么验证",
        "elements": [
            {"name": "主指标", "star": "R", "hint": "用哪个指标判断成功"},
            {"name": "基线", "star": "R", "hint": "改之前是多少"},
            {"name": "变化量", "star": "R", "hint": "改之后是多少"},
            {"name": "口径", "star": "R", "hint": "分母、时间窗、样本怎么定义"},
            {"name": "对照", "star": "R", "hint": "怎么排除同期其他因素"},
            {"name": "护栏", "star": "R", "hint": "有没有副作用，没达预期的部分"},
        ],
    },
}

DEFAULT_SET = {
    "label": "通用",
    "elements": [
        {"name": "背景", "star": "S", "hint": "说清是什么事"},
        {"name": "动作", "star": "A", "hint": "你具体做了什么"},
        {"name": "结果", "star": "R", "hint": "带来什么变化"},
        {"name": "边界", "star": "★", "hint": "哪部分是你做的、哪些不是你做的"},
    ],
}


def elements_for(intent: str) -> dict:
    """按问题类型取要素清单。intent 来自迷你面试（why_do / why_this / how_verify）。"""
    key = (intent or "").strip()
    if key in ELEMENT_SETS:
        return ELEMENT_SETS[key]
    # 兼容中文标签
    for k, v in ELEMENT_SETS.items():
        if key and key == v["label"]:
            return v
    return DEFAULT_SET


def describe_elements(intent: str) -> str:
    s = elements_for(intent)
    lines = [f"- [{e['star']}] {e['name']}：{e['hint']}" for e in s["elements"]]
    return "\n".join(lines)


def score_of(verdicts: list[str]) -> float:
    """三档折算：命中 1、沾边 0.5、缺失 0。"""
    return round(sum(1.0 if v == "hit" else 0.5 if v == "partial" else 0 for v in verdicts), 1)


# ---------------------------------------------------------------- 整场四维

def dimensions() -> list:
    from .prompts import REPORT_DIMENSIONS
    return REPORT_DIMENSIONS


def names() -> list[str]:
    return [d["name"] for d in dimensions()]


def describe_for_prompt() -> str:
    lines = []
    for d in dimensions():
        highs = "、".join(d["high"])
        lows = "、".join(d["low"])
        lines.append(f"- {d['name']}：命中信号={highs}；扣分信号={lows}")
    return "\n".join(lines)
