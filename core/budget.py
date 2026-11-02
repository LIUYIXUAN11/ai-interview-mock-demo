"""每日花费护栏：公开链接最怕的不是一个人用，是脚本无限刷。

做法：每次模型调用成功后，把这一笔的估算成本追加到当天的记账文件；
下一次调用前先看今天花了多少，超过上限就直接拒绝，不再打 API。

上限来自环境变量 DAILY_CAP_CNY（或 st.secrets 里的同名项），默认 0 = 不限制，
所以本地开发、离线测试完全不受影响。公开部署时建议设成 10~20 元：

- 一场快速面试约 ¥0.05，¥10 大约能撑 200 场；
- 真被刷，最多损失这一天上限，不会把账户余额打穿；
- 记账文件在 records/_public/ 下，平台重启会清零（免费托管本来就不保证持久化）。

更稳的第二道闸：给线上单独建一个 API Key，只充 ¥20~50，用完即止。
"""

import json
import os
import time

ROOT = os.environ.get("INTERVIEW_DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cap() -> float:
    raw = os.environ.get("DAILY_CAP_CNY", "")
    if not raw:
        try:
            import streamlit as st

            raw = st.secrets.get("DAILY_CAP_CNY", "")
        except Exception:
            raw = ""
    try:
        return max(0.0, float(raw or 0))
    except (TypeError, ValueError):
        return 0.0


def _path() -> str:
    day = time.strftime("%Y-%m-%d")
    return os.path.join(ROOT, "records", "_public", f"spend-{day}.jsonl")


def spent_today() -> float:
    cap = _cap()
    if cap <= 0:
        return 0.0
    path = _path()
    if not os.path.exists(path):
        return 0.0
    total = 0.0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    total += float(json.loads(line).get("cost", 0))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
    except OSError:
        return 0.0
    return round(total, 6)


def allowed() -> bool:
    cap = _cap()
    return cap <= 0 or spent_today() < cap


def remaining() -> float:
    cap = _cap()
    return max(0.0, cap - spent_today()) if cap > 0 else float("inf")


def record(cost: float) -> None:
    """记一笔实际花费。没设上限时不写盘，避免本地无意义的 IO。"""
    if _cap() <= 0 or cost <= 0:
        return
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "cost": round(cost, 6)},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
