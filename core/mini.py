"""迷你模拟面试：一段经历、3 轮对话（主问 + 两刀追问）、整链打分。

追问链固定成面试官最经典的三连，但**每一刀的具体问法由候选人的上一句答案生成**：
1. 为什么做这个（业务判断）
2. 为什么选这个方案（取舍：说得出被砍掉的方案吗）
3. 效果怎么验证（结果意识：主指标 + 对照 + 护栏 + 没达预期）

为什么固定这三个意图：这是面过几百人的面试官总结出来的高频路径，
问法可以变，但考察的东西不变——不固定意图，模型会随机乱追。

过程中不打断（真实感），结束后一次性给整链打分，再把 3 个问题变成练习题。
"""

import json
import os
import re
import time

from . import claims as claims_mod
from . import directions as dir_mod
from . import simple as simple_mod
from . import coach as coach_mod
from . import review as review_mod
from . import probe as probe_mod
from . import rubric
from .settings import temp as _t
from .llm import LLMError, chat

ROUNDS = 0          # 0 = 不限轮次（无限练习）；用户随时可以结束

ANGLE_MAP = {
    "info": "why_do",
    "judgment": "why_do",
    "options": "why_this",
    "tradeoff": "why_this",
    "verify": "how_verify",
    "attribution": "how_verify",
    "transfer": "how_verify",
}

INTENTS = [
    ("why_do", "为什么做这个", "考察业务判断：他从什么信号判断这件事值得做（数据信号 / 定性信号 / 优先级）"),
    ("why_this", "为什么选这个方案", "考察取舍：他说不说得出被砍掉的方案，以及为什么砍"),
    ("how_verify", "效果怎么验证", "考察结果意识：主指标、对照组、护栏指标、以及没达预期的部分"),
]

MAIN_PROMPT = """你是一场产品经理面试的面试官，面过几百个候选人。现在由你来问第一个问题。

候选人选了简历里的这段经历：
【经历】{name}
【简历原文】{context}

【你的原则：只问判断，不问事实】
- 事实（做过什么、指标多少）简历上已经写了，再问是浪费时间；
- 你要问的是：他当时**看到了什么、判断了什么、凭什么这么定、怎么知道自己对了**。

【从"决策链"里挑一环来问】（挑这段经历里最有信息量、最可能露馅的那一环）
- info —— 他当时看到什么信息、怎么看到的（数据 / 访谈 / 反馈）
- judgment —— 他从这些信息判断出真正的问题是什么（表象 vs 根因）
- options —— 他考虑过哪些方案
- tradeoff —— 为什么选这个、砍了那个（成本 / 收益 / 效率）
- verify —— 他怎么验证自己的判断是对的（指标、对照、灰度）
- attribution —— 结果变好，他归因到哪几个动作
- transfer —— 这套经验什么条件下成立，换个场景还成不成立

【反面清单｜问这些就是不及格】
- 不问"这个数字是怎么算出来的"——除非这个数明显可疑（口径没说清、提升幅度离谱）；
- 不问简历上已经写明的事实（你们做了啥、用的什么技术）；
- 不问行业默认做法（为什么 7 天、为什么 A/B 各 50%、为什么用周活做分母）；
- 不问"能详细讲讲吗"这类不带指向的话。

【硬规则】
- 一句话，不超过 45 字，口语化；
- 必须点到这段经历里的**具体环节或判断**，不许泛泛；
- 只用简历里出现过的词和数字，不许编。

只输出 JSON：{{"question": "问题", "angle": "info|judgment|options|tradeoff|verify|attribution|transfer"}}"""

FOLLOWUP_PROMPT = """你是产品经理面试官，正在追问（第 {round_no} 轮）。

【这段经历】{name}
【简历原文】{context}
【你上一轮问的】{last_question}
【候选人刚才的回答】
{last_answer}
【这一轮要确认他有没有说清】{intent_name}：{intent_desc}

追问只做一件事：**顺着他的话往下钻一层**，把"说了"钻成"怎么做到的、怎么知道的"。

从下面挑最相关的一个方向：
- 机制：他说的方法/指标/评测，具体是怎么做的（例：机器评测到底怎么评的）
- 归因：结果变好，怎么确定是他说的那几个动作导致的，不是别的因素
- 边界：这套做法什么条件下会失效，换个场景还成不成立

【硬规则】
- 必须引用他说过的一个短语（「」括起来，4–12 字），引用之后要**指向一个判断**，不许只是复述那个词；
- 禁止"能详细讲讲吗""展开说说""再具体一点"这类套话；
- 禁止质疑行业默认做法（7 天窗口、A/B 各 50%、按周复盘、用周活做分母）；
- 禁止问简历上或他刚说过的事实；
- 一句话，不超过 45 字，只问一个问题。

只输出 JSON：{{"question": "追问"}}"""

