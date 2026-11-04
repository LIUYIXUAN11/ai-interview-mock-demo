"""本地埋点：只写本地 JSONL，不外传，保护隐私。

为什么自己做而不接第三方：这是求职作品集产品，用户会上传真实简历，
任何把简历内容发出去的行为都不可接受。这里只记录**行为**，不记录内容和姓名。

用法：
    from core.analytics import track
    track("answer_submit", round=2, chars=120)
"""

import json
import os
import time

_ROOT = os.environ.get("INTERVIEW_DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.join(_ROOT, "records", "events")

SENSITIVE = ("resume", "name", "answer", "question", "content", "text")


def _path() -> str:
    os.makedirs(_DIR, exist_ok=True)
    return os.path.join(_DIR, time.strftime("%Y%m%d") + ".jsonl")


def track(event: str, **props) -> None:
    """记一条事件；任何异常都吞掉，埋点绝不能影响主流程。"""
    try:
        clean = {}
        for k, v in props.items():
            if any(s in k for s in SENSITIVE):
                continue                      # 含隐私的字段一律不记
            if isinstance(v, (int, float, bool)) or v is None:
                clean[k] = v
            else:
                clean[k] = str(v)[:40]
        rec = {"ts": round(time.time(), 3), "event": event, **clean}
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
