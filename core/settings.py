"""运行时可调参数（温度等），改 secrets 或环境变量即可，不用动代码。

优先级：环境变量 > st.secrets > 代码里的默认值

可调项（都在 0–2 之间）：
    TEMP_CLAIMS     主张抽取     默认 0.0   ← 抽取类要稳定，别动
    TEMP_PROBE      追问决策     默认 0.0   ← 决策类要稳定
    TEMP_REVIEW     线上审稿     默认 0.0   ← 审稿必须可复现
    TEMP_COACH      评价/建议    默认 0.0
    TEMP_QUESTION   提问措辞     默认 0.7   ← 唯一建议你调的一个
    TEMP_INTERVIEW  模拟面试     默认 0.7
"""

import os


def _raw(key: str) -> str:
    v = os.environ.get(key)
    if v:
        return v
    try:
        import streamlit as st
        return str(st.secrets.get(key, ""))
    except Exception:
        return ""


def temp(kind: str, default: float) -> float:
    v = _raw(f"TEMP_{kind.upper()}")
    if not v:
        return default
    try:
        return max(0.0, min(2.0, float(v)))
    except ValueError:
        return default