SCORE_PROMPT = """你是刚才这场面试的面试官，现在要**逐轮**给这段经历的问答打分。

【评分维度】
{dimensions}

【完整对话】
{transcript}

【要求】
1. **对每一轮单独打分**：这一轮的回答，四个维度各给一个 verdict：
   - hit：这一轮有明确证据
   - partial：沾边但没讲透
   - miss：这一轮完全没有
   每个 verdict 给一句理由，理由必须指向这一轮里的具体内容；
2. 从对话里挑出**最该改的 1-2 句原话**（逐字，12–60 字），系统会回查；
3. 给一条"如果重来一次最该补什么"。

只输出 JSON：
{{
  "rounds": [
    {{"round": 1,
      "dimensions": [
        {{"name": "产品思维", "verdict": "hit", "why": "说清了数据信号", "quote": "后台数据显示 60% 的人周计划完成不到一半"}},
        {{"name": "逻辑分析", "verdict": "partial", "why": "因果链断在归因", "quote": "没做对照组"}},
        {{"name": "沟通表达", "verdict": "hit", "why": "先给结论"}},
        {{"name": "数据驱动", "verdict": "partial", "why": "有数字没口径"}}
      ]}},
    {{"round": 2, "dimensions": [ ... ]}},
    {{"round": 3, "dimensions": [ ... ]}}
  ],
  "weak_quotes": ["对话里的逐字片段"],
  "redo": "如果重来最该补的一句话"
}}
"""


_KEEP = re.compile(r"[^\s，。、；：？！“”‘’（）《》【】,.!?;:\"'()\[\]{}<>—\-…·%]")


_REASK = "（这句没答到）刚才那个问题再说一次："
_REASK_RECALL = "（想不起来没关系）给你几个回忆线索再答："
_REASK_STRUCT = "（不知道怎么讲也行）按这个顺序再讲一遍："


def _clean_q(q: str) -> str:
    """重问出来的题，喂给模型时要把"重问"前缀去掉，否则模型会以为那是题目内容。"""
    q = q or ""
    for p_ in (_REASK, _REASK_RECALL, _REASK_STRUCT):
        if q.startswith(p_):
            return q[len(p_):]
    return q



# ---------------------------------------------------------------- 回答类型（规则层）
_NONE_WORDS = ("没做过", "没参与", "不是我负责", "不负责", "没经历过", "没接触过", "不是我做", "没有相关经历")
_RECALL_WORDS = ("记不清", "忘了", "想不起来", "记不得", "不太记得", "没印象")
_STRUCT_WORDS = ("不知道怎么说", "不知道怎么开口", "不知道该怎么说", "不知道怎么组织", "不知道怎么讲")
_UNKNOWN_WORDS = ("不知道", "不清楚", "不了解", "不确定", "说不上来")


def answer_kind(text: str) -> str:
    """规则层：先分边界，再判垃圾。0 延迟、可测。"""
    t = (text or "").strip()
    if any(w in t for w in _NONE_WORDS):
        return "boundary_none"
    if any(w in t for w in _RECALL_WORDS):
        return "boundary_recall"
    if any(w in t for w in _STRUCT_WORDS):
        return "need_structure"
    if any(w in t for w in _UNKNOWN_WORDS):
        return "boundary_unknown"
    if len(t) <= 6:
        has_cn = bool(__import__("re").search(r"[\u4e00-\u9fff]", t))
        # 有汉字、且不是同一个字重复 → 是"太短"，不是垃圾（如「都是」「没有」「对」）
        if has_cn and len(set(t)) >= 2:
            return "too_short"
        return "garbage"
    if len(set(t)) <= 3 or not __import__("re").search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
        return "garbage"
    return "ok"


_BOUNDARY_HINTS = {
    "boundary_unknown": [
        "这题记为「证据不足」，不扣分——不知道就说不知道，比编一个更安全。",
        "可以这样说：这个我不确定，我能确认的是 X；要回答这个问题，我需要查 Y。",
    ],
    "boundary_none": [
        "这题记为「证据不足」，不扣分——你没做过，硬编反而更糟。",
        "可以这样说：这块我没参与，我负责的是 X；能确认的是 Y，Z 需要找同事确认。",
    ],
    "boundary_recall": [
        "先别急着给结论，想想那段时间：先发生了什么、谁提的、交付物是什么。",
        "记不清的部分可以说不确定，但要把确定的那部分说清。",
    ],
    "need_structure": [
        "给你一个顺序：① 要解决的问题 ② 你做的动作 ③ 结果和判断依据。",
        "照着这个顺序说一遍就行，先不用追求说得多漂亮。",
    ],
}


def _norm(s: str) -> str:
    return "".join(ch for ch in _KEEP.findall(s or "")).lower()


def locate(text: str, fragment: str):
    if not text or not fragment:
        return None
    i = text.find(fragment)
    if i >= 0:
        return i, i + len(fragment)
    flat, idx = [], []
    src = "".join(text)
    for i, ch in enumerate(src):
        if _KEEP.match(ch):
            flat.append(ch.lower())
            idx.append(i)
    frag = _norm(fragment)
    if len(frag) < 6:
        return None
    j = "".join(flat).find(frag)
    if j < 0:
        return None
    return idx[j], idx[j + len(frag) - 1] + 1


_STOP = ("我们", "你们", "他们", "这个", "那个", "当时", "然后", "所以", "因为",
         "就是", "没有", "做了", "可以", "一个", "的话", "来说", "其实", "但是",
         "而且", "如果", "就是", "什么", "怎么", "这样", "那样", "一下", "比较")


