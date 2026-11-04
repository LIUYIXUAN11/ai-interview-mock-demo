"""练习页：对齐 TalkAI 的对话界面。

布局（自上而下）：
  顶栏：左返回 · 中头像+名字 · 右设置
  时间戳
  用户气泡（浅绿、整宽）：正文高亮 + 气泡内工具栏「⚠ 解析⌄ | 朗读」
      展开后：检测结果 / 解析如下（编号）/ 纠正建议（整段）
  评分条（气泡下方居中）：结构 · 证据 · 表达
  AI 气泡（浅灰）
  底部输入
"""

import html
import re

import streamlit as st

from core import analytics
from core import mini as mini_mod

CSS = """
<style>
  .tk-top { display:flex; align-items:center; justify-content:space-between;
            padding:2px 0 6px; border-bottom:1px solid #f0f0f2; margin-bottom:6px; }
  .tk-name { font-size:17px; font-weight:600; }
  .tk-avatar { width:26px; height:26px; border-radius:50%; background:#ffe7ba;
               display:inline-block; margin-right:8px; vertical-align:middle; }
  .tk-time { text-align:center; color:#bbb; font-size:12px; margin:10px 0; }

  .tk-user { background:#d9f7be; border-radius:12px; padding:16px 16px 40px;
             line-height:2.0; font-size:16px; margin:2px 0 0; }
  .tk-user mark { background:#b7eb8f; border-radius:3px; padding:1px 2px; }
  .tk-user mark.warn { background:#ffe7ba; }
  .tk-toggle, .tk-user a.tk-toggle { color:#9a9aa2 !important; font-size:13px;
      text-decoration:none !important; }
  .tk-toggle:hover { color:#666 !important; }
  .tk-detail { margin-top:10px; padding-top:12px; border-top:1px dashed #c9c9d1;
               font-size:15px; line-height:1.95; }

  .tk-ai { background:#f5f5f7; border-radius:12px; padding:16px; line-height:2.0;
           font-size:16px; margin:14px 0 6px; }

  .tk-chips-wrap { text-align:right; margin:-30px 0 2px; }
  .tk-chips { display:inline-flex; gap:26px; padding:8px 16px;
              border:1px dashed #dcdce2; border-radius:10px; background:#fcfcfd; }
  .tk-chip { text-align:center; font-size:13px; line-height:1.5; }
  .tk-chip b { font-size:14px; }
  .tk-chip small { display:block; color:#aaa; font-size:11px; margin-top:1px; }
  .tk-red { color:#f5222d; } .tk-green { color:#52c41a; } .tk-orange { color:#fa8c16; }
  div[data-testid="stButton"] button[kind="tertiary"] {
      color:#9a9aa2; font-size:13px; padding:0 2px; min-height:0; height:auto;
      background:transparent; border:none; }
  div[data-testid="stButton"] button[kind="tertiary"]:hover { color:#666; background:transparent; }
</style>
"""


def _esc(t: str) -> str:
    return html.escape(t or "")


def _mark(text: str, spans: list) -> str:
    """把被评价的片段染色：hit=绿、partial=橙。"""
    if not spans:
        return _esc(text)
    out, pos = [], 0
    for a, b, kind in sorted(spans):
        if a < pos or b > len(text):
            continue
        out.append(_esc(text[pos:a]))
        cls = "warn" if kind == "partial" else ""
        out.append(f"<mark class='{cls}'>{_esc(text[a:b])}</mark>")
        pos = b
    out.append(_esc(text[pos:]))
    return "".join(out)


def _spans(ev: dict) -> list:
    return [(e["span"][0], e["span"][1], e.get("verdict", "hit"))
            for e in (ev.get("elements") or [])
            if e.get("span") and e.get("verdict") in ("hit", "partial")]


def _detail_html(ev: dict) -> str:
    """展开后的建议：问题 → 建议这样说；最后给完整参考答法。"""
    issues = ev.get("issues") or []
    parts = ["<div class='tk-detail'>"]

    if issues:
        parts.append(f"<b>问题（{len(issues)} 条，按严重程度）</b>")
        rows = []
        for it in issues:
            rows.append(
                "<li style='margin-bottom:10px'>"
                f"{_esc(it.get('problem',''))}"
                "<div style='color:#237804;margin-top:3px'>建议这样说："
                f"{_esc(it.get('say',''))}</div></li>"
            )
        parts.append(f"<ol style='margin:8px 0 0 18px;padding:0'>{''.join(rows)}</ol>")
    else:
        parts.append("这轮没发现明显问题，可以继续往深处问。")

    if ev.get("model_answer"):
        parts.append("<div style='margin-top:14px'><b>完整参考答法（可以直接背）</b><br>"
                     + _esc(ev["model_answer"]) + "</div>")
        parts.append("<div style='color:#999;font-size:12px;margin-top:6px'>"
                     "（补你的数据）表示这里要用你真实的数字，系统不会替你编。</div>")
    parts.append("</div>")
    return "".join(parts)


