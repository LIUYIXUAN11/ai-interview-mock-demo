"""练习会话：一个环节的专项训练。

结构固定为「3 题训练 + 1 题复测」：
- 训练题：每题可以阻塞体检、最多改 2 版；
- 复测题：回到第 1 题原题，不给骨架、不许改版。

v3.1 修了两个真问题：
1. **出题会编**：模型会把"方案取舍"这类面试套路题套到简历上，编出候选人没做过的选择
   （真实案例：简历写了 RAG，题目里冒出"RAG + Workflow"）。
   修法：题目必须附「依据」原句，代码回查简历；再校验题干里的技术词是否都出自简历。
2. **答过就没了**：回答和体检卡只活在内存里。修法：每题落盘，可按题号回看。
"""

import hashlib
import json
import os
import re
import time

from . import coach as coach_mod
from . import scope
from .llm import LLMError, chat

ROOT = os.environ.get("INTERVIEW_DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCHEMA = 2           # 题目结构版本：1 = 旧的三题制，2 = 按经历 + 四角度
TRAIN_N = 4          # 一个项目固定 4 题，各问一个角度
MAX_REWRITES = 2   # 仅用于历史存档兼容，训练题已不限次数

ANGLES = [
    ("need", "真需求", "这项经历到底解决谁的什么问题、怎么确认是真需求"),
    ("tradeoff", "方案取舍", "当时怎么做决定的、为什么不用别的做法（没比较过就问怎么判断该不该这么做）"),
    ("result", "结果数据", "结果怎么样、数据怎么来的、口径是什么"),
    ("role", "你的角色", "哪部分是你亲手做的、和团队怎么分工"),
]

ANGLE_LABEL = {k: label for k, label, _ in ANGLES}
ANGLE_TO_INTENT = {"need": "why_do", "tradeoff": "why_this", "result": "how_verify", "role": "default"}
CATEGORIES = ["实习", "项目", "比赛", "科研"]

PROJECT_PROMPT = """你是一个简历信息抽取器。你的任务是从用户提供的简历文本中，只提取"实习经历"部分出现过的公司名称，并按指定 JSON 格式输出。

【任务范围】
1. 只提取明确属于"实习经历 / 实习经验 / 实践经历（实习）/ 工作经历中标注为实习"的板块中的公司。
2. 不要提取以下内容中的机构名：教育背景、项目经历、校园经历、获奖荣誉、证书、技能、自我评价、竞赛主办方、项目合作方、客户、学校、培训机构等。
3. 如果同一公司在实习经历中多次出现，只输出一次，source_span 使用第一次出现的原文片段。
4. 如果简历中没有实习经历，或无法识别出公司，返回 {{"companies": []}}。

【抽取规则】
1. 公司名称保留原文，不要翻译、不要改写、不要擅自补全为工商全称。
2. 每个公司必须给出 source_span。
3. source_span 必须从输入原文中逐字复制，不能改写、不能概括、不能拼接不存在的文字。
4. 如果找不到原文依据，不要输出该公司。
5. 不要臆造。不确定时宁可不输出。
6. 不要输出公司之外的实体，例如职位、学校、项目名、客户名、竞赛名。
7. 只输出 JSON，不要输出 Markdown、解释、前后缀文字、注释或代码块标记。

【输出格式】
输出必须严格符合以下结构：

{{
  "companies": [
    {{
      "name": "公司名称",
      "source_span": "原文中对应的逐字片段"
    }}
  ]
}}

字段要求：
- companies：数组，可以为空。
- name：字符串，公司名称，保留原文。
- source_span：字符串，必须逐字来自输入原文，用于校验。

【示例】
示例输入：
教育背景：某某大学 计算机专业
实习经历：
2023.06-2023.09 字节跳动 后端开发实习生
2022.07-2022.09 腾讯 前端开发实习生
项目经历：与阿里巴巴合作完成某系统
获奖荣誉：某某竞赛由美团赞助

示例输出：
{{
  "companies": [
    {{"name": "字节跳动", "source_span": "2023.06-2023.09 字节跳动 后端开发实习生"}},
    {{"name": "腾讯", "source_span": "2022.07-2022.09 腾讯 前端开发实习生"}}
  ]
}}

注意：示例中的"阿里巴巴"出现在项目经历中，"美团"出现在获奖荣誉中，都不应被提取。

【边界情况】
- 如果实习经历板块标题存在但内容为空，返回 {{"companies": []}}。
- 如果公司名称与职位连写，name 只保留公司名称，source_span 保留包含公司名称的原文片段。
- 如果文本中存在 OCR 噪声，但仍能明确判断公司名称，可以输出；source_span 仍必须逐字复制原文片段。
- 如果无法确定某机构是否属于实习经历中的公司，不要输出。

【简历】
{resume}"""


QUESTION_PROMPT = """你在为「项目深挖」环节出练习题。候选人选了简历里的这一条经历：

【经历】{name}（{type}）
【简历原文】{context}

出 4 道题，每题只问一个角度：
1. 真需求：这项经历到底解决谁的什么问题、怎么确认是真需求；
2. 方案取舍：当时怎么做决定的、为什么不用别的做法（如果没比较过，就问怎么判断该不该这么做，别预设）；
3. 结果数据：结果怎么样、数据怎么来的、口径是什么；
4. 你的角色：哪部分是他亲手做的、和团队怎么分工。

【硬规则｜违反即作废】
- **每道题的题干里必须出现「{name}」**，让人单独看题也知道在问哪段经历；
- 只能用简历里明确出现过的词汇和数字，简历没写的词一个都不许出现；
- 不许预设候选人做过"方案对比""选型""放弃过某个方案"，除非简历写了；
- 每题给一句简历原句作依据（逐字，12–60 字），系统会回查；
- 口语化、一次只问一件事、不超过 50 字。

只输出 JSON：{{"questions": [{{"angle": "need", "text": "问题", "basis": "简历原句"}}]}}"""


def _norm(s: str) -> str:
    s = re.sub(r"\s+", "", s or "")
    return "".join(ch for ch in s if ch not in "，。、；：？！“”‘’（）《》【】,.!?;:\"'()[]{}<>—-…·%").lower()


def _extract_json(raw: str):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _tech_tokens(text: str) -> set[str]:
    """题干里的英文技术词（≥2 个字母），用来查有没有编出新名词。"""
    return {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9\-\+\.]{1,}", text or "")}


def unknown_tokens(question: str, resume: str) -> set[str]:
    """题干里出现、但简历里没有的英文词。空集合 = 通过。"""
    return {t for t in _tech_tokens(question) if t not in _norm(resume)}


# 宽松模式：把「工作经历」也算进来（全职同样值得练）。只在严格模式抽不到时才用。
_STRICT_SCOPE = '1. 只提取明确属于"实习经历 / 实习经验 / 实践经历（实习）/ 工作经历中标注为实习"的板块中的公司。'
_LOOSE_SCOPE = '1. 提取明确属于"实习经历 / 实习经验 / 工作经历 / 实践经历"的板块中的公司（实习和全职都算）。'
LOOSE_PROMPT = PROJECT_PROMPT.replace(_STRICT_SCOPE, _LOOSE_SCOPE)
assert LOOSE_PROMPT != PROJECT_PROMPT, "宽松提示词没替换成功"




PROJECT_ITEM_PROMPT = """你是一个简历信息抽取器。你的任务是从用户提供的简历文本中，只提取"项目经历 / 科研项目 / 竞赛项目"部分出现过的**项目名称**，并按指定 JSON 格式输出。

【任务范围】
1. 只提取明确属于"项目经历 / 科研经历 / 竞赛经历 / 实践项目"板块中的项目。
2. 不要提取以下内容：教育背景、实习或工作经历里的子项目（那是公司经历的一部分）、获奖荣誉、证书、技能、自我评价、课程作业。
3. 同一个项目多次出现只输出一次，source_span 用第一次出现的原文片段。
4. 如果简历中没有项目经历，或识别不出项目，返回 {{"projects": []}}。

【抽取规则】
1. 项目名称保留原文，不要翻译、不要改写、不要擅自补全。
2. 每个项目必须给出 source_span，且必须从输入原文中**逐字复制**，不能改写、概括或拼接。
3. 找不到原文依据就不要输出；不确定时宁可不输出。
4. 只输出 JSON，不要输出 Markdown、解释或代码块标记。

【输出格式】
{{"projects": [{{"name": "项目名称", "source_span": "原文中对应的逐字片段"}}]}}

【示例】
示例输入：
实习经历：2023.06-2023.09 字节跳动 后端开发实习生
项目经历：
校园二手交易平台 负责人 2023.10-2024.03
"挑战杯"课外学术项目 核心成员 2022.09-2023.05
获奖荣誉：全国大学生数学建模竞赛二等奖

示例输出：
{{"projects": [
  {{"name": "校园二手交易平台", "source_span": "校园二手交易平台 负责人 2023.10-2024.03"}},
  {{"name": ""挑战杯"课外学术项目", "source_span": ""挑战杯"课外学术项目 核心成员 2022.09-2023.05"}}
]}}

注意：字节跳动属于实习经历，"数学建模竞赛"属于获奖荣誉，都不应被提取。

【简历】
{resume}"""


def _parse_items(data, key: str, typ: str) -> list[dict]:
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for it in items[:10]:
        if isinstance(it, dict):
            name = str(it.get("name", "")).strip()
            span = str(it.get("source_span", "")).strip()
        else:
            continue
        if not name or not span:
            continue
        out.append({"type": typ, "name": name[:40], "evidence": span[:120], "verified": True})
    return out


def extract_project_items(resume: str) -> list[dict]:
    """从"项目经历"里抽项目名。"""
    resume = (resume or "").strip()
    if not resume:
        return []
    try:
        raw, _ = chat([{"role": "user", "content": PROJECT_ITEM_PROMPT.format(resume=resume[:4000])}],
                      temperature=0.2, json_mode=True)
    except LLMError:
        return []
    data = _extract_json(raw) or {}
    items, seen = [], set()
    for it in _parse_items(data, "projects", "项目"):
        if _norm(it["evidence"]) not in _norm(resume):
            continue
        if _norm(it["name"]) in seen:
            continue
        seen.add(_norm(it["name"]))
        items.append(it)
    return _attach_context(resume, items)


def _has(resume: str, word: str) -> bool:
    return _norm(word) in _norm(resume)


def extract_experiences_detailed(resume: str, loose: bool = False) -> dict:
    """分别抽公司、项目，各自判断成败，最后合并。

    关键：**每个抽取器各判各的**，不能用"合并后非空"当成"都成功"——
    否则"有工作经历但没抽到"会被"抽到了项目"掩盖，静默丢失。
    """
    resume = (resume or "").strip()
    notes: list[str] = []

    companies = extract_projects(resume, loose=loose)
    if not companies and not loose and _has(resume, "工作经历"):
        # 简历里明明写着"工作经历"，严格模式却抽不到 → 自动放宽，不必让用户点
        companies = extract_projects(resume, loose=True)
        if companies:
            notes.append("工作经历已按宽松模式纳入")

    projects = extract_project_items(resume)
    if _has(resume, "项目经历") and not projects:
        notes.append("简历里有「项目经历」但没抽到条目")

    return {"items": _merge_same_entry(companies + projects), "notes": notes}


def extract_experiences(resume: str, loose: bool = False) -> list[dict]:
    """兼容接口：只要条目。"""
    return extract_experiences_detailed(resume, loose=loose)["items"]


def extract_projects(resume: str, loose: bool = False) -> list[dict]:
    """从简历的"实习经历"里抽公司名。

    只抽公司（不抽项目/比赛/科研），因为公司是简历里最确定的结构单位：
    一行公司名 + 时间，几乎不会切错。项目层级以后再按公司往下钻。

    返回 [{type:"实习", name, evidence, verified}]；source_span 回查不到的丢弃。
    """
    resume = (resume or "").strip()
    if not resume:
        return []
    try:
        prompt = LOOSE_PROMPT if loose else PROJECT_PROMPT
        raw, _ = chat([{"role": "user", "content": prompt.format(resume=resume[:4000])}],
                      temperature=0.2, json_mode=True)
    except LLMError:
        return []

    data = _extract_json(raw) or {}
    items = data.get("companies")
    if not isinstance(items, list):
        return []

    out, seen = [], set()
    for item in items[:10]:
        if isinstance(item, str):
            name, span = item, item
        elif isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            span = str(item.get("source_span", "")).strip()
        else:
            continue
        if not name or not span:
            continue
        if _norm(span) not in _norm(resume):      # 依据不是原文 → 丢弃
            continue
        key = _norm(name)
        if key in seen:                            # 同一公司只留一次
            continue
        seen.add(key)
        out.append({"type": "实习", "name": name[:40], "evidence": span[:120], "verified": True})
    return _attach_context(resume, out)


SECTION_HEADS = ("项目经历", "教育背景", "校园经历", "获奖", "荣誉", "证书", "技能", "自我评价", "科研")


def _attach_context(resume: str, items: list[dict]) -> list[dict]:
    """给每个公司挂上"这家公司下面的原文段落"，给模型出题用（不参与校验）。"""
    if not items:
        return items
    lines = resume.split("\n")
    flat = [_norm(l) for l in lines]

    starts = {}
    for idx, it in enumerate(items):
        key = _norm(it["name"])
        for i, l in enumerate(flat):
            if key and key in l:
                starts[idx] = i
                break
    for idx, it in enumerate(items):
        if idx not in starts:                       # 名字对不上行，就退化成只用 evidence
            it["context"] = it["evidence"]
            continue
        i = starts[idx]
        later = sorted([v for k, v in starts.items() if v > i])
        end = later[0] if later else len(lines)
        for j in range(i + 1, end):                 # 撞到大标题就截断
            if any(h in lines[j] for h in SECTION_HEADS):
                end = j
                break
        block = "\n".join(lines[i:end]).strip()
        it["context"] = block[:800] or it["evidence"]
    return items


def _merge_same_entry(items: list[dict]) -> list[dict]:
    """同一段经历被拆成多条时合并：类型相同 + 名字前 4 个字相同 → 视为同一段。

    真实案例：某支付公司的实习被拆成「公司·岗位」和「具体需求落地」两条。
    """
    merged: list[dict] = []
    seen: dict[tuple, int] = {}
    for it in items:
        key = (it["type"], _norm(it["name"])[:4])
        if key in seen:
            keep = merged[seen[key]]
            if len(it["evidence"]) > len(keep["evidence"]):
                keep["evidence"] = it["evidence"]      # 名字保留第一条（通常是公司·岗位），依据取更完整的
            continue
        seen[key] = len(merged)
        merged.append(it)
    return merged


def _projects_dir() -> str:
    """和会话文件分开，公开模式下同样按访问者隔离。"""
    return scope.user_dir("projects")


def save_projects(sig: str, items: list[dict]) -> None:
    """解析结果也落盘：同一份简历不用每次重解析。"""
    os.makedirs(_projects_dir(), exist_ok=True)
    with open(os.path.join(_projects_dir(), f"projects_{sig}.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def load_projects(sig: str) -> list[dict] | None:
    path = os.path.join(_projects_dir(), f"projects_{sig}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def has_project_name(text: str, name: str) -> bool:
    """题干里有没有出现这条经历的名字（允许只写主干词）。"""
    t, n = _norm(text), _norm(name)
    if not n:
        return False
    if n in t:
        return True
    # 名字太长时，退一步：取名字里最长的两段做匹配
    parts = [x for x in re.split(r"[·\-_/|（）()]", name) if len(x) >= 2]
    return any(_norm(x) in t for x in parts)


def generate_questions(resume: str, entry: dict, n: int = TRAIN_N) -> list[dict]:
    """按选中的一条经历出 4 道题，每道题一个角度。"""
    resume = (resume or "").strip()
    name = (entry or {}).get("name", "")
    typ = (entry or {}).get("type", "项目")
    evidence = (entry or {}).get("evidence", "")

    user = QUESTION_PROMPT.format(name=name, type=typ,
                                  context=(entry or {}).get("context") or evidence)
    try:
        raw, _ = chat([{"role": "user", "content": user}], temperature=0.6, json_mode=True)
    except LLMError:
        return []

    data = _extract_json(raw) or {}
    by_angle = {}
    for item in (data.get("questions") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        basis = str(item.get("basis", "")).strip()
        angle = str(item.get("angle", "")).strip()
        if not text:
            continue
        if angle not in ANGLE_LABEL:
            continue
        by_angle.setdefault(angle, {
            "text": text,
            "angle": angle,
            "angle_name": ANGLE_LABEL[angle],
            "basis": basis,
            "basis_verified": bool(basis) and _norm(basis) in _norm(resume),
            "has_project": has_project_name(text, name),
            "unknown": sorted(unknown_tokens(text, resume)),
        })

    # 按固定顺序返回，缺哪个角度就用它补一句兜底题（保证是 4 题）
    out = []
    for key, label, hint in ANGLES:
        if key in by_angle:
            out.append(by_angle[key])
        else:
            out.append({"text": f"{name}这块，{hint}？", "angle": key, "angle_name": label,
                        "basis": evidence, "basis_verified": True,
                        "has_project": has_project_name(f"{name}这块", name), "unknown": []})
    return out[:n]


def source_signature(resume: str, jd: str) -> str:
    """简历 + JD 的指纹：用来判断"题目是不是按旧材料出的"。"""
    raw = f"{resume or ''}||{jd or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


class PracticeSession:
    """一个环节的一次练习。纯状态机，不碰界面。"""

    def __init__(self, stage: str = "project", resume: str = "", jd: str = "", entry: dict | None = None,
                 items: list[dict] | None = None):
        self.items = items or []          # 来自迷你面试的 3 个问题
        self.stage = stage
        self.resume = resume or ""
        self.jd = jd or ""
        self.entry = entry or {}
        self.questions: list[dict] = []
        self.idx = 0
        self.versions: dict[str, list] = {}
        self.rewrites = 0
        self.rev_counts: dict[str, int] = {}   # 每题改过几版（改版是覆盖，不能靠 len 推）
        self.baselines: dict[str, dict] = {}   # 原答的体检结果（"优化方向"），每题只算一次
        self.finished = False
        self.schema = SCHEMA
        self.source_sig = source_signature(self.resume, self.jd)
        self.session_id = f"{time.strftime('%Y%m%d%H%M%S')}"
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = self.created_at
        self.error = ""

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> bool:
        if self.items:                    # 题目由迷你面试给出，不再自己出
            self.questions = [{"text": it["text"], "kind": "train",
                               "intent": it.get("intent", ""), "your_answer": it.get("your_answer", ""),
                               "basis": "", "basis_verified": True, "unknown": []}
                              for it in self.items]
            self.versions = {str(i): [] for i in range(len(self.questions))}
            self.idx = 0
            self.finished = False
            self.touch()
            return True

        if not self.entry.get("name"):
            self.error = "没有选中要练的经历"
            return False
        qs = generate_questions(self.resume, self.entry, TRAIN_N)
        if len(qs) < TRAIN_N:
            self.error = "题目生成失败，请重试"
            return False
        self.questions = [{"text": q["text"], "kind": "train",
                           "intent": ANGLE_TO_INTENT.get(q.get("angle", ""), "default"),
                           "basis": q["basis"],
                           "basis_verified": q["basis_verified"], "unknown": q["unknown"],
                           "angle": q["angle"], "angle_name": q["angle_name"],
                           "has_project": q["has_project"]}
                          for q in qs[:TRAIN_N]]
        # 复测 = 第 1 题原题（连依据一起带过来）
        first = self.questions[0]
        self.questions.append({**first, "kind": "retest", "source_idx": 0})
        self.versions = {str(i): [] for i in range(len(self.questions))}
        self.idx = 0
        self.finished = False
        self.touch()
        return True

    def touch(self):
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------------------------------------------------------------- 状态

    @property
    def current(self) -> dict:
        return self.questions[self.idx] if self.questions and not self.finished else {}

    @property
    def is_retest(self) -> bool:
        return self.current.get("kind") == "retest"

    def rewrites_of(self, idx: int | None = None) -> int:
        """这道题改过几版（每题独立计数）。"""
        i = self.idx if idx is None else idx
        return int(self.rev_counts.get(str(i), 0))

    @property
    def can_revise(self) -> bool:
        """训练题不限改版次数；复测仍然不许改。"""
        return not self.is_retest

    @property
    def has_retest(self) -> bool:
        return bool(self.questions) and self.questions[-1].get("kind") == "retest"

    @property
    def retest_index(self) -> int:
        return len(self.questions) - 1 if self.has_retest else -1

    def ready_for_retest(self) -> bool:
        """没有复测题时永远为真；有的话要求前面每题都答过。"""
        if not self.has_retest:
            return True
        return all(len(self.versions.get(str(i), [])) > 0 for i in range(self.retest_index))

    def go_to(self, idx: int) -> bool:
        """跳到任意一题。复测未解锁时不允许跳过去。"""
        if not self.questions:
            return False
        idx = max(0, min(idx, len(self.questions) - 1))
        if self.has_retest and idx == self.retest_index and not self.ready_for_retest():
            return False
        self.idx = idx
        self.finished = False if idx < self.retest_index else self.finished
        return True

    def next_unanswered(self) -> bool:
        """下一道还没答的题（用于"下一题"按钮）。"""
        for i in range(self.retest_index):
            if not self.versions.get(str(i)):
                return self.go_to(i)
        if self.ready_for_retest() and not self.versions.get(str(self.retest_index)):
            return self.go_to(self.retest_index)
        return False

    def recheck(self):
        """重新体检：不改版、不计数，复测题也能重跑一次。"""
        versions = self.current_versions()
        if not versions:
            return None
        last = versions[-1]
        result = coach_mod.check(self.current.get("text", ""), last["answer"], self.stage,
                                 intent=self.current.get("intent", ""))
        if result is not None:
            last["coach"] = result
            self.touch()
        return result

    def versions_of(self, idx: int) -> list:
        return self.versions.get(str(idx), [])

    def current_versions(self) -> list:
        return self.versions_of(self.idx)

    # ---------------------------------------------------------------- 答题

    def baseline_answer(self, idx: int | None = None) -> str:
        """用户在迷你面试里对这道题的原答。"""
        i = self.idx if idx is None else idx
        q = self.questions[i] if i < len(self.questions) else {}
        return (q.get("your_answer") or "").strip()

    def ensure_baseline(self):
        """对原答跑一次体检，产出"优化方向"。每题只跑一次。"""
        key = str(self.idx)
        if key in self.baselines:
            return self.baselines[key]
        ans = self.baseline_answer()
        if not ans:
            self.baselines[key] = None
            return None
        result = coach_mod.check(self.questions[self.idx]["text"], ans, self.stage,
                                 intent=self.questions[self.idx].get("intent", ""))
        if result is not None:
            self.baselines[key] = result
            self.touch()
        return result

    def submit(self, answer: str, revise: bool = False):
        answer = (answer or "").strip()
        if not answer or self.finished:
            return None
        if revise and not self.can_revise:
            return None

        result = coach_mod.check(self.current.get("text", ""), answer, self.stage,
                                 intent=self.current.get("intent", ""))
        versions = self.versions.setdefault(str(self.idx), [])
        if revise and versions:
            versions.append({"answer": answer, "coach": result,
                            "at": time.strftime("%H:%M:%S"), "rev": True})
            self.rev_counts[str(self.idx)] = self.rewrites_of() + 1
        else:
            versions.append({"answer": answer, "coach": result,
                             "at": time.strftime("%H:%M:%S"), "rev": False})
        if self.is_retest:
            self.finished = True
        self.touch()
        return result

    def next_question(self) -> bool:
        if self.finished:
            return False
        if self.idx + 1 >= len(self.questions):
            self.finished = True
            self.idx = len(self.questions) - 1
            self.touch()
            return False
        self.idx += 1
        self.touch()
        return True

    # ---------------------------------------------------------------- 对比

    def comparison(self) -> dict:
        """训练首答 vs 复测：比要素总分（同一套清单，分母天然一致）。"""
        first = (self.versions_of(0) or [{}])[0]
        retest_idx = len(self.questions) - 1
        retest = (self.versions_of(retest_idx) or [{}])[0]

        def score_of(v):
            c = v.get("coach") or {}
            return c.get("score"), c.get("total"), len(c.get("elements") or [])

        f_s, f_t, f_n = score_of(first)
        r_s, r_t, r_n = score_of(retest)
        common_total = min(t for t in (f_t, r_t) if t is not None) if (f_t and r_t) else None

        return {
            "question": (self.questions[0] or {}).get("text", "") if self.questions else "",
            "first": {"answer": first.get("answer", ""), "coach": first.get("coach"),
                      "passed": f_s, "total": f_t, "scored": f_n},
            "retest": {"answer": retest.get("answer", ""), "coach": retest.get("coach"),
                       "passed": r_s, "total": r_t, "scored": r_n},
            "common_dims": [],
            "delta": round(r_s - f_s, 1) if (f_s is not None and r_s is not None) else None,
        }

    def root_causes(self) -> list[str]:
        counter: dict[str, int] = {}
        for vs in self.versions.values():
            for v in vs:
                tag = ((v.get("coach") or {}).get("root_cause") or {}).get("tag") or ""
                if tag:
                    counter[tag] = counter.get(tag, 0) + 1
        return [t for t, _ in sorted(counter.items(), key=lambda x: -x[1])]

    # ---------------------------------------------------------------- 序列化

    _FIELDS = ("stage", "resume", "jd", "entry", "schema", "questions", "idx", "versions", "rev_counts",
               "baselines", "rewrites",
               "finished", "session_id", "created_at", "updated_at", "error", "source_sig")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> "PracticeSession":
        s = cls(stage=d.get("stage", "project"), resume=d.get("resume", ""), jd=d.get("jd", ""),
                entry=d.get("entry") or {})
        for k in cls._FIELDS:
            if k in d and k not in ("stage", "resume", "jd"):
                setattr(s, k, d[k])
        if not d.get("source_sig"):
            s.source_sig = source_signature(s.resume, s.jd)
        return s


# ---------------------------------------------------------------- 落盘（回答和体检卡不能只活在内存里）

def _practice_dir() -> str:
    """公开模式下按访问者隔离，本地模式沿用 records/practice。"""
    return scope.user_dir("practice")


def save(s: PracticeSession) -> str:
    os.makedirs(_practice_dir(), exist_ok=True)
    path = os.path.join(_practice_dir(), f"{s.session_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(s.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def load(session_id: str):
    path = os.path.join(_practice_dir(), f"{session_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):      # 目录里混进别的 json 时不要炸
            return None
        return PracticeSession.from_dict(data)
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return None


def load_latest_unfinished():
    """刷新页面后接着练：取最近一场没做完的。"""
    if not os.path.isdir(_practice_dir()):
        return None
    files = sorted(os.listdir(_practice_dir()), reverse=True)
    for name in files:
        if not name.endswith(".json"):
            continue
        s = load(name[:-5])
        if s and not s.finished and getattr(s, "schema", 1) == SCHEMA:
            return s
    return None