def salient(answer: str, limit: int = 6) -> list[str]:
    """挑可引用的短语：优先"带数字的完整小句"，其次 6–18 字的完整小句。

    按标点切成小句再挑，不用 n-gram——否则会切出「课率只有2」这种半截词。
    """
    text = re.sub(r"\s+", "", answer or "")
    if not text:
        return []
    clauses = re.split(r"[，。；！？,;!?\n]", text)          # 注意：不能按英文句点切，否则 33.6% 会被切开
    cleaned = []
    for c in clauses:
        for w in ("所以", "因为", "但是", "然后", "而且", "其实", "就是", "那个", "这个", "不过"):
            c = c.removeprefix(w).removesuffix(w)
        cleaned.append(c)
    clauses = cleaned
    clauses = [c for c in clauses if 4 <= len(c) <= 28]
    scored = []
    for c in clauses:
        has_num = bool(re.search(r"\d", c))
        if len(c) > 24:                                  # 太长才截，且保证数字完整
            if has_num:
                m = re.search(r"\d+(?:\.\d+)?%?", c)
                lo = max(0, m.start() - 8)
                hi = min(len(c), max(m.end() + 6, lo + 18))
                c = ("…" if lo > 0 else "") + c[lo:hi] + ("…" if hi < len(c) else "")
            else:
                c = c[:16] + "…"
        score = (2 if has_num else 0) + (1 if 6 <= len(c) <= 18 else 0)
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, c in scored:
        if c not in out and not any(w in c for w in ("什么", "怎么", "这个那个")):
            out.append(c)
        if len(out) >= limit:
            break
    return out


def quotes_answer(question: str, answer: str) -> bool:
    """追问里有没有真的用到候选人的话：出现原答里的数字，或共享 5 字以上片段。"""
    q = re.sub(r"\s+", "", question or "")
    a = re.sub(r"\s+", "", answer or "")
    if not q or not a:
        return False
    for num in set(re.findall(r"\d+(?:\.\d+)?%?", a)):
        if len(num) >= 2 and num in q:
            return True
    size = 5
    if len(a) < size:
        return False
    grams = {a[i:i + size] for i in range(len(a) - size + 1)}
    for i in range(len(q) - size + 1):
        g = q[i:i + size]
        if g in grams and not any(w in g for w in _STOP):
            return True
    return False


_FALLBACK_TPL = {
    ("why_do", False): "你刚才说「{p}」——这个判断当时是怎么得出来的？",
    ("why_do", True): "你提到「{p}」，当时是看到这个信号才决定做的？",
    ("why_this", False): "你提到「{p}」，那当时还有别的做法被砍掉吗？",
    ("why_this", True): "你说「{p}」，这个结果当时还有别的方案能做到吗？",
    ("how_verify", False): "你说「{p}」，这块最后是怎么验证效果的？",
    ("how_verify", True): "你说「{p}」，这个数的口径和对照怎么定的？",
}


_TYPE_QUESTION = {
    "result": "你提到「{p}」，这个数字的口径和基线是什么？",
    "design": "你提到「{p}」，这个设计当时的依据和约束是什么？",
    "judgment": "你说「{p}」，当时凭什么这么判断？排除了什么？",
    "action": "你提到「{p}」，具体你亲手做了哪几件事？",
    "tradeoff": "你说「{p}」，当时还有别的选择吗？为什么选它？",
}

_LIMIT_WORDS = ("没有灰度", "没做灰度", "没有对照组", "没做对照", "无法排除", "不能排除",
                "没法排除", "排除不了", "记不清", "不确定", "没有实验", "做不到严格",
                "霍桑", "存在混杂", "归因打折扣", "相关性证据", "不排除")

_GAP_TPL_BY_ANGLE = {
    "口径": "这个数具体怎么算的？分母和时间窗是什么？",
    "归因": "结果变好，你怎么确定是你这几个动作带来的？",
    "角色": "这部分里哪些是你亲手做的？",
    "取舍": "当时还有别的做法吗？为什么最后选这个？",
    "边界": "这套做法在什么情况下会失效？",
    "机制": "这套方案具体是怎么跑起来的？",
    "验证": "你当时怎么验证它确实有效？",
    "贡献": "几个动作各自的贡献怎么区分？",
}

_GENERIC = ("详细讲讲", "展开说说", "再具体一点", "具体一点", "能说说吗")


def _acknowledges_limit(text: str) -> bool:
    """候选人有没有明确承认"这块做不到严格验证"。"""
    return any(w in (text or "") for w in _LIMIT_WORDS)


_ANGLE_WORDS = {
    "口径": ("口径", "怎么算", "分母", "定义", "怎么统计", "基线", "哪一版"),
    "归因": ("归因", "确认是", "带来的", "排除", "对照", "同期", "别的改动"),
    "角色": ("亲手", "哪部分是你", "独立", "团队做的", "拍板", "拍的板", "谁定", "谁决定", "是你自己"),
    "取舍": ("砍掉", "备选", "别的做法", "为什么选", "取舍"),
    "边界": ("失效", "什么情况下", "约束", "风险", "兜底", "最先崩", "崩在"),
    "机制": ("怎么实现", "原理", "链路", "具体怎么设计", "跑起来", "怎么运转"),
    "验证": ("验证", "怎么判断有效", "指标", "看哪个数"),
    "贡献": ("贡献", "各自", "拆", "增量"),
}


def _angle_of(q: str) -> str:
    """粗分问题角度，用来避免反复问同一类。"""
    for name, words in _ANGLE_WORDS.items():
        if any(w in (q or "") for w in words):
            return name
    return ""


def _status_of(st: dict, i: int) -> str:
    """兼容 int / str 键。"""
    v = (st or {}).get(i) or (st or {}).get(str(i)) or {}
    return v.get("status", "")


