"""练习页：一段经历 → 先面一次（3 轮）→ 成绩单 → 练这 3 题 → 复测整链。

流程（按产品负责人的定稿）：
1. 迷你模拟面试：主问 + 两刀追问，中途不打断；
2. 逐轮打分（五维），展示每一轮哪里崩了；
3. 这 3 个问题全部变成练习题（不是只练崩掉的那个）；
4. 练习：逐题 重答 → 体检 → 改版（不限次数）；
5. 复测：整条链再来一遍，和上一次比。
"""

import html

import streamlit as st

from core import mini as mini_mod
import talk_ui
from core import analytics
from core import practice as practice_mod
from core.mini import INTENTS, MiniInterview
from core.practice import PracticeSession

ICON = {"hit": "✅", "partial": "🟡", "miss": "❌"}


# ---------------------------------------------------------------- 小工具

def _esc(s: str) -> str:
    return html.escape(s or "")


def render_multi_highlight(answer: str, els: list[dict]) -> None:
    """按要素判定给原回答多段标色：命中=绿、沾边=黄。"""
    spans = []
    for e in els:
        sp = e.get("span")
        if not sp and e.get("quote"):                     # 老数据没有 span，用引用文本现找
            q = e["quote"]
            i = answer.find(q)
            if i >= 0:
                sp = (i, i + len(q))
        if sp and e.get("verdict") in ("hit", "partial"):
            spans.append((sp[0], sp[1], e["verdict"]))
    if not spans:
        render_highlighted(answer, None)
        return
    spans.sort()
    merged, cur = [], None
    for a, b, v in spans:
        if cur and a <= cur[1]:
            cur = (cur[0], max(cur[1], b), "partial" if "partial" in (cur[2], v) else "hit")
        else:
            if cur:
                merged.append(cur)
            cur = (a, b, v)
    if cur:
        merged.append(cur)

    color = {"hit": "#d9f7be", "partial": "#ffe58f"}
    out, pos = [], 0
    for a, b, v in merged:
        if a < pos:
            a = pos
        out.append(_esc(answer[pos:a]))
        out.append(f"<mark style='background:{color[v]};padding:0 2px;border-radius:3px'>{_esc(answer[a:b])}</mark>")
        pos = b
    out.append(_esc(answer[pos:]))
    st.markdown(f"<div style='line-height:1.9'>{''.join(out)}</div>", unsafe_allow_html=True)


def render_highlighted(answer: str, span) -> None:
    if not answer:
        return
    if span:
        a, b = span
        body = (f"{_esc(answer[:a])}"
                f"<mark style='background:#ffe58f;padding:0 2px;border-radius:3px'>{_esc(answer[a:b])}</mark>"
                f"{_esc(answer[b:])}")
    else:
        body = _esc(answer)
    st.markdown(f"<div style='line-height:1.9'>{body}</div>", unsafe_allow_html=True)


def render_dims(dims: list[dict]) -> None:
    """整场层的四维（带引用）。"""
    for d in dims:
        st.markdown(f"{ICON.get(d['verdict'], '➖')} **{d['name']}**　{d['why']}")
        if d.get("quote"):
            st.caption(f"　「{d['quote']}」")


ELEM_ICON = {"hit": "✅", "partial": "🟡", "miss": "❌"}
LABEL = {"why_do": "为什么做这个", "why_this": "为什么选这个方案",
         "how_verify": "结果怎么验证", "default": "通用"}


