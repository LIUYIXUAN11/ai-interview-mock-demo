"""面试引擎：硬状态机 + 轮次预算 + 上下文组装 + 越权拦截。

这是整个产品最核心的一块，也是「Demo → 产品」的分水岭：
v1 靠 prompt 里写「必须按顺序走完 4 个环节」，模型实际上随时会跳环节、无限追问、忘记收尾；
v2 把流程控制权收回代码：**代码决定现在该问哪个环节、还能问几次，模型只负责把这一问说好**。

v2.7 增加「越权拦截」：
模型可以在回复末尾加 [[END]] 表达「我想收尾了」，但**这个标记不授权任何事**——
它只是一个越权信号。代码看到它（或看到收尾话术）就作废这条回复，带上
「已问清单 + 剩余角度 + 禁止重复」重写一次；再越权就换成本地兜底问句。
关键点：重写换的是**输入**，不是「再抽一次奖」。
"""

import time

from . import prompts
from .settings import temp as _t
from .llm import LLMError, chat, parse_reply


class InterviewEngine:
    def __init__(self, resume: str = "", jd: str = "", pace: str = "normal"):
        self.resume = (resume or "").strip()
        self.jd = (jd or "").strip()
        self.pace = pace if pace in prompts.STAGE_BUDGET else "normal"
        self.budget = prompts.STAGE_BUDGET[self.pace]

        self.stage = prompts.STAGE_ORDER[0]          # 不写死环节名：环节改了这里也不用动
        self.display_stage = self.stage              # 最后一个「已问出」的问题所属环节
        self.asked_in_stage = 0                      # 显示用：该环节已问到第几个
        self.asked_total = 0                         # 累计提问次数
        self.rounds_in_stage = 0
        self.user_turns = 0
        self.messages = []          # 只存 user / assistant，system 每次动态拼
        self.question_log = []      # [{"stage": ..., "text": ...}] 用于「禁止重复」
        self.override_stats = {"rejected": 0, "rewritten": 0, "fallback": 0}
        self.usage = {"input": 0, "output": 0, "calls": 0, "latency_ms": 0}
        self.started_at = time.time()
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at))
        self.session_id = f"{time.strftime('%Y%m%d%H%M%S')}-{abs(hash(self.created_at)) % 1000:03d}"
        self.finished = False
        self.error = ""

    # ------------------------------------------------------------ 状态

    @property
    def turns_left(self) -> int:
        return max(self.budget[self.stage] - self.rounds_in_stage, 0)

    @property
    def stage_label(self) -> str:
        return prompts.STAGE_NAMES.get(self.display_stage, self.display_stage)

    @property
    def stage_index(self) -> int:
        return prompts.STAGE_ORDER.index(self.display_stage)

    @property
    def total_stages(self) -> int:
        return len(prompts.STAGE_ORDER)

    def progress_text(self) -> str:
        # 显示「已经问出的问题」属于哪个环节，而不是「下一个该问的环节」
        budget = self.budget[self.display_stage]
        # 夹住计数：旧存档（环节预算改小过）会出现 "2/1 问" 这种越界显示
        asked = max(1, min(self.asked_in_stage, budget))
        return f"第 {self.stage_index + 1}/{self.total_stages} 环节 · {self.stage_label}（本环节第 {asked}/{budget} 问）"

    # ------------------------------------------------------------ Prompt 组装

    def _system_prompt(self, extra: str = "") -> str:
        parts = [prompts.PERSONA, prompts.HARD_RULES]

        remaining = self.turns_left
        stage_cmd = prompts.STAGE_INSTRUCTIONS[self.stage]
        if remaining > 0:
            stage_cmd += f"\n\n【本环节剩余提问次数】{remaining} 次。问完这一问就进入下一环节，不要提前跳。"
        else:
            stage_cmd += "\n\n【本环节次数已用完】这一轮不再追问，直接自然过渡到下一个环节。"
        if self.stage == "project":
            stage_cmd += (
                f"\n\n【项目深挖安排】本环节共 {self.budget['project']} 问：前 3 问挖第 1 个项目，"
                f"第 4 问起换第 2 个项目。当前已问 {self.rounds_in_stage} 次。"
            )
        parts.append(stage_cmd)

        if self.user_turns == 0:
            parts.append("【注意】这是面试的第一句话，请开场并请候选人做自我介绍。")

        if self.resume:
            parts.append(f"【候选人简历】\n{self.resume[:6000]}")
        if self.jd:
            parts.append(f"【目标岗位 JD】\n{self.jd[:2000]}")

        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    def _raw_call(self, temperature: float, extra_system: str = "") -> tuple[str, str]:
        """只负责「调一次 + 记账 + 解析」，不改任何展示状态（重写时不会重复计数）。"""
        t0 = time.time()
        text, usage = chat(
            [{"role": "system", "content": self._system_prompt(extra_system)}] + self.messages,
            temperature=_t("interview", temperature),
        )
        self.usage["input"] += usage["input"]
        self.usage["output"] += usage["output"]
        self.usage["calls"] += 1
        self.usage["latency_ms"] += int((time.time() - t0) * 1000)
        return parse_reply(text)

    # ------------------------------------------------------------ 越权拦截

    def _is_violation(self, text: str, intent: str) -> bool:
        """判断这条回复是否越权（试图结束面试 / 说了收尾话）。"""
        if self.stage == "closing":
            return False          # 收尾环节本来就说这些，不算越权
        if intent == "finish":
            return True           # 想收尾 = 越权（标记永远不授权结束）
        if not (text or "").strip():
            return True           # 空回复当成异常，走重写/兜底
        return any(p in text for p in prompts.CLOSING_PHRASES)   # 漏标也能抓

    def _asked_texts(self) -> list[str]:
        return [q["text"] for q in self.question_log if q.get("stage") == self.stage]

    def _remaining_angles(self) -> str:
        angles = prompts.STAGE_ANGLES.get(self.stage, [])
        rest = angles[self.rounds_in_stage:] or angles[-1:]
        return "、".join(rest) if rest else "换一个角度"

    def _rewrite_prompt(self, base_extra: str) -> str:
        asked = self._asked_texts()
        asked_block = "\n".join(f"- {t}" for t in asked[-6:]) or "（本环节还没问过问题）"
        hint = prompts.REWRITE_INSTRUCTION.format(
            remaining=max(self.turns_left, 1),
            dims=self._remaining_angles(),
            asked=asked_block,
        )
        return f"{base_extra}\n\n{hint}" if base_extra else hint

    def _fallback_question(self) -> str:
        pool = prompts.FALLBACK_QUESTIONS.get(self.stage) or ["我们换一个话题：你怎么看这个方向？"]
        asked = set(self._asked_texts())
        for q in pool:
            if q not in asked:
                return q
        return pool[self.rounds_in_stage % len(pool)]

    def _ask_one(self, base_extra: str = "", count: bool = True) -> str:
        """生成一个「合法」的问题：正常 → 重写一次 → 本地兜底。

        count=False 用于「重新生成」：问题被替换掉，总提问数不该 +1。
        """
        self.display_stage = self.stage
        self.asked_in_stage = self.rounds_in_stage + 1
        if count:
            self.asked_total += 1

        try:
            text, intent = self._raw_call(0.7, base_extra)
        except LLMError as e:
            self.error = str(e)
            return prompts.FALLBACK_REPLY

        if not self._is_violation(text, intent):
            self.error = ""
            self.question_log.append({"stage": self.stage, "text": text})
            return text

        # ---- 越权：作废，带「已问清单 + 剩余角度」重写一次 ----
        self.override_stats["rejected"] += 1
        try:
            text2, intent2 = self._raw_call(0.3, self._rewrite_prompt(base_extra))
        except LLMError as e:
            self.error = str(e)
            fb = self._fallback_question()
            self.override_stats["fallback"] += 1
            self.question_log.append({"stage": self.stage, "text": fb})
            return fb

        if self._is_violation(text2, intent2):
            # ---- 连续越权：不再调模型，直接用本地问句（0 成本、0 延迟、绝不越权）----
            self.override_stats["fallback"] += 1
            fb = self._fallback_question()
            self.question_log.append({"stage": self.stage, "text": fb})
            return fb

        self.override_stats["rewritten"] += 1
        self.error = ""
        self.question_log.append({"stage": self.stage, "text": text2})
        return text2

    # ------------------------------------------------------------ 推进流程

    def _advance(self):
        """当前环节问完，按预算推进到下一环节。"""
        self.rounds_in_stage += 1
        if self.rounds_in_stage >= self.budget[self.stage]:
            idx = prompts.STAGE_ORDER.index(self.stage)
            if idx + 1 < len(prompts.STAGE_ORDER):
                self.stage = prompts.STAGE_ORDER[idx + 1]
                self.rounds_in_stage = 0
            else:
                self.finished = True

    def open_interview(self) -> str:
        """开场白。不需要用户输入。"""
        reply = self._ask_one()
        self.messages.append({"role": "assistant", "content": reply})
        self._advance()
        return reply

    def answer(self, user_text: str) -> str:
        """候选人回答一轮，返回面试官下一句。"""
        self.messages.append({"role": "user", "content": user_text})
        self.user_turns += 1
        reply = self._ask_one()
        self.messages.append({"role": "assistant", "content": reply})
        self._advance()
        return reply

    def regenerate_question(self) -> str:
        """候选人还没回答时，对最后一个问题不满意：撤掉它，换一个不同的问法。

        不占轮次（预算不变）、不增加总提问数（旧问题被替换），
        但旧问题会进 question_log，保证新问题不重复它。
        """
        if self.finished or not self.messages or self.messages[-1]["role"] != "assistant":
            return ""

        old = self.messages.pop()["content"]
        # 旧问题本来就在已问清单里，这里只打一个"被替换过"的标，不要重复记一条
        if self.question_log and self.question_log[-1].get("text") == old:
            self.question_log[-1]["replaced"] = True
        else:
            self.question_log.append({"stage": self.display_stage, "text": old, "replaced": True})

        extra = prompts.REGENERATE_INSTRUCTION.format(
            previous=old[:200],
            dims=self._remaining_angles(),
        )
        reply = self._ask_one(base_extra=extra, count=False)
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def skip_stage(self) -> str:
        """用户主动跳过当前环节：进下一环节，并让面试官立刻问新环节的第一问。

        跳环节是「用户行使的权力」，模型只能在本环节内换角度——
        这样标签和内容不会各说各话。
        """
        if self.finished:
            return ""
        idx = prompts.STAGE_ORDER.index(self.stage)
        if idx + 1 >= len(prompts.STAGE_ORDER):
            self.finished = True
            return ""

        self.stage = prompts.STAGE_ORDER[idx + 1]
        self.rounds_in_stage = 0
        self.messages.append({"role": "user", "content": prompts.SKIP_MARKER})
        reply = self._ask_one(prompts.SKIP_INSTRUCTION)
        self.messages.append({"role": "assistant", "content": reply})
        self._advance()
        return reply

    def force_finish(self):
        """用户手动结束：直接进收尾环节。"""
        self.stage = "closing"
        self.rounds_in_stage = 0

    # ------------------------------------------------------------ 序列化（用于中断恢复）

    _FIELDS = (
        "resume", "jd", "pace", "stage", "display_stage", "asked_in_stage",
        "asked_total", "rounds_in_stage", "user_turns", "messages", "usage",
        "started_at", "created_at", "session_id", "finished", "error",
        "question_log", "override_stats",
    )

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> "InterviewEngine":
        e = cls(resume=d.get("resume", ""), jd=d.get("jd", ""), pace=d.get("pace", "normal"))
        for k in cls._FIELDS:
            if k in d and k not in ("resume", "jd", "pace"):
                setattr(e, k, d[k])

        # 兼容旧存档：环节改过名/合并过（intro、self_intro → icebreak），否则恢复会 KeyError
        legacy = {"intro": "icebreak", "self_intro": "icebreak"}
        if e.stage not in prompts.STAGE_ORDER:
            e.stage = legacy.get(e.stage, prompts.STAGE_ORDER[0])
        if e.display_stage not in prompts.STAGE_ORDER:
            e.display_stage = e.stage   # 展示环节跟内部对齐，避免标签和剩余次数打架
        if e.rounds_in_stage >= e.budget[e.stage]:
            e.rounds_in_stage = 0
        return e

    # ------------------------------------------------------------ 指标

    def metrics(self) -> dict:
        return {
            "stage": self.stage,
            "user_turns": self.user_turns,
            "duration_s": int(time.time() - self.started_at),
            "usage": dict(self.usage),
            "finished": self.finished,
            "override": dict(self.override_stats),
        }
