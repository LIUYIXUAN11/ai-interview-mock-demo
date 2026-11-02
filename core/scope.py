"""访问作用域：公开部署时，把每个浏览器会话的数据隔开。

为什么需要这一层：
Streamlit 的所有访问者共用同一个进程、同一份磁盘。本地一个人用没问题；
一旦把链接公开，A 上传的简历、面到一半的对话会被 B 看到——这不是体验问题，
是隐私事故。所以公开模式下，每个会话拿一个服务端生成的随机 scope，
所有用户数据都落到 records/_scopes/<scope>/ 下面。

为什么用 contextvar 而不是模块级变量：
每个 Streamlit 会话在自己的线程里跑脚本。模块级变量会被并发会话互相覆盖，
contextvar 跟着线程上下文走，每次脚本开头 set_current() 一次即可。

本地（不设 scope）完全沿用原来的 records/ 目录，历史数据不受影响。
"""

import contextvars
import os
import re

ROOT = os.environ.get("INTERVIEW_DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CURRENT = contextvars.ContextVar("interview_scope", default="")
# 只接受服务端生成的十六进制串，防止外部传入路径穿越
_SAFE = re.compile(r"^[0-9a-f]{8,32}$")


def set_current(scope: str) -> None:
    _CURRENT.set(scope if scope and _SAFE.match(scope) else "")


def current() -> str:
    return _CURRENT.get()


def isolated() -> bool:
    return bool(_CURRENT.get())


def user_dir(*parts: str) -> str:
    """用户数据目录：公开模式按会话隔离；本地模式沿用 records/。"""
    if isolated():
        return os.path.join(ROOT, "records", "_scopes", current(), *parts)
    return os.path.join(ROOT, "records", *parts)