def _chips(ev: dict) -> None:
    ms = {m["name"]: m for m in (ev.get("metrics") or [])}
    cells = []
    labels = {"结构": "表达结构", "证据": "有数据支撑", "表达": "说得清楚"}
    for name in ("结构", "证据", "表达"):
        m = ms.get(name)
        if not m:
            continue
        s = m.get("score", 3)
        cls = "tk-green" if s >= 4 else "tk-orange" if s == 3 else "tk-red"
        mark = "✓" if s >= 4 else "！" if s <= 2 else ""
        cells.append(f"<div class='tk-chip'><b class='{cls}'>{name}{mark}</b>"
                     f"<small>{labels[name]} {s}/5</small></div>")
    st.markdown(f"<div class='tk-chips-wrap'><div class='tk-chips'>{''.join(cells)}</div></div>",
                unsafe_allow_html=True)


def _unused_toggle_param() -> None:
    """气泡里的「查看解析」是链接，点了改写 query 参数，这里把它变成状态。

    每次点击都带一个递增的 n，保证地址真的变了——否则点第二次浏览器认为
    "还是同一个地址"，不会触发重跑，看起来就是"点了没反应"。
    """
    qp = st.query_params
    if "tk_open" not in qp:
        return
    idx = str(qp["tk_open"])
    n = str(qp.get("n", ""))
    key = f"tk_open_{idx}"
    if st.session_state.get(f"tk_seen_{idx}") != n:
        st.session_state[f"tk_seen_{idx}"] = n
        st.session_state[key] = not st.session_state.get(key, False)
        st.session_state[f"tk_n_{idx}"] = st.session_state.get(f"tk_n_{idx}", 0) + 1
    st.query_params.clear()
    st.rerun()


def render(mini, on_exit=None) -> None:
    st.markdown(CSS, unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1, 6, 2])
    if c1.button("←", use_container_width=True, key="tk_back"):
        import time as _t
        dwell = int(_t.time() - st.session_state.get("tk_enter_ts", _t.time()))
        analytics.track("session_leave", round=len(mini.answers), dwell_s=dwell, reason="back")
        if on_exit:
            on_exit()
        return
    c2.markdown("<div class='tk-top'><span><span class='tk-avatar'></span>"
                "<span class='tk-name'>AI 面试官</span></span></div>", unsafe_allow_html=True)
    with c3:
        st.caption(f"第 {len(mini.answers) + 1} 轮")

    st.markdown(f"<div class='tk-time'>{mini.entry.get('name','')} · 对话练习</div>",
                unsafe_allow_html=True)

    for i, q in enumerate(mini.questions):
        # ---- AI 提问 ----
        st.markdown(f"<div class='tk-ai'>{_esc(q)}</div>", unsafe_allow_html=True)
        if i >= len(mini.answers):
            break

        # ---- 用户回答（浅绿气泡）----
        ev = mini.evals[i] if i < len(mini.evals) else {}
        key = f"tk_open_{i}"
        opened = st.session_state.get(key, True)      # 默认展开，点「收起解析」才收
        body = _mark(mini.answers[i], _spans(ev))
        detail = _detail_html(ev) if opened else ""
        st.markdown(f"<div class='tk-user'>{body}{detail}</div>", unsafe_allow_html=True)
        toggle = ("收起解析 ⌃" if opened else "查看解析 ⌄")
        st.markdown(
            f"<style>.st-key-tk_btn_{i}{{margin-top:-32px;margin-bottom:6px;padding-left:16px;}}"
            f".st-key-tk_btn_{i} button{{color:#9a9aa2 !important;font-size:13px;background:transparent;"
            f"border:none;padding:0;min-height:20px;height:20px;}}</style>",
            unsafe_allow_html=True,
        )
        cl, cr = st.columns([3, 5], vertical_alignment="top")
        with cl:
            if st.button(toggle, key=f"tk_btn_{i}", type="tertiary"):
                st.session_state[key] = not opened
                st.rerun()
        with cr:
            _chips(ev)
        analytics.track("eval_shown", round=i + 1,
                        issues=len(ev.get("issues") or []))

    ans = st.chat_input("点击说话……（这里打字也可以）")
    if ans:
        analytics.track("answer_submit", round=len(mini.answers) + 1, chars=len(ans))
        with st.spinner("正在评价 + 想下一个问题……"):
            mini.answer(ans)
            mini_mod.save(mini)
        analytics.track("question_shown", round=len(mini.questions))
        st.rerun()