def _same_numbers(q: str, history: list[str]) -> bool:
    """新问题如果和之前某个问题用的是同一组数字，就是在问同一件事。"""
    def nums(t):
        return {x for x in re.findall(r"\d+(?:\.\d+)?%?", t or "") if len(x) >= 2}
    qn = nums(q)
    if not qn:
        return False
    return any(qn & nums(h) for h in history)


def _too_similar(q: str, history: list[str], n: int = 10) -> bool:
    """新问题与之前某个问题共享 10 字以上片段，就当成重复。"""
    q = re.sub(r"\s+", "", q or "")
    if len(q) < n:
        return False
    grams = {q[i:i + n] for i in range(len(q) - n + 1)}
    for h in history:
        h = re.sub(r"\s+", "", h or "")
        for i in range(max(0, len(h) - n + 1)):
            if h[i:i + n] in grams:
                return True
    return False


def _is_generic(q: str) -> bool:
    """是不是不接内容的套话。"""
    return any(g in (q or "") for g in _GENERIC)


_GAP_TPL = [
    ("口径", "这个数具体怎么算的？分母、时间窗分别是什么？"),
    ("数据", "这个结论背后的数据是怎么得来的？样本和时间范围是什么？"),
    ("归因", "结果变好，你怎么确定是你这几个动作带来的？"),
    ("动作", "你具体做了哪几件事？各自起了什么作用？"),
    ("角色", "这部分里哪些是你亲手做的，哪些是团队做的？"),
    ("取舍", "当时还有别的做法吗？为什么最后选这个？"),
    ("边界", "这套做法在什么情况下会失效？"),
]


def _gap_question(gap: dict, answer: str) -> str:
    """按缺口造句：缺口是什么，就问什么，避免每轮同一句。"""
    missing = (gap or {}).get("missing", "")
    for key, tpl in _GAP_TPL:
        if key in missing:
            return tpl
    phrases = salient(answer)
    if phrases:
        return f"你刚才说「{phrases[0]}」，这块能再往下说一层吗？具体是怎么做的？"
    return "这块能再说细一点吗？比如你当时具体做了什么。"


def _fallback_followup(answer: str, intent: str = "") -> str:
    """模型没接上原话时的兜底：用候选人自己的片段造句，且贴合本轮考察意图。"""
    phrases = salient(answer)
    if not phrases:
        return "这块能再具体一点吗？比如你当时具体做了什么。"
    p = phrases[0]
    tpl = _FALLBACK_TPL.get((intent, bool(re.search(r"\d", p))))
    if tpl:
        return tpl.format(p=p)
    return f"你刚才说「{p}」，能再具体一点吗？"


def _verified_quote(quote: str, texts: list[str]) -> str:
    """引用必须能在某一轮回答里逐字找到；找不到就置空（不展示假引用）。"""
    quote = (quote or "").strip()
    if not quote:
        return ""
    for t in texts:
        span = locate(t, quote)
        if span:
            return t[span[0]:span[1]]
    return ""


def _json(raw: str):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    for c in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        if not c:
            continue
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


def _ask(prompt: str, temperature: float = 0.7):
    try:
        raw, usage = chat([{"role": "user", "content": prompt}], temperature=temperature, json_mode=True)
    except LLMError:
        return None, None
    return _json(raw), usage


