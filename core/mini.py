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
from . import coach as coach_mod
from . import review as review_mod
from . import probe as probe_mod
from . import rubric
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
    "口径": ("口径", "怎么算", "分母", "定义", "怎么统计"),
    "归因": ("归因", "确认是你", "排除", "对照", "对照组"),
    "角色": ("亲手", "哪部分是你", "独立", "团队做的"),
    "取舍": ("砍掉", "备选", "别的做法", "为什么选"),
    "边界": ("失效", "什么情况下", "约束", "风险"),
    "机制": ("怎么实现", "原理", "链路", "具体怎么设计"),
    "验证": ("验证", "怎么判断有效", "指标"),
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
        context = self.entry.get("context") or self.entry.get("evidence", "")
        got = claims_mod.extract(name, context)
        if got and got.get("claims"):
            self.claims = got["claims"]
            q = (got.get("question") or "").strip()
            angle = "judgment"
        else:                                        # 抽不出主张就退回旧的主问
            data, _ = _ask(MAIN_PROMPT.format(name=name, context=context), 0.6)
            q = (data or {}).get("question", "").strip()
            angle = str((data or {}).get("angle", "")).strip().lower() or "judgment"
        if not q:
            self.error = "题目生成失败，请重试"
            return False
        self.questions = [q]
        self.intents = [ANGLE_MAP.get(angle, INTENTS[0][0])]
        self.answers = []
        self.idx = 0
        return True

    def answer(self, text: str) -> bool:
        """记录一轮回答；还有主张没被证明就按缺口追问，证明完了或到上限就打分。"""
        text = (text or "").strip()
        if not text or self.finished or not self.questions:
            return False
        self.answers.append(text)

        # 即时评价：这一轮说成什么样（三指标 + 逐句替换 + 润色）
        try:
            ev = coach_mod.check(self.questions[len(self.answers) - 1], text,
                                 intent=self.intents[len(self.answers) - 1]
                                 if len(self.intents) >= len(self.answers) else "")
            self.evals.append(ev or {})
        except Exception as e:
            self.evals.append({"error": str(e)[:80]})

        # 有主张：走"缺口驱动"；没主张：退回旧的固定追问
        if self.claims:
            history = [{"q": self.questions[i], "a": self.answers[i]}
                       for i in range(min(len(self.questions), len(self.answers)))]
            used = [a for a in (_angle_of(q) for q in self.questions[:-1]) if a]
            res = probe_mod.next_probe(self.entry.get("name", ""), self.claims,
                                       history, text, used_angles=used)
            if res:
                self.claim_status.append(res.get("statuses") or {})
                if res.get("gap"):
                    self.gaps.append(res["gap"])
                exhausted = all(self.probed.get(str(i), 0) >= 2 for i in range(len(self.claims)))
                if res.get("all_proven") or exhausted:
                    # 对话练习没有终点：这套主张问透了，就换个角度再开一问
                    nxt = self._new_angle_question()
                    if nxt:
                        self.questions.append(nxt)
                        self.intents.append("why_do")
                        self.idx = len(self.answers)
                        self.touch()
                        return True
                q = (res.get("question") or "").strip()
                if q:
                    if _is_generic(q) or not q:
                        q = _gap_question(res.get("gap") or {}, text)
                    else:
                        q = self._review_and_fix(q, text, res.get("gap") or {})

                gap = res.get("gap") or {}
                idx = gap.get("index")
                if isinstance(idx, int) and idx >= 0:
                    self.probed[str(idx)] = self.probed.get(str(idx), 0) + 1

                # 他已经承认这块做不到严格验证 → 不再追问"怎么排除"，改问"当时实际怎么判断的"
                locked = False
                if isinstance(idx, int) and idx >= 0 and _acknowledges_limit(text):
                    self.limited[str(idx)] = True
                    q = "你说这块没法严格验证——那当时你实际是怎么判断这个改动有效的？"
                    locked = True

                # 重复追问同一个点（或同一主张已追两次）→ 换一条主张
                # 追问一定要先"接住"他说的话；没接住就用他的原话补一个开头
                if q and not any(k in q for k in ("你说", "你提到", "你刚才", "「")):
                    ph = salient(text)
                    if ph:
                        q = f"你说「{ph[0]}」，" + q

                if q and not locked and _angle_of(q) and used.count(_angle_of(q)) >= 2 + 1:
                    alt_angle = next((a for a in _ANGLE_WORDS if a not in used), "")
                    if alt_angle:
                        q = _GAP_TPL_BY_ANGLE.get(alt_angle, q)

                if q and not locked and (_too_similar(q, self.questions)
                                         or _same_numbers(q, self.questions)
                                         or (isinstance(idx, int) and self.probed.get(str(idx), 0) > 2)):
                    alt = self._switch_claim(gap, text)
                    if alt:
                        q = alt

                # 换到另一条主张时，先说一句收束，别像翻简历
                prev_idx = self.gaps[-2].get("index") if len(self.gaps) >= 2 else None
                if isinstance(idx, int) and prev_idx is not None and idx != prev_idx:
                    q = "这块我大概清楚了，换一个问问——" + q
                    self.questions.append(q)
                    self.intents.append("why_this")
                    self.idx = len(self.answers)
                    self.touch()
                    return True

        it = min(len(self.answers), len(INTENTS) - 1)      # 轮次上限 4 > 意图数 3，取最后一个兜底
        key, intent_name, intent_desc = INTENTS[it]
        data, _ = _ask(FOLLOWUP_PROMPT.format(
            round_no=len(self.answers) + 1,
            name=self.entry.get("name", ""),
            context=self.entry.get("context") or self.entry.get("evidence", ""),
            last_question=self.questions[-1],
            last_answer=text,
            intent_name=intent_name,
            intent_desc=intent_desc,
        ), 0.6)
        q = (data or {}).get("question", "").strip()
        if not q:
            self.error = "追问生成失败，这一轮可以重试"
            self.answers.pop()
            return False

        if not quotes_answer(q, text):
            retry, _ = _ask(FOLLOWUP_PROMPT.format(
                round_no=len(self.answers) + 1,
                name=self.entry.get("name", ""),
                context=self.entry.get("context") or self.entry.get("evidence", ""),
                last_question=self.questions[-1],
                last_answer=text,
                intent_name=intent_name,
                intent_desc=intent_desc + "｜**必须原样引用候选人说过的 4–12 个字**，用「」括起来",
            ), 0.5)
            q2 = (retry or {}).get("question", "").strip()
            q = q2 if q2 and quotes_answer(q2, text) else _fallback_followup(text, key)

        self.questions.append(q)
        self.intents.append(key)
        self.idx = len(self.answers)
        self.touch()
        return True

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

    def _new_angle_question(self) -> str:
        """主张聊透了之后，换一个新角度继续问（不让对话结束）。"""
        asked = "；".join(self.questions[-6:])
        data, _ = _ask(MAIN_PROMPT.format(
            name=self.entry.get("name", ""),
            context=(self.entry.get("context") or self.entry.get("evidence", "")),
        ), 0.7)
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
        hist = [{"q": self.questions[i], "a": self.answers[i]}
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

    _FIELDS = ("entry", "resume", "jd", "questions", "claims", "claim_status", "gaps", "evals",
               "intents", "answers", "idx", "finished",
               "report", "error", "session_id", "created_at", "updated_at")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> "MiniInterview":
        m = cls(entry=d.get("entry") or {}, resume=d.get("resume", ""), jd=d.get("jd", ""))
        for k in cls._FIELDS:
            if k in d:
                setattr(m, k, d[k])
        # 旧版本存档可能缺字段/类型不对：这里统一兜底，坏档不能让首页起不来
        for k in ("questions", "claims", "claim_status", "gaps", "evals", "intents", "answers"):
            if not isinstance(getattr(m, k, None), list):
                setattr(m, k, [])
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


def save(m: MiniInterview) -> str:
    os.makedirs(_dir(), exist_ok=True)
    path = os.path.join(_dir(), f"{m.session_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m.to_dict(), f, ensure_ascii=False, indent=2)
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
        if name.endswith(".json"):
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
