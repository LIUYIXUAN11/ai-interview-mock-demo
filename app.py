"""AI 面试官 v2 · 界面层

只做三件事：收输入、渲染对话、把报告的每一句依据摊开给人看。
流程控制在 core/engine.py，话术在 core/prompts.py，评价在 core/report.py，存档在 core/store.py。
"""

import hmac
import importlib.util
import json
import os
import sys
import uuid

import streamlit as st

import practice_ui
from core import analytics
from core import mini as mini_mod
from core import demo_resume
from core import bank, ocr, profile, scope, store
from core.engine import InterviewEngine
from core.llm import PRICING_CNY_PER_1M, cost_cny
from core.report import generate_report

st.set_page_config(page_title="AI 面试官 · 可溯源面试练习", page_icon="🎤", layout="wide")

try:
    analytics.track("app_open")
except Exception:
    pass


# ---------------------------------------------------------------- 公开部署护栏
# 本地跑：什么都不变。线上跑：每个浏览器会话一份独立的数据目录，
# 避免 A 的简历/面试记录被 B 看到；口令是可选闸门，设了 APP_PASSWORD 才生效。

def _flag(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        try:
            raw = st.secrets.get(name, "")
        except Exception:
            raw = ""
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


PUBLIC_DEMO = _flag("PUBLIC_DEMO")
if PUBLIC_DEMO:
    # 为什么放 URL 里：session_state 在整页刷新后会重置，而用户刷新一次就丢整场面试，
    # 恰恰是这个产品最不能接受的失败。URL 上的随机串能跨刷新保持不变；
    # 发给别人的原始链接不带它，所以每个人打开都会拿到自己的新会话。
    _token = str(st.query_params.get("v", "") or "") if hasattr(st, "query_params") else ""
    if not (8 <= len(_token) <= 32 and all(c in "0123456789abcdef" for c in _token)):
        _token = uuid.uuid4().hex[:12]
        try:
            st.query_params["v"] = _token
        except Exception:
            pass
    st.session_state["_scope"] = _token
    scope.set_current(_token)


def _app_password() -> str:
    if os.environ.get("APP_PASSWORD"):
        return os.environ["APP_PASSWORD"]
    try:
        return str(st.secrets.get("APP_PASSWORD", "") or "")
    except Exception:
        return ""


_password = _app_password()
if _password and not st.session_state.get("_authed"):
    st.markdown("## 🔒 需要访问口令")
    st.caption("这个链接是私有的。输入口令后即可开始。")
    _entered = st.text_input("访问口令", type="password", key="_pw_input")
    if _entered:
        if hmac.compare_digest(_entered, _password):
            st.session_state["_authed"] = True
            st.rerun()
        st.error("口令不对")
    st.stop()


# ---------------------------------------------------------------- 工具

def parse_upload(file) -> tuple[str, str]:
    """把上传的简历转成纯文本，返回 (文本, 错误提示)。失败时告诉用户缺什么、怎么修。"""
    name = (file.name or "").lower()
    analytics.track("resume_upload", kind=(name.rsplit(".", 1)[-1] or "?"),
                    size_kb=int((getattr(file, "size", 0) or 0) / 1024))
    try:
        if name.endswith(".pdf"):
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader  # 老环境兜底
            reader = PdfReader(file)
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip(), ""
        if name.endswith(".docx"):
            import docx

            d = docx.Document(file)
            return "\n".join(p.text for p in d.paragraphs).strip(), ""
        if name.endswith((".jpg", ".jpeg", ".png")):
            from core import ocr
            return ocr.image_to_text(file.read())
        return file.read().decode("utf-8", errors="ignore").strip(), ""
    except ImportError as e:
        return "", (
            f"当前解释器（{sys.executable}）缺少解析依赖：{e.name}。"
            f"请用项目根目录的 ./run.sh 启动，或执行 venv/bin/pip install -r requirements.txt。"
        )
    except Exception as e:
        return "", f"简历解析失败（{type(e).__name__}）：{e}。可以直接粘贴文本。"


def _pretty_name(name: str) -> str:
    """浏览器有时传上来的是哈希文件名，显示成可读的短名。"""
    import re
    base = os.path.basename(name or "")
    stem, dot, ext = base.rpartition(".")
    if re.fullmatch(r"[0-9a-f]{16,}", stem or ""):
        return f"上传的简历.{ext}" if dot else "上传的简历"
    return base if len(base) <= 28 else base[:25] + "…" + (("." + ext) if dot else "")


def ensure_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        try:
            key = st.secrets.get("DEEPSEEK_API_KEY", "")
        except Exception:
            key = ""
    if not key or str(key).startswith("sk-你的"):
        st.error("还没配置 DeepSeek API Key。请在 `.streamlit/secrets.toml` 里写 `DEEPSEEK_API_KEY = \"sk-...\"`。")
        st.stop()


# ---------------------------------------------------------------- 顶部页签：练习 / 模拟面试
mode = st.segmented_control("模式", ["练习", "模拟面试"], default="练习",
                            label_visibility="collapsed", key="mode") or "练习"

if PUBLIC_DEMO:
    st.caption("公开演示版 · 简历和面试记录只在本会话内可见；请勿上传身份证号等敏感信息。")

if mode == "练习":
    ensure_key()

    # 简历永远以"当前简历"为准：切了简历就同步，避免界面和数据对不上
    _active = profile.active_id()
    if _active and st.session_state.get("resume_uid") != _active:
        st.session_state["resume_uid"] = _active
        st.session_state["resume_text"] = profile.load_resume(_active)
        st.session_state.pop("projects_cache", None)
        st.session_state.pop("force_parse", None)
    if not st.session_state.get("resume_text"):
        saved = profile.load_resume()
        if saved:
            st.session_state["resume_text"] = saved

    with st.sidebar:
        st.markdown("### 练习设置")
        up = st.file_uploader("上传简历（PDF / Word / TXT / 图片）", type=["pdf", "docx", "txt", "jpg", "jpeg", "png"],
                              key="practice_upload")
        if up is not None:
            sig = f"{up.name}:{up.size}"
            # 简历被清空后重新上传同一份文件时，p_sig 还停在上一个值，这里要能重新解析
            if st.session_state.get("p_sig") != sig or not st.session_state.get("resume_text"):
                text, err = parse_upload(up)
                st.session_state["p_sig"] = sig
                if text:
                    analytics.track("resume_parse", ok=True, chars=len(text))
                    st.session_state["resume_text"] = text
                    st.session_state["resume_uid"] = profile.add_resume(text, up.name)
                    st.session_state.pop("practice", None)
                    st.session_state.pop("projects_cache", None)   # 换了简历要重新解析
                    st.session_state.pop("parse_done", None)
                else:
                    st.warning(err)

        resume = st.session_state.get("resume_text", "")

        # 示例简历（虚构）：没有简历的人也要能一键体验，所以放在 if resume 外面。
        if st.button("🎬 示例简历（虚构）· 一键体验", use_container_width=True,
                     help="用一份虚构的 AI 产品经理简历，直接开始练习"):
            profile.set_active("")          # 别让上一份真实简历在下一页把示例覆盖掉
            st.session_state["resume_text"] = demo_resume.DEMO_TEXT
            st.session_state["resume_uid"] = demo_resume.DEMO_ID
            st.session_state["from_demo"] = True
            st.session_state["practice_home"] = True          # 不要恢复任何旧对话
            for k in ("projects_cache", "mini", "practice", "p_sig", "upload_sig"):
                st.session_state.pop(k, None)
            st.session_state["force_parse"] = True
            st.rerun()

        if resume:
            meta = profile.load()
            if st.session_state.get("from_demo") or st.session_state.get("resume_uid") == demo_resume.DEMO_ID:
                fname, when = "示例简历（虚构）", ""
            else:
                fname = _pretty_name(meta.get("resume_from") or "简历")
                when = meta.get("updated_at") or ""
            with st.container(border=True):
                st.caption("当前简历")
                st.markdown(f"**{fname}**")
                st.caption(f"{len(resume)} 字 · {when}")
            if st.button("重新解析经历", use_container_width=True):
                st.session_state.pop("projects_cache", None)
                st.session_state["force_parse"] = True
                st.rerun()

            history = [h for h in profile.list_resumes()
                       if h["id"] not in (profile.active_id(), demo_resume.DEMO_ID)]
            if history:
                with st.expander(f"历史简历（{len(history)}）"):
                    for h in history:
                        if st.button(f"{h['filename']} · {h['chars']} 字", key=f"hist_{h['id']}",
                                     use_container_width=True):
                            profile.set_active(h["id"])
                            st.session_state["resume_text"] = profile.load_resume(h["id"])
                            st.session_state.pop("projects_cache", None)
                            st.session_state.pop("mini", None)
                            st.session_state.pop("practice", None)
                            st.session_state["force_parse"] = True
                            st.rerun()

        else:
            st.caption("先上传简历，才能按经历出题")
        jd = st.text_area("目标岗位 JD（可选）", height=110, key="jd_input")

        if st.session_state.get("confirm_clear"):
            @st.dialog("清空所有练习记录？")
            def _clear_dialog():
                st.caption("会删除所有对话与练习记录，不可恢复。简历不受影响。")
                c1, c2 = st.columns(2)
                if c1.button("取消", use_container_width=True):
                    st.session_state.pop("confirm_clear", None)
                    st.rerun()
                if c2.button("确认清空", type="primary", use_container_width=True):
                    n = mini_mod.clear_history()
                    st.session_state.pop("confirm_clear", None)
                    for k in ("mini", "practice"):
                        st.session_state.pop(k, None)
                    st.session_state["practice_home"] = True
                    st.session_state["_cleared_n"] = n
                    st.rerun()

            _clear_dialog()

    practice_ui.render(resume, jd)
    _attr = bank.attribution()
    if _attr:
        st.caption(_attr)
    st.stop()

# ---------------------------------------------------------------- 恢复历史面试
# 必须放在侧边栏控件之前：控件的初值要从历史记录里回填。

# 时序：这里必须早于侧边栏控件创建。按钮回调只记「要打开哪一场」，
# 真正的回填写在下一轮脚本最开头——那时 jd_input / pace_fast 还没被实例化。
skip_resume = st.session_state.pop("_new_session", False)
pending_id = st.session_state.pop("_pending_session", None)
restore = store.load(pending_id) if pending_id else None
auto_resumed = False
if restore is None and not skip_resume and "engine" not in st.session_state:
    restore = store.latest_unfinished()
    auto_resumed = restore is not None

if restore:
    st.session_state["resume_text"] = restore.get("resume", "")
    st.session_state["jd_input"] = restore.get("jd", "")
    st.session_state["pace_fast"] = restore.get("pace") == "fast"


# ---------------------------------------------------------------- 侧边栏

with st.sidebar:
    st.markdown("### 面试前准备")
    status_slot = st.sidebar.container()   # 当前环节常驻显示，等引擎就绪后填充

    _img_types = ["jpg", "jpeg", "png"] if ocr.available() else []   # 图片识别依赖 macOS Vision，Linux 上不列出来
    uploaded = st.file_uploader(
        "上传简历（PDF / Word / TXT" + (" / 图片" if _img_types else "") + "）",
        type=["pdf", "docx", "txt"] + _img_types,
    )
    if uploaded is not None:
        sig = f"{uploaded.name}:{uploaded.size}"
        if st.session_state.get("upload_sig") != sig:
            text, err = parse_upload(uploaded)
            st.session_state["upload_sig"] = sig
            if text:
                analytics.track("resume_parse", ok=True, chars=len(text))
                st.session_state["resume_text"] = text
                st.session_state["parse_error"] = ""
            else:
                st.session_state["parse_error"] = err

    resume = st.session_state.get("resume_text", "")
    if resume:
        st.caption(f"✓ 已读入简历 {len(resume)} 字")
    elif st.session_state.get("parse_error"):
        st.warning(st.session_state["parse_error"])
        resume = st.text_area("手动粘贴简历", height=140, key="resume_fallback",
                              placeholder="把简历内容粘到这里…")

    jd = st.text_area("目标岗位 JD", height=96, key="jd_input", placeholder="粘贴岗位描述…")

    if st.button("开始新面试", use_container_width=True):
        for k in ["engine", "report", "saved", "signature"]:
            st.session_state.pop(k, None)
        st.session_state["_new_session"] = True  # 告诉下一轮：别自动恢复，开新的一场
        st.rerun()

    pace_fast = st.toggle("快速模式（9 问，约 5 分钟）", value=False, key="pace_fast",
                          help="完整模式 15 问、约 15-20 分钟；快速模式 9 问，用于快速看完整个闭环（含报告）。")

    ops_slot = st.sidebar.container()   # 本场操作：等引擎就绪后再填充

    # ── 次要区：全部塞进一个折叠区，保证常驻区一屏放得下 ──
    sessions = store.list_sessions(30)
    unfinished = [d for d in sessions if not d.get("finished")]
    st_stats = store.stats()
    with st.expander(f"更多（历史 {len(sessions)} · 数据 · 环境）", expanded=False):
        if unfinished:
            st.caption(f"⏳ {len(unfinished)} 场未完成")
        for d in sessions[:10]:
            sm = store.summary(d)
            status = "✅" if d.get("finished") else "⏳"
            score = f" · {sm['overall_score']} 分" if sm.get("overall_score") else ""
            st.caption(f"{sm['ts'][5:16]} {status} {sm.get('user_turns', 0)} 轮{score}")
            c1, c2 = st.columns(2)
            if c1.button("查看" if d.get("finished") else "继续",
                         key=f"open_{d['id']}", use_container_width=True):
                st.session_state["_pending_session"] = d["id"]
                st.rerun()
            if c2.button("删除", key=f"del_{d['id']}", use_container_width=True,
                         help="删除这场面试（不可恢复）"):
                store.delete(d["id"])
                if st.session_state.get("engine") and st.session_state["engine"].session_id == d["id"]:
                    for k in ["engine", "report", "saved", "signature"]:
                        st.session_state.pop(k, None)
                st.rerun()
        if not sessions:
            st.caption("还没有面试记录")

        st.divider()
        c1, c2 = st.columns(2)
        c1.metric("已完成", st_stats["count"])
        c2.metric("成功率", f"{int(st_stats['completion_rate'] * 100)}%")
        c1.metric("平均轮次", st_stats["avg_turns"])
        c2.metric("单场成本", f"¥{st_stats['avg_cost']:.3f}")
        if st_stats["count"]:
            st.caption(f"累计 ¥{st_stats['total_cost']:.3f} · 平均 {st_stats['avg_duration'] // 60} 分 {st_stats['avg_duration'] % 60} 秒")

        st.divider()
        st.caption(f"解释器：{sys.executable}")
        missing = [m for m in ("pypdf", "docx") if importlib.util.find_spec(m) is None]
        if missing:
            st.error(f"缺少解析依赖：{', '.join(missing)} —— 请用 ./run.sh 启动")



ensure_key()

# ---------------------------------------------------------------- 引擎生命周期

pace = "fast" if pace_fast else "normal"
signature = (pace, hash(resume + "|" + jd))

if restore:
    engine = store.engine_from_session(restore)
    st.session_state["engine"] = engine
    st.session_state["signature"] = signature
    st.session_state["saved"] = store.summary(restore)
    if restore.get("report"):
        st.session_state["report"] = {"ok": True, "report": restore["report"], "raw": "", "error": ""}
    else:
        st.session_state.pop("report", None)
    st.session_state["notice"] = (
        f"已恢复上次未完成的面试：第 {engine.stage_index + 1}/{engine.total_stages} 环节 · "
        f"{engine.stage_label}，继续回答即可。"
        if not engine.finished
        else f"已打开历史面试（{restore.get('updated_at', '')}）的报告。"
    )
else:
    engine = st.session_state.get("engine")
    if engine is None or st.session_state.get("signature") != signature:
        engine = InterviewEngine(resume=resume, jd=jd, pace=pace)
        st.session_state["engine"] = engine
        st.session_state["signature"] = signature
        st.session_state.pop("report", None)
        st.session_state.pop("saved", None)
        with st.spinner("面试官准备中…"):
            engine.open_interview()
        # 不在开场白阶段建档：只有用户真正答了一句才值得保存

# ---------------------------------------------------------------- 侧边栏常驻状态
with status_slot:
    st.caption(f"⏱ {engine.progress_text()}")
    st.divider()

# ---------------------------------------------------------------- 本场操作（侧边栏）
# 放在侧边栏而不是输入框旁边：输入框是主操作区，破坏性操作不该挨着它。
with ops_slot:
    st.divider()
    if engine.finished:
        st.caption("本场已结束")
    else:
        c1, c2 = st.columns(2)
        if c1.button("跳过本环节", use_container_width=True, disabled=engine.stage == "closing",
                     help="直接进入下一个环节。跳环节的权力在你手里。"):
            engine.skip_stage()
            store.save(engine)
            st.rerun()
        if not st.session_state.get("confirm_end"):
            if c2.button("结束面试", use_container_width=True,
                         help="结束后本场不能再作答，会二次确认。"):
                st.session_state["confirm_end"] = True
                st.rerun()
        else:
            st.warning("结束后不能继续作答，确认吗？")
            d1, d2 = st.columns(2)
            if d1.button("确认结束", type="primary", use_container_width=True):
                engine.force_finish()
                engine.finished = True
                st.session_state.pop("confirm_end", None)
                store.save(engine)
                st.rerun()
            if d2.button("取消", use_container_width=True):
                st.session_state.pop("confirm_end", None)
                st.rerun()

# ---------------------------------------------------------------- 头部进度

st.markdown("""
<style>
/* 侧边栏：压缩留白，让常驻区一屏放得下 */
[data-testid="stSidebarContent"] { padding-top: 0 !important; }
[data-testid="stSidebarHeader"] { height: 2rem !important; min-height: 2rem !important; padding: 0 !important; }
[data-testid="stSidebarUserContent"] { padding-top: .6rem !important; padding-bottom: .5rem !important; }
[data-testid="stSidebarUserContent"] [data-testid="stVerticalBlock"] { gap: .35rem; }

/* 隐藏 Streamlit 自带顶栏，页面从顶部开始 */
header[data-testid="stHeader"] { display: none; }
.block-container { padding-top: 1.6rem !important; }
</style>
""", unsafe_allow_html=True)

head_l, head_r = st.columns([3, 2])
with head_l:
    st.markdown("### AI 面试官 · 可溯源模拟面试")
with head_r:
    st.caption(engine.progress_text())

if st.session_state.get("notice"):
    st.caption(st.session_state.pop("notice"))
if not resume.strip():
    st.caption("提示：左侧上传简历后，面试官会针对你的项目追问。")

# ---------------------------------------------------------------- 对话区

# 只有「最后一个问题、且还没被回答」时才允许重新生成
last_idx = len(engine.messages) - 1
can_regen = (
    not engine.finished
    and last_idx >= 0
    and engine.messages[last_idx]["role"] == "assistant"
)

for i, m in enumerate(engine.messages):
    if m["content"].startswith("〔"):
        st.caption(f"⏭  {m['content'].strip('〔〕')}")
        continue
    with st.chat_message("user" if m["role"] == "user" else "assistant"):
        st.markdown(m["content"])
        if can_regen and i == last_idx:
            if st.button("重新生成这个问题", key="regen_btn", type="tertiary", icon="🔄"):
                engine.regenerate_question()
                store.save(engine)
                st.rerun()

if engine.error:
    st.warning(f"有一次模型调用失败，已自动降级继续：{engine.error}")


# ---------------------------------------------------------------- 报告

def render_report(rep: dict, record: dict):
    trace = rep.get("trace", {})
    st.divider()
    st.markdown("## 面试评估报告")

    top = st.columns([1, 1, 1, 2])
    top[0].metric("总分", f"{rep['overall_score']}")
    top[1].metric("结论", rep["verdict"])
    top[2].metric("可溯源", f"{int(trace.get('rate', 0) * 100)}%",
                 help=f"{trace.get('valid', 0)}/{trace.get('total', 0)} 条依据在对话原文中逐字找到")
    top[3].markdown(f"**一句话**：{rep['summary']}")

    st.caption(
        f"本场：{record.get('user_turns', 0)} 轮 · {record.get('duration_s', 0) // 60} 分 {record.get('duration_s', 0) % 60} 秒 · "
        f"{record.get('tokens_input', 0) + record.get('tokens_output', 0)} tokens · "
        f"成本 ¥{record.get('cost_cny', 0):.4f}（按 ¥{PRICING_CNY_PER_1M['input']}/¥{PRICING_CNY_PER_1M['output']} 每百万 token 估算）"
    )

    st.markdown("### 五维评分")
    for d in rep["dimensions"]:
        left, mid, right = st.columns([1, 3, 3])
        left.markdown(f"**{d['name']}**\n\n`权重 {int(d['weight'] * 100)}%`")
        mid.markdown(f"### {d['score']}")
        mid.progress(d["score"] / 100)
        right.write(d["comment"])
        for h in d.get("hit", []):
            right.caption(f"✅ 命中：{h}")
        for m_ in d.get("miss", []):
            right.caption(f"❌ 未命中：{m_}")

        for q in d.get("quotes", []):
            if q.get("verified"):
                right.caption(f"✅ 「{q['text']}」")
            else:
                right.caption(f"⚠️ 未在对话原文中找到，已作废：「{q['text'][:40]}…」")

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("### 优势")
        for item in rep["strengths"]:
            mark = "✅" if item.get("verified") else "⚠️"
            st.markdown(f"- {mark} **{item['point']}**")
            if item.get("quote"):
                st.caption(f"「{item['quote']}」")
    with col_b:
        st.markdown("### 短板")
        for g in rep["gaps"]:
            mark = "✅" if g.get("verified") else "⚠️"
            st.markdown(f"- {mark} **{g['issue']}**")
            if g.get("quote"):
                st.caption(f"「{g['quote']}」")
            st.caption(f"→ {g.get('suggestion', '')}")

    st.markdown("### 下一步行动")
    for i, a in enumerate(rep.get("next_actions", []), 1):
        st.markdown(f"{i}. {a}")


if engine.finished and "report" not in st.session_state:
    with st.spinner("面试官在整理评估报告（会逐条核对你的原话）…"):
        result = generate_report(engine.messages)
        saved = store.save(engine, report=result["report"], report_error=result["error"])
        st.session_state["report"] = result
        st.session_state["saved"] = store.summary(saved)

if "report" in st.session_state:
    result = st.session_state["report"]
    record = st.session_state.get("saved") or {}
    if result["ok"]:
        render_report(result["report"], record)
        st.download_button(
            "下载本场记录（JSON）",
            data=json.dumps({"report": result["report"], "metrics": record}, ensure_ascii=False, indent=2),
            file_name="interview_report.json",
            mime="application/json",
        )
    else:
        st.error(f"报告生成失败：{result['error']}")
        if result.get("raw"):
            with st.expander("模型原始返回（用于排查）"):
                st.code(result["raw"][:2000])

# ---------------------------------------------------------------- 输入

if not engine.finished:
    # 输入框：整块重绘成 DeepSeek 网页版的样子（胶囊形 + 内层无边框 + 圆形发送键）
    st.markdown("""
<style>
/* ---------- 外层：胶囊形容器 ---------- */
[data-testid="stChatInput"] {
  max-width: none;
  width: 100%;
  margin: 0 0 .2rem 0;
  padding: 6px 10px;
  background: Canvas;
  border: 1px solid color-mix(in srgb, CanvasText 13%, transparent);
  border-radius: 22px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, .05);
  transition: border-color .15s ease, box-shadow .15s ease;
}
[data-testid="stChatInput"]:focus-within {
  border-color: color-mix(in srgb, CanvasText 28%, transparent);
  box-shadow: 0 2px 12px rgba(0, 0, 0, .09);
}

/* ---------- 去掉输入框与正文之间的那条空白 ---------- */
[data-testid="stBottomBlockContainer"] {
  padding-top: 0 !important;
  max-width: none !important;
}
[data-testid="stBottom"] > div { padding-top: 0 !important; }
[data-testid="stBottom"] { padding-top: 0 !important; }
/* 对照框已经限高，底部留一点余量即可 */
.block-container { padding-bottom: 1.5rem !important; }

/* ---------- 去掉 Streamlit 自带的内层描边，避免双层框 ---------- */
[data-testid="stChatInput"] div,
[data-testid="stChatInput"] > div {
  border: none !important;
  box-shadow: none !important;
  background: transparent !important;
}

/* 发送键脱离文档流，否则它自己占一行，整个输入框会高出一倍 */
[data-testid="stChatInput"] > div > div { position: relative; }
[data-testid="stChatInput"] > div > div > div:last-child {
  position: absolute;
  right: 0;
  bottom: -2px;
  height: auto !important;
  width: auto !important;
  margin: 0 !important;
}
[data-testid="stChatInputTextArea"] { width: 100% !important; }

/* ---------- 文本区：透明底、贴合胶囊、留白适中 ---------- */
[data-testid="stChatInput"] textarea,
[data-testid="stChatInputTextArea"] {
  background: transparent !important;
  border: none !important;
  padding: 6px 4px !important;
  font-size: 1rem;
  line-height: 1.6;
  min-height: 100%;
  max-height: 120px;
}
[data-testid="stChatInput"] textarea::placeholder {
  color: color-mix(in srgb, CanvasText 45%, transparent);
}

/* ---------- 发送键：圆形，靠右下 ---------- */
[data-testid="stChatInputSubmitButton"] {
  width: 34px !important;
  height: 34px !important;
  min-width: 34px;
  border-radius: 50% !important;
  margin: 0 2px 4px 0;
}

/* ---------- 底部提示文字：居中小字 ---------- */
.ds-hint {
  max-width: none;
  margin: 0 auto .2rem auto;
  text-align: center;
  font-size: .74rem;
  color: color-mix(in srgb, CanvasText 45%, transparent);
}
</style>
""", unsafe_allow_html=True)
    if prompt := st.chat_input("给 AI 面试官发送消息…", height=40):
        engine.answer(prompt)
        store.save(engine)  # 每轮都落盘：误关页面也能接着面
        st.rerun()


if not engine.finished:
    st.markdown('<div class="ds-hint">内容由 AI 面试官生成，仅用于练习参考</div>', unsafe_allow_html=True)

st.caption(
    f"本次已用 {engine.usage['input'] + engine.usage['output']} tokens · "
    f"约 ¥{cost_cny(engine.usage):.4f} · 平均每轮 {(engine.usage['latency_ms'] / max(engine.usage['calls'], 1)) / 1000:.1f} 秒"
)
_attr = bank.attribution()
if _attr:
    st.caption(_attr)
