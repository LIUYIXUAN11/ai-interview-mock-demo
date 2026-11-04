"""离线评审专用的第二个模型（Kimi）。

**只给 scripts/ 用，线上绝不调用。**
为什么要单独一层：
  - 线上审稿要和生成同源（延迟低、依赖少）；
  - 离线评测需要"另一个厂商的眼睛"，否则等于自己批自己。

配置（放 .streamlit/secrets.toml 或环境变量，都不进 git）：
    JUDGE_API_KEY   = "sk-..."                 # 必填，没有就退回主模型
    JUDGE_BASE_URL  = "https://api.moonshot.cn/v1"
    JUDGE_MODEL     = "kimi-k2-0905-preview"   # 按你开通的型号改
"""

import os

from openai import OpenAI

from .llm import LLMError


def _cfg(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    if v:
        return v
    try:
        import streamlit as st
        return st.secrets.get(key, default)
    except Exception:
        return default


_LAST_CALL = {"t": 0.0}
_MIN_INTERVAL = float(os.environ.get("JUDGE_MIN_INTERVAL", "25"))   # 账号 RPM=3 → 每 20 秒一次更稳


def available() -> bool:
    return bool(_cfg("JUDGE_API_KEY"))


def judge_model() -> str:
    return _cfg("JUDGE_MODEL", "kimi-k2-0905-preview")


def chat_judge(messages: list[dict], temperature: float = 0.0) -> tuple[str, dict]:
    """和主模型同签名的调用；失败抛 LLMError，由调用方降级。"""
    key = _cfg("JUDGE_API_KEY")
    if not key:
        raise LLMError("没配 JUDGE_API_KEY")
    client = OpenAI(api_key=key, base_url=_cfg("JUDGE_BASE_URL", "https://api.moonshot.cn/v1"))
    def _call(use_temp: bool):
        kw = {"model": judge_model(), "messages": messages, "timeout": 60}
        if use_temp:
            kw["temperature"] = temperature
        return client.chat.completions.create(**kw)

    import time as _t
    gap = _MIN_INTERVAL - (_t.time() - _LAST_CALL["t"])
    if gap > 0:
        _t.sleep(gap)
    _LAST_CALL["t"] = _t.time()

    try:
        for attempt in range(5):          # 你的账号 RPM=3，被限流就等一会儿再来
            try:
                r = _call(True)
                break
            except Exception as e:
                # 推理型模型（如 kimi-k3.1-pro）只允许 temperature=1，去掉参数重试
                if "temperature" in str(e):
                    r = _call(False)
                    break
                if "429" in str(e) and attempt < 4:
                    _t.sleep(25)
                    continue
                raise
    except Exception as e:                       # 网络/鉴权/限流统一降级
        raise LLMError(f"judge 调用失败：{type(e).__name__}: {e}") from e

    text = (r.choices[0].message.content or "").strip()
    usage = getattr(r, "usage", None)
    return text, {
        "input": getattr(usage, "prompt_tokens", 0) or 0,
        "output": getattr(usage, "completion_tokens", 0) or 0,
    }
