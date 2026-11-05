"""主张层：把简历里的一段经历，拆成"待验证的主张"。

为什么需要它：
现在的追问按"第几轮"排（第 2 轮问取舍、第 3 轮问验证），不管候选人说了什么。
真实面试官不是这样——他心里挂着一张"这人说了几件事、哪件还没被证明"的清单，
追问永远指向**最没被证明的那一条**。

主张 = 简历里一句可被验证的断言（"我把注册转化从 12.3% 提到 15.1%"）。
"""

import json
import re

from .exemplars import as_prompt
from .settings import temp as _t
from .llm import LLMError, chat

CLAIM_PROMPT = """你在准备一场产品经理面试。候选人选了简历里的这段经历：

【经历】{name}
【简历原文】
{context}

{exemplars}

【任务 1】把它拆成 3–5 条**待验证的主张**，并标清类型：
- result：有具体数字的结果（"转化率从 30% 提到 40%"）
- **design：设计/流程/机制上的改动（"把审核节点重新排了一下""把入口提前"）**
- judgment：一个判断（"我判断是入口太深"）
- action：一个动作（"我主导了 X 重构"）
- tradeoff：一次取舍（"放弃扩容，选了重构"）

**注意 design 和 result 的区别**：说"改了设计"不等于说"指标提升了"。
不要把设计改动写成 result，也不要给 design 类主张编造数字。
主张 = 一句可以被追问验证的断言，例如：
- 结果类："我把注册转化从 12.3% 提到 15.1%"（要验：口径、基线、对照、归因）
- 判断类："我判断是入口太深，不是用户不会用"（要验：证据、排他）
- 动作类："我主导了入口重构"（要验：到底哪些是你做的）
- 取舍类："放弃扩容方案，选了重构入口"（要验：备选、判据、代价）

【任务 2】给每条主张标一个**风险**（最可能被面试官戳破的地方，10 字内），
例如"高指标没基线""动词模糊（主导/参与）""有技术名词没决策依据""结果大但个人动作不清"。

【任务 3】从这些主张里挑**风险最高的一条**，直接向候选人提第一个问题（≤45 字，口语化）。
问题要指向那条主张**最没被证明的地方**，不许问简历上已经写明的事实。

【硬规则】
- 每条主张必须给 source：**简历原文里逐字存在的一句话**（8–40 字），系统会回查，查不到就丢弃；
- 只用简历里出现过的词和数字；
- 只输出 JSON。

{{
  "claims": [
    {{"text": "我把注册转化从 12.3% 提到 15.1%", "type": "result", "source": "新登录转化率从 12.3% 提升至 15.1%", "risk": "高指标没基线"}}
  ],
  "question": "你说转化从 12.3% 提到 15.1%，这个提升怎么确认是你改入口带来的？",
  "target": 0
}}"""


def _norm(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", s or "").lower()


def _risk_tags(claim: dict) -> list[str]:
    """代码补几条可判的风险标签。**按主张类型给，不能一刀切。**"""
    t = claim.get("text", "")
    ctype = claim.get("type", "")
    tags = []

    if ctype == "design":
        # 设计类：不追问指标，追问依据、约束、边界
        if not re.search(r"(因为|为了|考虑到|约束|限制)", t):
            tags.append("设计依据缺失")
        return tags

    if re.search(r"\d+(?:\.\d+)?\s*%", t) and not re.search(r"从.{0,8}[到至]", t):
        tags.append("有数字没基线")
    if re.search(r"(主导|负责|参与|协助|推动)", t):
        tags.append("动词边界模糊")
    if ctype == "result" and re.search(r"(提升|下降|增长|降低)", t) \
            and not re.search(r"(对照|实验|A/B|归因)", t):
        tags.append("结果未归因")
    return tags


def _nums(text: str) -> set[str]:
    return {n for n in re.findall(r"\d+(?:\.\d+)?%?", text or "") if len(n) >= 2}


def _dedupe(claims: list[dict]) -> list[dict]:
    """同一组数字被拆成多条主张时合并——否则会围着同一个指标反复追问。"""
    out = []
    for c in claims:
        nums = _nums(c["text"])
        dup = None
        for k in out:
            if nums and len(nums & _nums(k["text"])) >= 2:
                dup = k
                break
        if dup:
            if len(c["text"]) > len(dup["text"]):
                dup["text"] = c["text"]
            dup["tags"] = list(set(dup.get("tags", [])) | set(c.get("tags", [])))
            continue
        out.append(c)
    return out


def extract(name: str, context: str) -> dict:
    """返回 {claims, question, target}。失败返回空。"""
    context = (context or "").strip()
    if not context:
        return {}
    try:
        raw, _ = chat([{"role": "user", "content": CLAIM_PROMPT.format(
            name=name, context=context[:1500], exemplars=as_prompt())}],
                      temperature=_t('claims', 0.0), json_mode=True)
    except LLMError:
        return {}

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = text[text.find("{"): text.rfind("}") + 1]
        try:
            data = json.loads(m)
        except json.JSONDecodeError:
            return {}

    claims = []
    for c in (data.get("claims") or [])[:5]:
        if not isinstance(c, dict):
            continue
        t = str(c.get("text", "")).strip()
        src = str(c.get("source", "")).strip()
        if not t or not src:
            continue
        if _norm(src) not in _norm(context):      # 原文依据查不到 → 丢弃
            continue
        claim = {"text": t[:60], "type": str(c.get("type", "result"))[:10],
                 "source": src[:60], "risk": str(c.get("risk", ""))[:14]}
        claim["tags"] = _risk_tags(claim)
        claims.append(claim)

    claims = _dedupe(claims)
    if not claims:
        return {}

    target = data.get("target", 0)
    try:
        target = max(0, min(int(target), len(claims) - 1))
    except (TypeError, ValueError):
        target = 0

    return {"claims": claims, "question": str(data.get("question", ""))[:60], "target": target}