class MiniInterview:
    """一段经历的一次迷你面试。3 轮，不打断，结束后整链打分。"""

    def __init__(self, entry: dict, resume: str = "", jd: str = ""):
        self.entry = entry or {}
        self.resume = resume or ""
        self.jd = jd or ""
        self.questions: list[str] = []
        self.claims: list[dict] = []              # 这段经历的待验证主张
        self.claim_status: list[dict] = []        # 每轮回答后，各主张的证明状态
        self.gaps: list[dict] = []                # 每轮指出的缺口
        self.probed: dict[str, int] = {}          # 每条主张追问过几次（同一主张最多 2 刀）
        self.limited: dict[str, bool] = {}        # 哪些主张候选人已承认"做不到严格验证"
        self.intents: list[str] = []               # 每题的问题类型（决定用哪套固定清单）
        self.answers: list[str] = []
        self.evals: list[dict] = []               # 每轮回答的即时评价（三指标/纠错/润色）
        self.fmt: int = 2                          # 存档格式：2 = 有方向（旧档为 0/1）
        self.resume_key: str = ""                 # 这份对话对应哪份简历（换简历别串台）
        self.directions: list[dict] = []          # 这段经历切出来的方向（小经历）
        self.cur_dir: int = 0                     # 当前方向
        self.q_dirs: list[int] = []               # 每个问题属于哪个方向
        self.angles: list[str] = []               # 已经用过的提问角度（防止换角度时重复）
        self.retries: dict[str, dict] = {}        # 同题重答：{题号: {resolved, left, answer}}
        self.idx = 0                 # 当前第几轮（0 起）
        self.finished = False
        self.report = None
        self.error = ""
        self.session_id = time.strftime("%Y%m%d%H%M%S")
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = self.created_at

    def touch(self):
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------------------------------------------------------------- 流程

    def start(self) -> bool:
        name = self.entry.get("name", "")
        if not name:
            self.error = "没有选中要面的经历"
            return False
        import hashlib
        self.resume_key = hashlib.md5((self.resume or "").encode("utf-8")).hexdigest()[:8]
        context = self.entry.get("context") or self.entry.get("evidence", "")
        dirs = dir_mod.split(name, context)
        if dirs:
            self.directions = dirs
        hint = self._dir_hint() if self.directions else ""
        res = simple_mod.next_question(name, context, self.jd,
                                       [], [], variant="v9") if not hint else \
            simple_mod.next_question(name, f"{hint}\n\n{hint}", self.jd, [], [], variant="v9")
        q = (res or {}).get("question", "").strip()
        angle = "judgment"
        if not q:                                     # 兜底：老的主问
            data, _ = _ask(MAIN_PROMPT.format(name=name, context=context), _t('question', 0.6))
            q = (data or {}).get("question", "").strip()
            angle = str((data or {}).get("angle", "")).strip().lower() or "judgment"
        if not q:
            self.error = "题目生成失败，请重试"
            return False
        self.questions = [q]
        self.q_dirs = [0]
        self.intents = [ANGLE_MAP.get(angle, INTENTS[0][0])]
        self.answers = []
        self.idx = 0
        return True

    def answer(self, text: str) -> bool:
        """记录一轮回答，再用 v8(v9) 生成下一问。"""
        text = (text or "").strip()
        if not text:
            return False
        self.answers.append(text)

        # 评价 + 出下一题：两件事互不依赖，并行跑（延迟取较慢的那个）
        from concurrent.futures import ThreadPoolExecutor

        def _eval():
            try:
                return coach_mod.check(
                    _clean_q(self.questions[len(self.answers) - 1]), text,
                    intent=self.intents[len(self.answers) - 1]
                    if len(self.intents) >= len(self.answers) else "")
            except Exception as e:
                return {"error": str(e)[:80]}

        kind = answer_kind(text)
        base_q = _clean_q(self.questions[len(self.answers) - 1]) if self.questions else ""

        if kind in _BOUNDARY_HINTS:
            # 到达能力边界：不调模型、不计分，按类别给不同处理
            ev = {
                "unscored": True, "boundary": kind,
                "boundary_label": {"boundary_none": "没做过", "boundary_unknown": "不知道",
                                   "boundary_recall": "想不起", "need_structure": "不知道怎么说"}[kind],
                "hints": _BOUNDARY_HINTS[kind], "issues": [], "metrics": [],
                "one_line": "这题记为证据不足，不计分",
                "facts": [], "model_answer": "", "replacements": [], "rewrite": "", "elements": [],
            }
            if kind == "boundary_recall":
                q = _REASK_RECALL + base_q
            elif kind == "need_structure":
                q = _REASK_STRUCT + base_q
            else:
                alt = self._pick_angle(["角色", "机制", "边界", "贡献", "取舍"])
                q = "换个角度：" + _GAP_TPL_BY_ANGLE.get(alt, "你当时具体做了哪几件事？")
        elif kind == "too_short":
            ev = _eval() or {}
            ev.pop("garbage", None)          # 太短 ≠ 垃圾，别让界面说"没看出在回答"
            ev["too_short"] = True
            ev.setdefault("raw", text[:20])
            q = self._expand_question(text)
        elif kind == "garbage":
            # 乱答/空答：不拿它当素材出题，直接重问原来那个问题
            ev = _eval() or {}
            ev["garbage"] = True
            ev.setdefault("raw", text[:20])
            # 垃圾回答不再重问同一题：换个角度，别让用户卡在一个问题上
            alt = self._pick_angle(["角色", "机制", "边界", "贡献", "取舍", "验证"])
            q = "换个角度：" + _GAP_TPL_BY_ANGLE.get(alt, "你当时具体做了哪几件事？")
        else:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_ev = ex.submit(_eval)
                f_q = ex.submit(self._next_question)
                ev = f_ev.result()
                q = f_q.result()

        self.evals.append(ev or {})
        if not q:
            self.error = "下一问生成失败，这一轮可以重试"
            self.answers.pop()
            self.evals.pop()
            return False

        self.questions.append(q)
        self.q_dirs.append(self.cur_dir)
        self.intents.append("why_this")
        self.idx = len(self.answers)
        self.touch()
        return True

    def reanswer(self, idx: int, text: str) -> bool:
        """针对同一道题再答一次，并比较原来的问题有没有解决（不生成新题）。"""
        text = (text or "").strip()
        if not text or not (0 <= idx < len(self.questions)):
            return False
        ev = coach_mod.check(self.questions[idx], text,
                             intent=self.intents[idx] if idx < len(self.intents) else "")
        if not ev:
            return False
        before = (self.evals[idx].get("issues") or []) if idx < len(self.evals) else []
        after = ev.get("issues") or []

        def _kw(t: str) -> set[str]:
            return {t[i:i + 4] for i in range(max(0, len(t) - 3))}

        left = []
        resolved = []
        for it in before:
            prob = it.get("problem", "")
            hit = any(len(_kw(prob) & _kw(n.get("problem", ""))) >= 2 for n in after)
            (left if hit else resolved).append(prob[:40])

        self.retries[str(idx)] = {"resolved": resolved, "left": left,
                                  "answer": text[:400], "ev": ev}
        self.touch()
        return True

    def _pick_angle(self, prefer: list[str] | None = None) -> str:
        """挑一个还没用过的角度，并记下来。"""
        order = prefer or ["角色", "机制", "边界", "贡献", "取舍", "验证", "归因", "口径"]
        used = set(self.angles[-6:])
        pick = next((a for a in order if a not in used), "")
        if pick:
            self.angles.append(pick)
        return pick

    def _expand_question(self, text: str) -> str:
        """太短的回答：顺着他的话拆开问，别丢掉他刚说的。"""
        base = _clean_q(self.questions[len(self.answers) - 1]) if self.questions else ""
        prompt = (
            "你是面试官。候选人刚才只回了很短一句，但方向是对的。请顺着他的话，问一个更具体的问题，"
            "让他把这句话展开成事实。\n"
            f"面试官的问题：{base}\n候选人的简短回答：{text}\n"
            "要求：一句话，≤40 字；必须引用他的原话；不要问新的角度，只让他展开这一句。"
            '只输出 JSON：{"question": "你的问题"}'
        )
        data, _ = _ask(prompt, _t("question", 0.5))
        q = ((data or {}).get("question") or "").strip()
        if q:
            return q
        return f"你刚说「{text[:12]}」，那就从这块展开一句：你具体做了什么、结果怎样？"

    def _next_question(self) -> str:
        """v8 出题 + 去重护栏（与历史问题太像就重写一次，再不行用方向模板）。"""
        name = self.entry.get("name", "")
        ctx = self._dir_hint() or (self.entry.get("context") or self.entry.get("evidence", ""))
        hist = [{"q": _clean_q(self.questions[i]), "a": self.answers[i]}
                for i in range(min(len(self.questions), len(self.answers)))]
        hist = hist[-3:]
        asked = self.questions[-8:]

        res = simple_mod.next_question(name, ctx, self.jd, hist, asked, variant="v8")
        q = (res or {}).get("question", "").strip()

        # 角度去重：和上一问同一角度就重写一次
        last_angle = _angle_of(self.questions[-1]) if self.questions else ""
        if q and last_angle and _angle_of(q) == last_angle:
            res2 = simple_mod.next_question(
                name, ctx, self.jd, hist,
                asked + [f"（上一问已经在问「{last_angle}」，这一问必须换一个角度）"],
                variant="v8")
            q2 = (res2 or {}).get("question", "").strip()
            if q2 and _angle_of(q2) != last_angle:
                q = q2
            else:
                # 重写也换不过来 → 用没用过的角度模板兜一个
                used = {_angle_of(x) for x in self.questions[-3:]}
                alt = next((a for a in _GAP_TPL_BY_ANGLE if a not in used), "")
                if alt:
                    q = _GAP_TPL_BY_ANGLE[alt]

        if q and _too_similar(q, self.questions):
            res2 = simple_mod.next_question(
                name, ctx, self.jd, hist,
                asked + ["（上面这几问已经被判重复，必须换一个完全不同的点）"],
                variant="v8")
            q2 = (res2 or {}).get("question", "").strip()
            q = q2 if q2 and not _too_similar(q2, self.questions) else ""
        if not q:
            d = self.directions[self.cur_dir] if self.directions else {}
            q = _TYPE_QUESTION.get("judgment", "这件事里，哪个决定是你自己拍的板？")
            if d.get("result"):
                q = f"你说「{d.get('result','')[:18]}」，这中间哪一步是你自己拍板的？"
        return q


    def _proven_count(self) -> int:
        if not self.claim_status:
            return 0
        st = self.claim_status[-1]
        return sum(1 for i in range(len(self.claims)) if _status_of(st, i) == "proven")

    def _no_new_info(self) -> bool:
        """连续两轮，已证明的主张数没变 → 再问也挖不出东西。"""
        if len(self.claim_status) < 2:
            return False
        prev, cur = self.claim_status[-2], self.claim_status[-1]
        if not prev or not cur:          # 这一轮没拿到有效判定，不做结论
            return False

        def rank(st, i):
            v = _status_of(st, i)
            return 2 if v == "proven" else 1 if v == "partial" else 0

        # 只要有任意一条主张状态变好，就算有新信息
        improved = any(rank(cur, i) > rank(prev, i) for i in range(len(self.claims)))
        return not improved

    def _remaining_low_risk(self) -> bool:
        """剩下没证明的主张都是低风险 → 不值得再追。"""
        if not self.claim_status:
            return False
        st = self.claim_status[-1]
        if not st:
            return False
        for i, c in enumerate(self.claims):
            v = _status_of(st, i)
            if v == "proven":
                continue
            if v == "partial":                               # 聊到一半的还要追
                return False
            if c.get("tags"):                                # 有风险标签 → 高价值
                return False
        return True

    def stop_reason(self) -> str:
        """为什么停：全证明 / 无新信息 / 剩余低风险 / 到上限。"""
        if not self.claim_status:
            return ""
        if all(self.claim_status[-1].get(i, {}).get("status") == "proven" for i in range(len(self.claims))):
            return "关键主张都已被证明"
        if self._no_new_info():
            return "连续两轮没有新的有效信息"
        if self._remaining_low_risk():
            return "剩下的主张风险都不高，再问收益不大"
        if self.claims and all(self.probed.get(str(i), 0) >= 2 for i in range(len(self.claims))):
            return "每条主张都问过两遍了"
        return ""

    def _dir_hint(self) -> str:
        if not self.directions:
            return ""
        d = self.directions[self.cur_dir]
        return f"{d['name']}（问题：{d.get('problem','')}｜动作：{d.get('action','')}｜结果：{d.get('result','')}）"

    def _ask_direction(self, i: int) -> str:
        """给某个方向生成第一问（换方向和清空后都用它）。"""
        d = self.directions[i]
        ctx = (f"【只问这个方向】{d['name']}：问题={d.get('problem','')}；"
               f"动作={d.get('action','')}；结果={d.get('result','')}。原文：{d.get('source','')}")
        res = simple_mod.next_question(self.entry.get("name", ""), ctx, self.jd,
                                       [], self.questions[-6:], variant="v9")
        q = (res or {}).get("question", "").strip()
        if not q or _too_similar(q, self.questions):
            q = f"你说的「{d['name']}」这件事，当时最初是怎么判断要做的？"
        return q

    def switch_direction(self, i: int) -> bool:
        """换一个方向，用 v9 围绕新方向出第一问。"""
        if not (0 <= i < len(self.directions)) or i == self.cur_dir:
            return False
        self.cur_dir = i
        self.angles = []          # 换方向后角度重新开始
        self.questions.append(self._ask_direction(i))
        self.q_dirs.append(i)
        self.intents.append("why_do")
        self.touch()
        return True

    def clear_direction(self, i: int) -> int:
        """清空某个方向的全部问答；清完补一道新题，别让页面空着。"""
        keep = [k for k, d in enumerate(getattr(self, "q_dirs", [])) if d != i]
        removed = len(self.questions) - len(keep)
        if removed <= 0:
            return 0
        self.questions = [self.questions[k] for k in keep]
        self.q_dirs = [self.q_dirs[k] for k in keep]
        if len(self.intents) >= len(keep):
            self.intents = [self.intents[k] for k in keep]
        self.answers = [self.answers[k] for k in keep if k < len(self.answers)]
        self.evals = [self.evals[k] for k in keep if k < len(self.evals)]
        self.idx = len(self.answers)

        if self.directions and not any(d == i for d in self.q_dirs):
            self.questions.append(self._ask_direction(i))
            self.q_dirs.append(i)
            self.intents.append("why_do")

        self.touch()
        return removed


    def _new_angle_question(self) -> str:
        """主张聊透了之后，换一个新角度继续问（不让对话结束）。"""
        asked = "；".join(self.questions[-6:])
        data, _ = _ask(MAIN_PROMPT.format(
            name=self.entry.get("name", ""),
            context=(self.entry.get("context") or self.entry.get("evidence", "")),
        ), _t('question', 0.7))
        q = (data or {}).get("question", "").strip()
        if not q or q in self.questions:
            q = "这段经历里，如果让你现在重做一遍，你会最先改哪一步？为什么？"
        return q

    def _switch_claim(self, gap: dict, last_answer: str) -> str:
        """换一条还没聊过的主张，按它的类型造一句追问。"""
        cur = gap.get("index")
        for i, c in enumerate(self.claims):
            if i == cur:
                continue
            if self.probed.get(str(i), 0) >= 2:
                continue
            tpl = _TYPE_QUESTION.get(c.get("type", ""), "这条你能具体说说吗？")
            return tpl.format(p=c.get("text", "")[:24])
        return ""

    def _review_and_fix(self, q: str, last_answer: str, gap: dict) -> str:
        """审稿：不合格就带着理由重写一次；还不行就用按缺口造的句子。"""
        rv = review_mod.check(q, self.entry.get("name", ""), self.questions[-1], last_answer)
        if rv.get("ok"):
            return q

        hint = rv.get("fix_hint") or "换个问法：抓住他刚说的一个点，问判断不问事实"
        hist = [{"q": _clean_q(self.questions[i]), "a": self.answers[i]}
                for i in range(min(len(self.questions), len(self.answers)))]
        res2 = probe_mod.next_probe(self.entry.get("name", ""), self.claims, hist,
                                    last_answer + f"\n\n【上一版追问被退回：{hint}，请换个问法】")
        q2 = (res2.get("question") or "").strip()
        if not q2 or _is_generic(q2):
            return _gap_question(gap, last_answer)

        rv2 = review_mod.check(q2, self.entry.get("name", ""), self.questions[-1], last_answer)
        return q2 if rv2.get("ok") else _gap_question(gap, last_answer)

    def finish(self):
        self.finished = True
        self.idx = max(len(self.questions) - 1, 0)
        self.report = self.score()
        self.touch()

    def score(self):
        all_answers = "\n".join(self.answers)
        transcript = "\n".join(
            f"[第 {i+1} 轮]\n面试官：{q}\n候选人：{a}" for i, (q, a) in enumerate(zip(self.questions, self.answers))
        )
        data, _ = _ask(SCORE_PROMPT.format(
            dimensions=rubric.describe_for_prompt(),
            transcript=transcript,
        ), 0.3)
        if not isinstance(data, dict):
            return None

        raw_rounds = {int(r.get("round", 0)): r for r in (data.get("rounds") or []) if isinstance(r, dict)}
        rounds, total_hits = [], 0.0
        for i in range(len(self.questions)):
            got = {str(d.get("name", "")).strip(): d
                   for d in ((raw_rounds.get(i + 1) or {}).get("dimensions") or []) if isinstance(d, dict)}
            dims = []
            for name in rubric.names():
                d = got.get(name, {})
                verdict = str(d.get("verdict", "")).strip().lower()
                if verdict not in ("hit", "partial", "miss"):
                    verdict = "miss"
                quote = _verified_quote(str(d.get("quote", "")), self.answers)
                dims.append({"name": name, "verdict": verdict, "why": str(d.get("why", ""))[:50],
                             "quote": quote})
            hit = sum(1.0 if d["verdict"] == "hit" else 0.5 if d["verdict"] == "partial" else 0 for d in dims)
            total_hits += hit
            rounds.append({"round": i + 1, "question": self.questions[i],
                           "answer": self.answers[i] if i < len(self.answers) else "",
                           "dimensions": dims, "score": round(hit, 1), "total": len(dims)})

        all_text = all_answers
        quotes = []
        for q in (data.get("weak_quotes") or [])[:2]:
            q = str(q).strip()
            span = locate(all_text, q)
            quotes.append({"text": all_text[span[0]:span[1]] if span else q, "verified": span is not None})

        n = len(rounds) * len(rubric.names())
        return {
            "rounds": rounds,
            "weak_quotes": quotes,
            "redo": str(data.get("redo", ""))[:80],
            "score": round(total_hits, 1),
            "total": n,
        }

    # ---------------------------------------------------------------- 产出

    def practice_items(self) -> list[dict]:
        """把这 3 个问题变成练习题（按面试里的原始顺序）。"""
        out = []
        for i, q in enumerate(self.questions):
            out.append({
                "text": q,
                "round": i + 1,
                "intent": self.intents[i] if i < len(self.intents) else "",
                "your_answer": self.answers[i] if i < len(self.answers) else "",
            })
        return out

    # ---------------------------------------------------------------- 序列化

    _FIELDS = ("entry", "resume", "jd", "questions", "claims", "claim_status", "gaps", "evals", "directions", "cur_dir", "q_dirs", "resume_key", "fmt", "retries", "angles",
               "intents", "answers", "idx", "finished",
               "report", "error", "session_id", "created_at", "updated_at")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> "MiniInterview":
        m = cls(entry=d.get("entry") or {}, resume=d.get("resume", ""), jd=d.get("jd", ""))
        m.fmt = 0                       # 旧档默认 0；文件里带了才用文件的值
        for k in cls._FIELDS:
            if k in d:
                setattr(m, k, d[k])
        # 旧版本存档可能缺字段/类型不对：这里统一兜底，坏档不能让首页起不来
        for k in ("questions", "claims", "claim_status", "gaps", "evals",
                  "directions", "q_dirs", "intents", "answers"):
            if not isinstance(getattr(m, k, None), list):
                setattr(m, k, [])
        if not isinstance(getattr(m, "cur_dir", None), int):      # 标量字段别被当列表清掉
            m.cur_dir = 0
        if not isinstance(getattr(m, "resume_key", None), str):
            m.resume_key = ""
        if not isinstance(getattr(m, "fmt", None), int):
            m.fmt = 0
        try:
            # JSON 存盘会把 int 键变成字符串，读回来统一转回 int
            m.claim_status = [{int(k): v for k, v in (st or {}).items()} for st in m.claim_status]
        except (AttributeError, TypeError, ValueError):
            m.claim_status = []
        try:
            m.probed = {str(k): v for k, v in (m.probed or {}).items()}
        except (AttributeError, TypeError):
            m.probed = {}
        return m


