"""LLM 调用层：超时、重试、token 记账、失败降级。

为什么单独一层：v1 里每次对话都直接 `client.chat.completions.create(...)`，
API 一抖整个页面就白屏 —— 这是「Demo 特征」最明显的地方。
真实产品里，模型调用一定是不可靠依赖，必须包一层：超时、重试、降级、记账。
"""

import os
import re
import time

from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError, APIStatusError

from . import budget

# 价格：DeepSeek 官方价目（2026-11，deepseek-flash 峰时、缓存未命中口径，按 1 USD≈7.1 CNY 折算）。
# 谷时是峰时的一半，所以这里算出来的是上限，页面标注「约」。价格变动快，集中改这一处。
PRICING_CNY_PER_1M = {"input": 2.13, "output": 8.52}

# 模型名：deepseek-chat 是 2026-07 起就宣布下线的旧别名，虽然目前仍被路由到 flash，
# 但随时可能消失。线上显式用 deepseek-flash，并关掉思考模式：
# 一是不需要（面试对话不需要长链推理），二是能把延迟和输出 token 压住。
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

TIMEOUT_SECONDS = 60
MAX_RETRIES = 2


# 匹配 [[END]] / [END] / [[ end ]] 等写法，整段剥掉，不留 "[]" 残渣
_END_RE = re.compile(r"\[+\s*END\s*\]+", re.IGNORECASE)


def parse_reply(text: str) -> tuple[str, str]:
    """剥离收尾标记，返回 (干净文本, 意图)。

    意图：finish = 模型表达了「想收尾」（越权信号，不授权任何事）；continue = 正常提问。
    标记绝不能流到界面，所以所有入库前都必须先过这里。
    """
    raw = text or ""
    had_marker = bool(_END_RE.search(raw))
    clean = _END_RE.sub("", raw)
    clean = re.sub(r"[ \t]+\n", "\n", clean).strip()
    return clean, ("finish" if had_marker else "continue")


class LLMError(Exception):
    """对外暴露的统一异常，UI 只显示这句话，不把堆栈甩给用户。"""


def get_client() -> OpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        try:
            import streamlit as st

            api_key = st.secrets.get("DEEPSEEK_API_KEY", "")
        except Exception:
            api_key = ""
    if not api_key or api_key.startswith("sk-你的"):
        raise LLMError("还没有配置 DeepSeek API Key")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def chat(messages, *, temperature=0.7, json_mode=False, max_retries=MAX_RETRIES):
    """调一次模型，返回 (文本, usage)。失败时重试，最终失败抛 LLMError。"""
    if not budget.allowed():
        raise LLMError("今天的演示额度已经用完，明天再来试试。想继续练可以找我要一个独立额度。")

    client = get_client()
    last_err = None

    for attempt in range(max_retries + 1):
        try:
            kwargs = {}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=temperature,
                timeout=TIMEOUT_SECONDS,
                extra_body={"thinking": {"type": "disabled"}},
                **kwargs,
            )
            usage = {
                "input": getattr(resp.usage, "prompt_tokens", 0) or 0,
                "output": getattr(resp.usage, "completion_tokens", 0) or 0,
            }
            budget.record(cost_cny(usage))
            return (resp.choices[0].message.content or "").strip(), usage

        except (APITimeoutError, APIConnectionError, RateLimitError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))  # 线性退避，别把限流打得更死
        except APIStatusError as e:
            # 4xx（如 key 无效、余额不足）重试没意义，直接失败
            if 400 <= e.status_code < 500 and e.status_code != 429:
                raise LLMError(f"模型服务拒绝了请求（HTTP {e.status_code}），请检查 API Key 或账户余额") from e
            last_err = e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # 兜底：任何未知异常都不能让页面白屏
            last_err = e
            break

    raise LLMError(f"模型调用失败（已重试 {max_retries} 次）：{type(last_err).__name__}")


def cost_cny(usage_totals) -> float:
    """按 token 数估算费用。"""
    return (
        usage_totals.get("input", 0) / 1_000_000 * PRICING_CNY_PER_1M["input"]
        + usage_totals.get("output", 0) / 1_000_000 * PRICING_CNY_PER_1M["output"]
    )
