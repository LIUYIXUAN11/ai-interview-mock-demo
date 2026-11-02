"""会话存储：每场面试（含未完成）实时落盘，支持中断恢复。

为什么要从「只存已完成」改成「每轮都存」：
用户手滑刷新、误关标签页，20 分钟的面试就白做了——这是「完成率」最直接的杀手。
而解法不是提醒用户小心，是别让面试只活在内存里。
"""

import json
import os
import time

from . import scope
from .engine import InterviewEngine
from .llm import cost_cny

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dir() -> str:
    """公开模式下按访问者隔离，本地模式沿用 records/sessions。"""
    return scope.user_dir("sessions")


def _ensure_dir():
    os.makedirs(_dir(), exist_ok=True)


def _path(sid: str) -> str:
    return os.path.join(_dir(), f"{sid}.json")


def save(engine: InterviewEngine, report: dict | None = None, report_error: str = "") -> dict:
    """落盘一场面试。每轮回答后都调用，所以未完成的对话也能恢复。"""
    _ensure_dir()
    m = engine.metrics()
    data = {
        "id": engine.session_id,
        "created_at": engine.created_at,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished": engine.finished,
        "pace": engine.pace,
        "resume": engine.resume,
        "jd": engine.jd,
        "engine": engine.to_dict(),
        "report": report,
        "report_error": report_error,
        "summary": {
            "user_turns": m["user_turns"],
            "duration_s": m["duration_s"],
            "stage": m["stage"],
            "tokens_input": m["usage"]["input"],
            "tokens_output": m["usage"]["output"],
            "calls": m["usage"]["calls"],
            "latency_ms": m["usage"]["latency_ms"],
            "cost_cny": round(cost_cny(m["usage"]), 5),
            "overall_score": (report or {}).get("overall_score"),
            "trace_rate": ((report or {}).get("trace") or {}).get("rate"),
            "override": m.get("override", {}),
        },
    }
    with open(_path(engine.session_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def load(sid: str) -> dict | None:
    try:
        with open(_path(sid), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def delete(sid: str) -> bool:
    p = _path(sid)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def list_sessions(limit: int = 50) -> list[dict]:
    """按最近更新倒序。读文件头即可，不解析整份对话。"""
    folder = _dir()
    if not os.path.isdir(folder):
        return []
    rows = []
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        rows.append(d)
    rows.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    return rows[:limit]


def latest_unfinished() -> dict | None:
    """最近一场「答过至少一句、但没结束」的面试。只问过开场白的空会话不算。"""
    for d in list_sessions():
        if d.get("finished"):
            continue
        if (d.get("summary") or {}).get("user_turns", 0) >= 1:
            return d
    return None


def summary(d: dict) -> dict:
    """给界面用的扁平记录（保持和旧版 record 的字段名一致）。"""
    s = dict(d.get("summary") or {})
    s["ts"] = d.get("updated_at", "")
    s["finished"] = d.get("finished", False)
    s["report_ok"] = bool(d.get("report"))
    return s


def engine_from_session(d: dict) -> InterviewEngine:
    return InterviewEngine.from_dict(d.get("engine") or {})


def stats() -> dict:
    rows = [d for d in list_sessions(500) if d.get("finished")]
    n = len(rows)
    if not n:
        return {"count": 0, "completion_rate": 0, "avg_turns": 0, "avg_cost": 0, "total_cost": 0, "avg_duration": 0}
    with_report = sum(1 for r in rows if r.get("report"))
    return {
        "count": n,
        "completion_rate": round(with_report / n, 2),
        "avg_turns": round(sum(r["summary"]["user_turns"] for r in rows) / n, 1),
        "avg_cost": round(sum(r["summary"]["cost_cny"] for r in rows) / n, 4),
        "total_cost": round(sum(r["summary"]["cost_cny"] for r in rows), 4),
        "avg_duration": int(sum(r["summary"]["duration_s"] for r in rows) / n),
    }