def render_metrics(coach: dict) -> None:
    """三个可判指标并排：覆盖 / 全面 / 细致。"""
    els = coach.get("elements") or []
    score = coach.get("score", 0)
    total = coach.get("total", len(els)) or len(els)
    cov = coach.get("coverage")
    det = coach.get("detail")

    st.markdown("""
<style>
  [data-testid="stMetricValue"] { font-size: 22px !important; line-height: 1.3 !important; }
  [data-testid="stMetricLabel"] { font-size: 12px !important; color: #888 !important; }
  [data-testid="stMetric"] { padding: 0 !important; }
</style>
""", unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    if score is None:
        st.warning("这题没答到，暂不评分。")
        return
    c1.metric("要素命中", f"{score} / {total}")
    c2.metric("全面", f"{int(round(cov * 100))}%" if cov is not None else "—")
    c3.metric("细致", f"{det:.1f}" if det is not None else "—")
    if coach.get("guards"):
        st.caption("守则生效：" + "、".join(coach["guards"]))


def render_elements(coach: dict) -> None:
    """要素清单：命中给原文引用，未命中就在这一条下面说缺什么、怎么补。"""
    for e in coach.get("elements") or []:
        icon = ELEM_ICON.get(e["verdict"], "·")
        st.markdown(f"{icon} **{e['name']}**　{e['why']}")
        if e.get("quote"):
            st.markdown(
                f"<div style='margin:-6px 0 8px 26px;padding:6px 10px;background:#f7f7f9;"
                f"border-left:3px solid #d9d9e3;border-radius:4px;color:#555;font-size:13px'>"
                f"「{_esc(e['quote'])}」</div>",
                unsafe_allow_html=True,
            )
        if e.get("verdict") != "hit" and e.get("fix"):
            st.markdown(
                f"<div style='margin:-6px 0 10px 26px;color:#b35c00;font-size:13px'>"
                f"↳ 怎么补：{_esc(e['fix'])}</div>",
                unsafe_allow_html=True,
            )


def render_offtopic(s, latest: dict) -> None:
    """没答到这道题：第一次给提示重答；第二次记"证据不足"，不评 0 分。"""
    coach = latest.get("coach") or {}
    st_ = coach.get("status") or {}
    n = s.offtopic_count()
    st.warning(f"这题没答到——{st_.get('why', '答的不是这个问题')}")
    if st_.get("quote"):
        st.caption(f"「{st_['quote']}」")

    if n <= 1:
        st.info("再答一次就好：回到题目问的那件事，先给结论再补细节。")
        ans = st.text_area("重新回答", height=120, key=f"retry_{s.idx}_{n}",
                           placeholder="这次直接回答题目问的那件事……")
        if st.button("提交重答", type="primary", disabled=not (ans or "").strip()):
            with st.spinner("正在比对……"):
                s.submit(ans, revise=True)
                practice_mod.save(s)
            st.rerun()
        return

    st.error("两次都没答到，这题记为「证据不足」，不评分。")
    if st.button("下一题 →", use_container_width=True):
        if not s.next_unanswered():
            st.info("这题先跳过，去下一题。")
        practice_mod.save(s)
        st.rerun()


CIRCLED = "①②③④⑤⑥⑦⑧⑨"


def render_coach_card(coach: dict, heading: str = "这句话的问题") -> None:
    """打分卡：一句结论 → 三个指标 → 要素清单 → 问题与改法 → 骨架 → 示范开头。"""
    with st.container(border=True):
        st.markdown(f"**{heading}**：{coach.get('one_line', '')}")
        render_metrics(coach)
        st.divider()
        render_elements(coach)

        sk = coach.get("skeleton") or []
        if sk:
            st.divider()
            steps = "\n".join(f"{CIRCLED[i] if i < len(CIRCLED) else '·'} {x}" for i, x in enumerate(sk))
            st.markdown("**骨架**\n\n" + steps)
        if coach.get("demo_opening"):
            st.caption(f"示范开头：{coach['demo_opening']}")


def score_badge(score, total):
    pct = int(score / total * 100) if total else 0
    return f"{score}/{total}（{pct}%）"


# ---------------------------------------------------------------- 首页

def get_cached_projects(resume: str):
    sig = practice_mod.source_signature(resume, "")
    cache = st.session_state.get("projects_cache")
    if cache and cache.get("sig") == sig:
        return cache["items"]
    saved = practice_mod.load_projects(sig)
    if saved is not None:
        st.session_state["projects_cache"] = {"sig": sig, "items": saved}
        return saved
    return None


def parse_now(resume: str, loose: bool = False):
    sig = practice_mod.source_signature(resume, "")
    with st.spinner("正在读简历、整理可练的经历……"):
        result = practice_mod.extract_experiences_detailed(resume, loose=loose)
    items = result["items"]
    st.session_state["parse_notes"] = result.get("notes") or []
    st.session_state["projects_cache"] = {"sig": sig, "items": items}
    practice_mod.save_projects(sig, items)
    st.session_state.pop("force_parse", None)
    return items


def render_home(resume: str, jd: str) -> None:
    # 同一行卡片等高：让列拉伸、卡片填满列高
    st.markdown("""
<style>
  [data-testid="stHorizontalBlock"] { align-items: stretch; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    display: flex;
  }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] > div {
    width: 100%;
    display: flex;
    flex-direction: column;
  }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] > div > div {
    height: 100%;
  }
  /* 卡片内部：纵向 flex，按钮区沉到卡片底部，同一行按钮就对齐了 */
  [class*="st-key-card_"] {
    display: flex;
    flex-direction: column;
    height: 100%;
  }
  [class*="st-key-card_"] > div:last-child {
    margin-top: auto;
  }
</style>
""", unsafe_allow_html=True)
    st.markdown("## 练习 · 选一段经历")
    st.caption("每段经历点「开始练习」直接进对话：边答边评，追问按你缺的地方走。")

    if not (resume or "").strip():
        st.info("先在左侧上传简历——题目会按你简历里的经历出。")
        return

    force = st.session_state.get("force_parse", False)   # 解析成功后才清，避免点了没反应
    items = None if force else get_cached_projects(resume)
    if items is None:
        with st.container(border=True):
            st.markdown("**简历已就绪**")
            st.caption(f"已上传 {len(resume)} 字。点下面按「实习 / 项目 / 比赛 / 科研」拆成可练的经历。")
            if st.button("开始解析简历", type="primary", use_container_width=True):
                parse_now(resume)
                st.rerun()
        return

    if not items:
        st.warning("没抽到实习或项目经历。")
        st.caption("这份简历可能只写了工作经历（全职）。要把它也算进来吗？")
        if st.button("用宽松模式重新解析（工作经历也算）", type="primary"):
            parse_now(resume, loose=True)
            st.rerun()
        return

    for note in (st.session_state.get("parse_notes") or []):
        st.caption(f"· {note}")

    for cat in practice_mod.CATEGORIES:
        group = [x for x in items if x["type"] == cat]
        if not group:
            continue
        st.markdown("#### " + ("实习 / 工作经历" if cat == "实习" else cat))
        cols = st.columns(2)
        for i, it in enumerate(group):
            with cols[i % 2]:
                with st.container(border=True, key=f"card_{cat}_{i}"):
                    st.markdown(f"**{it['name']}**")
                    st.caption(f"依据：{it['evidence']}")
                    prev = mini_mod.find_for(it["name"])
                    unfinished = prev is not None and not prev.finished
                    if unfinished:
                        st.caption(f"上次停在第 {len(prev.answers) + 1} 轮，点「开始练习」接着练")
                    elif prev is not None:
                        st.caption(f"上次练了 {len(prev.answers)} 轮，点「开始练习」重新开始")

                    def start_fresh():
                        analytics.track("experience_select", kind=cat)
                        m2 = MiniInterview(it, resume, jd)
                        with st.spinner("准备面试官……"):
                            if m2.start():
                                mini_mod.save(m2)
                                import time as _t
                                st.session_state["tk_enter_ts"] = _t.time()
                                analytics.track("session_start", resumed=False)
                                st.session_state["mini"] = m2
                                st.session_state["practice_home"] = False
                                st.session_state.pop("mini_before", None)
                                st.rerun()
                            else:
                                st.error(m2.error or "出题失败")

                    def enter(m):
                        import time as _t
                        st.session_state["tk_enter_ts"] = _t.time()
                        analytics.track("session_start", resumed=True)
                        st.session_state["mini"] = m
                        st.session_state["practice_home"] = False
                        st.rerun()

                    # 每张卡都是同一套按钮：主按钮「开始练习」（未完成就续上）
                    if unfinished:
                        c1, c2 = st.columns([2, 1])
                        if c1.button("开始练习", key=f"mini_go_{cat}_{i}", type="primary",
                                     use_container_width=True):
                            enter(prev)
                        if c2.button("重新开始", key=f"mini_redo_{cat}_{i}", use_container_width=True):
                            start_fresh()
                    else:
                        if st.button("开始练习", key=f"mini_go_{cat}_{i}", type="primary",
                                     use_container_width=True):
                            start_fresh()


# ---------------------------------------------------------------- 迷你面试

def back_home() -> None:
    for k in ("mini", "practice"):
        st.session_state.pop(k, None)
    st.session_state["practice_home"] = True
    st.rerun()


def render_mini(m: MiniInterview) -> None:
    top_l, top_r = st.columns([4, 1])
    top_l.markdown(f"### {m.entry.get('name', '')} · 模拟面试")
    if top_r.button("← 返回环节列表", use_container_width=True):
        back_home()
    st.caption(f"第 {min(len(m.answers) + 1, 3)}/3 轮 · 中途不打断，答完统一打分")

    for i, q in enumerate(m.questions):
        st.markdown(f"**面试官（第 {i+1} 轮）**：{q}")
        if i < len(m.answers):
            st.markdown(f"**你**：{m.answers[i]}")

    if not m.finished:
        ans = st.chat_input("在这里回答……")
        if ans:
            with st.spinner("面试官在想下一刀……" if len(m.answers) < 2 else "正在打分……"):
                m.answer(ans)
                mini_mod.save(m)
            st.rerun()
    else:
        render_mini_result(m)


def render_mini_result(m: MiniInterview) -> None:
    rep = m.report
    st.divider()
    top_l, top_r = st.columns([4, 1])
    top_l.markdown("### 逐轮打分")
    if top_r.button("← 返回环节列表", use_container_width=True, key="back_from_result"):
        back_home()
    if not rep:
        st.error("这次打分没成功")
        return

    st.caption(f"合计 {score_badge(rep['score'], rep['total'])}")
    for r in rep["rounds"]:
        with st.container(border=True):
            st.markdown(f"**第 {r['round']} 轮** · {score_badge(r['score'], r['total'])}")
            st.caption(f"问：{r['question']}")
            render_dims(r["dimensions"])

    if rep.get("redo"):
        st.info(f"如果重来最该补：{rep['redo']}")

    cols = st.columns(2)
    if cols[0].button("把这 3 题练一遍", type="primary", use_container_width=True):
        s = PracticeSession(entry=m.entry, resume=m.resume, jd=m.jd, items=m.practice_items())
        if s.start():
            practice_mod.save(s)
            st.session_state["practice"] = s
            st.session_state["mini_before"] = m.report
            st.session_state.pop("mini", None)
            st.rerun()
    if cols[1].button("先不练，回列表", use_container_width=True):
        st.session_state.pop("mini", None)
        st.rerun()


# ---------------------------------------------------------------- 练习

def render_practice(s) -> None:
    total = len(s.questions)
    top_l, top_r = st.columns([4, 1])
    top_l.markdown(f"### {s.entry.get('name', '')} · 逐题练")
    if top_r.button("← 返回环节列表", use_container_width=True):
        back_home()
    cols = st.columns(total)
    for i, q in enumerate(s.questions):
        done = bool(s.versions_of(i))
        if cols[i].button(("✅ " if done else "") + f"第 {i+1} 题", key=f"nav_{i}", use_container_width=True):
            if s.go_to(i):
                st.rerun()
            else:
                st.warning("这题还没解锁——先把前面的题各答一次。")

    q = s.questions[s.idx]
    st.caption(f"考察：{LABEL.get(q.get('intent', ''), q.get('intent', ''))}　（来自刚才那场面试的第 {s.idx+1} 轮）")
    st.markdown(f"**{q['text']}**")

    versions = s.current_versions()
    latest = versions[-1] if versions else None

    if versions:
        pct = [f"v{i+1} {( (v.get('coach') or {}).get('score') or 0) }"
               for i, v in enumerate(versions)]
        st.caption("修改记录：" + "　›　".join(pct) + "　（每次提交都会保留，不覆盖）")

    if latest:
        st.markdown("**你的回答**")
        coach = latest.get("coach") or {}
        hl_cur = coach.get("highlight") or {}
        render_multi_highlight(latest["answer"], coach.get("elements") or [])
        st.divider()

    # 还没改过：原答整段标黄 + 诊断，全部收进下拉框；编辑框预填原答
    if latest is None:
        original = s.baseline_answer()
        if original:
            with st.spinner("正在找原答里哪句有问题……"):
                base = s.ensure_baseline()
            with st.expander("你当时是怎么答的（点开看哪句有问题、怎么改）", expanded=True):
                st.caption("有问题的地方已经标黄")
                render_multi_highlight(original, (base or {}).get("elements") or [])
                st.divider()
                if base is None:
                    st.warning("原答的优化方向没算出来，可以直接在下面重写。")
                else:
                    st.divider()
                    render_coach_card(base, heading="这段的问题")

            revised = st.text_area("在原文上改（不限次数）", value=original, height=140,
                                   key=f"edit_{s.idx}_0",
                                   help="把缺的那部分补进去，不用从头重写")
            if st.button("提交这一版", type="primary", disabled=not (revised or "").strip()):
                if revised.strip() == original.strip():
                    st.warning("还没有改动——按骨架补一句再提交。")
                else:
                    with st.spinner("正在逐句比对……"):
                        s.submit(revised)
                        practice_mod.save(s)
                    st.rerun()
            return

    if latest is None:
        ans = st.chat_input("重新答一遍……")
        if ans:
            with st.spinner("正在逐句比对……"):
                s.submit(ans)
                practice_mod.save(s)
            st.rerun()
        return

    coach = latest.get("coach")
    status = (coach or {}).get("status") or {}
    if coach and status.get("code") == "off_topic":
        render_offtopic(s, latest)
        return

    if not coach:
        st.warning("这次体检没成功。")
        if st.button("重新体检"):
            with st.spinner("重新体检中……"):
                ok = s.recheck()
            practice_mod.save(s)
            if ok is None:
                st.error("还是没成功，稍后再试。")
            else:
                st.rerun()
        return

    render_coach_card(coach, heading="这句话的问题")

    revised = st.text_area("继续改（不限次数）", value=latest["answer"], height=140,
                           key=f"rev_{s.idx}_{len(versions)}",
                           help="在上一次的基础上补，不用重写")
    cols = st.columns(2)
    if cols[0].button("提交改版", type="primary", use_container_width=True,
                      disabled=not (revised or "").strip()):
        with st.spinner("正在重新比对……"):
            s.submit(revised, revise=True)
            practice_mod.save(s)
        st.rerun()
    if cols[1].button("下一题 →", use_container_width=True):
        if not s.next_unanswered():
            st.info("三题都练过了，去复测。")
        practice_mod.save(s)
        st.rerun()

    st.divider()
    if st.button("复测：整条链再面一次", type="primary"):
        m = MiniInterview(s.entry, s.resume, s.jd)
        with st.spinner("准备复测……"):
            ok = m.start()
        if ok:
            mini_mod.save(m)
            st.session_state["mini"] = m
            st.session_state["practice_home"] = False
            st.session_state.pop("practice", None)
            st.rerun()
        else:
            st.error(m.error or "复测出题失败")


# ---------------------------------------------------------------- 入口

def restore() -> None:
    """刷新后接着来：只恢复对话（旧的逐题练不再恢复）。"""
    if st.session_state.get("mini"):
        return
    if st.session_state.get("practice_home"):
        return
    m = mini_mod.latest()
    if m is not None and not m.finished:
        st.session_state["mini"] = m
        st.info(f"已恢复上次没练完的对话：第 {len(m.answers) + 1} 轮。")
        return
    if m is not None and m.finished and m.report:
        st.session_state["mini"] = m
        st.info("已打开上一次面试的成绩单。")


def render(resume: str, jd: str) -> None:
    restore()
    m = st.session_state.get("mini")
    if m is not None:
        talk_ui.render(m, on_exit=back_home)
        return
    render_home(resume, jd)