# ---------------------------------------------------------------- 落盘（刷新能接着面）

ROOT = os.environ.get("INTERVIEW_DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dir() -> str:
    """公开模式下按访问者隔离，本地模式沿用 records/mini。"""
    from . import scope

    return scope.user_dir("mini")


def _mark_current(session_id: str) -> None:
    """记下"当前正在看的那一场"，刷新后优先恢复到它。"""
    try:
        with open(os.path.join(_dir(), "_current.json"), "w", encoding="utf-8") as f:
            json.dump({"id": session_id}, f)
    except OSError:
        pass


def current():
    """刷新后优先恢复的那一场（_current.json 指向）。"""
    path = os.path.join(_dir(), "_current.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            sid = json.load(f).get("id", "")
    except (OSError, json.JSONDecodeError):
        return None
    return load(sid) if sid else None


def save(m: MiniInterview) -> str:
    os.makedirs(_dir(), exist_ok=True)
    path = os.path.join(_dir(), f"{m.session_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m.to_dict(), f, ensure_ascii=False, indent=2)
    _mark_current(m.session_id)
    return path


def load(session_id: str):
    path = os.path.join(_dir(), f"{session_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return MiniInterview.from_dict(data) if isinstance(data, dict) else None
    except Exception:
        # 坏档/旧 schema 一律当没有，绝不能让恢复流程把整个页面炸掉
        return None


def latest():
    """最近一场（不管完没完）。刷新后用它恢复。"""
    if not os.path.isdir(_dir()):
        return None
    files = sorted(os.listdir(_dir()), reverse=True)
    for name in files:
        if name.endswith(".json") and not name.startswith("_"):
            m = load(name[:-5])
            if m is not None:
                return m
    return None


def find_for(entry_name: str):
    """这段经历最近的一次面试（不管完没完）。列表页用它决定按钮叫「接着面」还是「重新面」。"""
    if not entry_name or not os.path.isdir(_dir()):
        return None
    for name in sorted(os.listdir(_dir()), reverse=True):
        if not name.endswith(".json"):
            continue
        m = load(name[:-5])
        if m is not None and (m.entry or {}).get("name") == entry_name:
            return m
    return None


def clear_history() -> int:
    """清空所有对话练习记录（含当前指针）。返回删除的文件数。"""
    import glob
    n = 0
    for p in glob.glob(os.path.join(_dir(), "*.json")):
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    return n
