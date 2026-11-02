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

from . import rubric
from .llm import LLMError, chat

ROUNDS = 3

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
        self.intents: list[str] = []               # 每题的问题类型（决定用哪套固定清单）
        self.answers: list[str] = []
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
        data, _ = _ask(MAIN_PROMPT.format(name=name, context=self.entry.get("context") or self.entry.get("evidence", "")), 0.6)
        q = (data or {}).get("question", "").strip()
        if not q:
            self.error = "题目生成失败，请重试"
            return False
        self.questions = [q]
        angle = str((data or {}).get("angle", "")).strip().lower()
        self.intents = [ANGLE_MAP.get(angle, INTENTS[0][0])]
        self.answers = []
        self.idx = 0
        return True

    def answer(self, text: str) -> bool:
        """记录一轮回答；未满 3 轮就生成下一刀追问，满 3 轮就整链打分。"""
        text = (text or "").strip()
        if not text or self.finished:
            return False
        self.answers.append(text)

        if len(self.answers) >= ROUNDS:
            self.finish()
            return True

        key, intent_name, intent_desc = INTENTS[len(self.answers)]
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

        # 追问必须接上候选人自己的话；没接上就重写一次，再不行用原话兜底
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
        self.intents.append(key)                 # 追问用本轮固定意图
        self.idx = len(self.answers)
        self.touch()
        return True

    def finish(self):
        self.finished = True
        self.idx = ROUNDS - 1
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

    _FIELDS = ("entry", "resume", "jd", "questions", "intents", "answers", "idx", "finished",
               "report", "error", "session_id", "created_at", "updated_at")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> "MiniInterview":
        m = cls(entry=d.get("entry") or {}, resume=d.get("resume", ""), jd=d.get("jd", ""))
        for k in cls._FIELDS:
            if k in d:
                setattr(m, k, d[k])
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
    except (json.JSONDecodeError, OSError, TypeError):
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
