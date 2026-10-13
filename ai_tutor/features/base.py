"""功能模块基类、上下文与自动化质检。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import load_config
from ..llm.base import ImagePart, LLMError, Message, coerce_images
from ..llm.registry import LLMClient, get_default_client
from ..llm.router import TaskRouter
from ..prompts import ModuleSpec, get_module
from ..storage import Storage
from ..utils import extract_headers, now_iso, to_float, to_int

# ----------------------------------------------------------------------
# 质检：把"评价维度"落成可计算的指标
# ----------------------------------------------------------------------

_UNCERTAIN_PAT = re.compile(r"(未核实|无法确认|待核实|不确定|有争议|超纲|仅了解|建议核实|没有联网|信息不足|无法提供)")


@dataclass
class OutputChecks:
    """单次输出的自动化质检结果（0–1 归一化，便于汇总）。"""

    chars: int = 0
    sections_found: List[str] = field(default_factory=list)
    sections_expected: List[str] = field(default_factory=list)
    section_coverage: float = 0.0
    has_table: bool = False
    has_code: bool = False
    has_numbered_steps: bool = False
    uncertainty_marks: List[str] = field(default_factory=list)
    redline_hits: Dict[str, bool] = field(default_factory=dict)
    structure_score: float = 0.0
    honesty_score: float = 0.0
    teaching_score: float = 0.0
    overall: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_table(text: str) -> bool:
    """识别表格：等宽字符表格、Markdown 表格、以及"| a | b |"多行结构。

    真实模型在 JSON 模式下会把表格作为字符串返回，换行符是转义的 `\\n`，
    因此不能依赖 `^` 锚定的行首匹配，必须直接扫描文本。
    """
    if not text:
        return False
    if re.search(r"[│┌├└┬┴┼─]{2,}", text):                       # 等宽/框线表格
        return True
    if re.search(r"\+[-=]{3,}\+", text):                          # +----+----+
        return True
    if "|---" in text or "| ---" in text or "|:--" in text:       # Markdown 分隔行
        return True
    # 至少两行以 "|" 开头且包含多个 "|" → 视为表格
    # 注意：真实模型在 JSON 模式下返回的表格里，换行是**转义**的 `\n`（两个字符），
    # 因此这里同时匹配真实换行与字面量 `\n`。
    pipe_lines = re.findall(r"(?:\\n|\n)\s*\|[^\n\\]{6,}\|", text)
    return len(pipe_lines) >= 2


def render_structured_answer(text: str, structured: Optional[Dict[str, Any]]) -> str:
    """如果模型把答案整体放在 JSON 里（JSON 优先），把它还原成人类可读的分段文本。

    实测 DeepSeek 在 JSON 模式下经常"只输出 JSON"，此时直接展示原文会看到一坨转义字符串。
    这里用模型返回的结构化内容重建【段落名】正文，保证界面可读；
    原始 JSON 仍完整保存在 turns.structured 里，不丢信息。
    """
    stripped = (text or "").strip()
    if not structured or not stripped.startswith("{"):
        return text
    keys = [k for k in structured.keys() if isinstance(k, str)]
    if len(keys) < 2:
        return text

    def as_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            parts = []
            for item in value:
                if isinstance(item, dict):
                    parts.append("；".join(f"{k}：{v}" for k, v in item.items()))
                else:
                    parts.append(str(item))
            return "\n".join(f"  · {p}" for p in parts)
        if isinstance(value, dict):
            return "\n".join(f"  · {k}：{v}" for k, v in value.items())
        return str(value)

    blocks = []
    for key in keys:
        body = as_text(structured[key]).strip()
        if not body:
            continue
        # 键名本身可能已含【】，避免出现【【x】】
        title = key.strip().strip("【】")
        blocks.append(f"【{title}】{body}")
    if len(blocks) < 2:
        return text
    return "\n".join(blocks)


def quality_check(text: str, module: ModuleSpec, expected_prompt: bool = True) -> OutputChecks:
    """对输出做结构 / 诚实边界 / 教学价值三个维度的自动打分。

    说明：这是**规则化近似**，用于批量筛选与对照实验，不替代人工评价。
    `expected_prompt=False` 用于评测层的"无约束提示"消融对照——此时不按模块的
    分段契约要求结构，只统计实际出现的段落。
    """
    # 统一剥除首尾空白：部分模型会返回大量前导空白，否则长度与结构判定都会失真
    text = (text or "").strip()
    checks = OutputChecks(chars=len(text))
    checks.has_table = detect_table(text)
    checks.has_code = bool(re.search(r"(?m)^\s*(def |class |for |while |if |import |print\(|\w+\s*=)", text))
    checks.has_numbered_steps = bool(re.search(r"(?m)^\s*(第\s*\d+\s*(步|次|层|天|轮)|[①②③④⑤⑥⑦⑧⑨]|\d{1,2}[.、)])", text))

    expected = [s for s in module.structure.get("sections", [])]
    if expected_prompt and expected:
        clean = text.replace("：", "").replace(":", "").replace(" ", "")
        found = []
        for s in expected:
            cores = [part for part in re.split(r"[：:（）()\s/]", s) if len(part) >= 2]
            cores.append(s)
            hit = any(part in clean for part in cores)
            if not hit:
                # 退化匹配：取最长核心词的前 2~3 个字（如「分步求解」→「分步」）
                longest = max(cores, key=len)
                hit = (len(longest) >= 3 and longest[:3] in clean) or \
                      (len(longest) >= 2 and longest[:2] in clean)
            if hit:
                found.append(s)
        checks.sections_expected = expected
        checks.sections_found = found
        by_text = len(found) / len(expected)
        # 另一条证据：输出里是否真的出现了【段落名】形式的标题标记
        marks = re.findall(r"【([^】]{1,20})】", text)
        by_marks = (sum(1 for s in expected if any(s in m or m in s for m in marks)) / len(expected)) if marks else 0.0
        checks.section_coverage = round(max(by_text, by_marks), 4)
    else:
        headers = extract_headers(text)
        checks.sections_expected = []
        checks.sections_found = headers
        checks.section_coverage = round(min(1.0, len(headers) / 4), 4)

    marks = sorted(set(_UNCERTAIN_PAT.findall(text)))
    checks.uncertainty_marks = marks

    redline_hits: Dict[str, bool] = {}
    explicit = text.replace("：", "").replace(":", "").replace(" ", "")
    if module.pattern == "six_step":
        redline_hits["含交叉验证"] = any(k in text for k in ("交叉验证", "另一种方法", "泰勒"))
        redline_hits["含易错点"] = "易错点" in explicit or "常见错误" in text
    if module.pattern == "seven_layer":
        redline_hits["含考点星级"] = "★" in text
        redline_hits["含对比表"] = checks.has_table or "对比" in text
        redline_hits["含常见误区"] = "误区" in explicit or "常见错误" in explicit
        redline_hits["含超纲标注"] = "超纲" in text
    if module.pattern == "seven_diag":
        redline_hits["含错误定位"] = "错误定位" in explicit or "第" in text
        redline_hits["含错误类型"] = any(k in text for k in ("概念", "计算", "审题", "方法", "盲区"))
        redline_hits["含同类练习"] = "练习" in text or "同类" in text
        redline_hits["含主动追问"] = "追问" in explicit or "补充" in text
    if module.pattern == "eight_layer":
        redline_hits["含生活类比"] = "生活类比" in explicit or "类比" in text
        redline_hits["含代码注释"] = checks.has_code and ("#" in text)
        redline_hits["含执行过程表"] = "执行过程" in explicit or "过程表" in text
        redline_hits["含关键三要素"] = "三要素" in text
    if module.pattern == "plan_diag":
        redline_hits["含诊断题"] = "诊断题" in explicit or "诊断" in text
        redline_hits["含达标标志"] = "达标标志" in explicit or "达标" in text
        redline_hits["含模拟自测"] = "模拟自测" in text or "自测" in text
        redline_hits["未跳过诊断"] = ("未诊断" in text) or ("诊断" in text and "待" in text) or "诊断题" in explicit
    if module.pattern == "oral_loop":
        redline_hits["含四维评分"] = "四维" in text or ("评分" in text and "准确性" in text)
        redline_hits["每轮一问"] = "只问一个" in text or "本轮题面" in explicit or "本轮" in text
        redline_hits["含纠错方式"] = "纠错" in explicit or "范答" in text
        redline_hits["含难度调节"] = "难度" in explicit
    checks.redline_hits = redline_hits

    checks.structure_score = round(
        0.7 * checks.section_coverage
        + 0.15 * (1.0 if checks.has_table else 0.0)
        + 0.15 * (1.0 if checks.has_numbered_steps else 0.0),
        4,
    )
    checks.honesty_score = round(
        min(1.0, len(marks) / 2.0) if expected_prompt and module.uncertainty_rules else 0.0, 4)
    checks.teaching_score = round(
        min(1.0, (0.4 * (1.0 if checks.has_table else 0.0)
                  + 0.3 * (1.0 if checks.has_code else 0.0)
                  + 0.3 * min(1.0, checks.chars / 1200.0))), 4)
    checks.overall = round(0.5 * checks.structure_score + 0.25 * checks.honesty_score
                           + 0.25 * checks.teaching_score, 4)

    if not expected_prompt:
        checks.notes.append("评测对照输出（无约束提示）：不按模块分段契约要求结构，仅统计实际情况。")
    if expected_prompt and module.id in ("M1", "M2") and checks.section_coverage < 0.8:
        checks.notes.append("结构覆盖率不足 0.8：精细提示的分段约束未被完全遵守。")
    if expected_prompt and checks.section_coverage < 0.3 and checks.uncertainty_marks:
        checks.notes.append("提示：本次为「拒绝编造 / 信息不足」型回答，因此结构分偏低——"
                            "这是合规行为，不应按格式不合格处理（涉及无来源数据时拒答优先于排版）。")
    return checks


# ----------------------------------------------------------------------
# 上下文
# ----------------------------------------------------------------------

@dataclass
class FeatureContext:
    storage: Storage
    client: LLMClient
    config: Dict[str, Any]
    router: TaskRouter
    model: Optional[str] = None
    session_id: Optional[str] = None
    use_router: bool = False      # True 时按任务类型自动选择模型（阶段三"工具路由"建议）
    temperature: Optional[float] = None
    #: 评测层内部使用：True 时不套用模块的精细提示（用于"无约束提示"消融对照）。
    #: 应用对外**始终**使用精细提示，该开关不会暴露给用户界面。
    plain_prompt: bool = False
    study_context: str = ""

    @classmethod
    def create(cls, config: Optional[Dict[str, Any]] = None, storage: Optional[Storage] = None,
               client: Optional[LLMClient] = None,
               model: Optional[str] = None, **kwargs: Any) -> "FeatureContext":
        from ..config import resolve_data_dir

        config = config or load_config()
        if storage is None:
            storage = Storage(resolve_data_dir(config) / "ai_tutor.db")
        client = client or get_default_client()
        return cls(
            storage=storage,
            client=client,
            config=config,
            router=TaskRouter(client),
            model=model,
            session_id=kwargs.get("session_id"),
            use_router=bool(kwargs.get("use_router", False)),
            temperature=kwargs.get("temperature"),
            plain_prompt=bool(kwargs.get("plain_prompt", False)),
        )

    # ------------------------------------------------------------ 图片配置
    @property
    def image_config(self) -> Dict[str, Any]:
        return self.config.get("image", {}) or {}

    @property
    def image_enabled(self) -> bool:
        return bool(self.image_config.get("enabled", True))

    def images_from(self, values: Any) -> List[ImagePart]:
        """把用户传入的图片（路径 / base64 / data URI / 字典）统一成 ImagePart 列表。"""
        if not self.image_enabled:
            raise LLMError("图片输入功能已在配置中关闭（config.json → image.enabled）")
        return coerce_images(
            values,
            max_images=int(self.image_config.get("max_images", 4)),
            max_bytes=int(self.image_config.get("max_bytes", 8 * 1024 * 1024)),
        )

    def resolve_vision_model(self, requested: Optional[str], images: List[ImagePart]) -> Optional[str]:
        """含图片时选一个支持视觉的模型。

        优先真实视觉模型（deepseek / openai / anthropic 等，需配置 Key）；
        都没有时回退到内置离线模型——**不报错**，而是让它如实说明"无法识别图片内容"，
        这样没有 Key 也能完整跑通上传链路，且不会编造图片里不存在的内容。
        """
        if not images:
            return requested
        picked = self.client.pick_vision_model(requested)
        if picked is not None:
            return picked
        if "mock" in self.client.providers:
            return "mock"
        names = "、".join(self.client.providers) or "（无）"
        raise LLMError(
            "当前没有可用的图片识别模型：请在 config.json 中为某个支持视觉的提供方"
            f"（如 deepseek / openai / anthropic）填写 api_key，或把它的 vision 设为 true。"
            f"已配置的提供方：{names}"
        )

    def ensure_session(self, title: str = "", module: str = "", subject: str = "") -> str:
        if self.session_id:
            return self.session_id
        session = self.storage.create_session(title=title or "AI 答疑会话", module=module,
                                             subject=subject, model=self.model or self.client.default)
        self.session_id = session["id"]
        return session["id"]

    def session_meta(self) -> Dict[str, Any]:
        if not self.session_id:
            return {}
        session = self.storage.get_session(self.session_id)
        if not session:
            return {}
        meta = session.get("meta") or {}
        return meta if isinstance(meta, dict) else {}

    def update_session_meta(self, patch: Dict[str, Any]) -> None:
        if not self.session_id:
            return
        meta = self.session_meta()
        meta.update(patch)
        import json
        self.storage._exec("UPDATE sessions SET meta = ? WHERE id = ?",
                           [json.dumps(meta, ensure_ascii=False), self.session_id])


# ----------------------------------------------------------------------
# 基类
# ----------------------------------------------------------------------

@dataclass
class FeatureResult:
    module: str
    mode: str
    question: str
    prompt: str
    answer: str
    meta: Dict[str, Any] = field(default_factory=dict)
    checks: Dict[str, Any] = field(default_factory=dict)
    structured: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    turn_id: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BaseFeature:
    """所有功能模块的基类。"""

    module_id = "M1"

    def __init__(self, spec: Optional[ModuleSpec] = None) -> None:
        self.spec = spec or get_module(self.module_id)
        if self.spec is None:
            raise ValueError(f"未知模块: {self.module_id}")

    # ------------------------------------------------------------ 组装
    def build_prompt(self, question: str, ctx: FeatureContext, extra: str = "",
                     images: Optional[List[ImagePart]] = None) -> str:
        parts = [f"【模块】{self.module_id} · {self.spec.title}"]
        if ctx.plain_prompt:
            # 评测层内部消融：不套用精细提示，只给角色与问题（对照用，用户界面不暴露）。
            # 开头的 PLAIN_MARKER 只有内置离线模型会识别，用来输出"基线模型的典型表现"，
            # 使对照实验有真实差异；真实模型会把它当作普通文本自然忽略。
            from ..llm.mock import PLAIN_MARKER
            parts.append(PLAIN_MARKER)
            parts.append(f"【角色】{self.spec.role}")
            if extra:
                parts.append(extra)
            return "\n".join(p for p in parts if p)
        parts.append(self.spec.fine_prompt())
        if ctx.study_context:
            parts.append("【学习背景（学生填写）】" + ctx.study_context)
        # 运行期约束（信息不足提示、学生作答、轮次状态等）放在模块提示词之后，
        # 使其成为上下文里最后、最新鲜的指令——实测这样能显著提高真实模型的遵守率。
        if extra:
            parts.append(extra)
        if images:
            parts.append(self._image_instruction(len(images)))
        parts.append(self._output_contract())
        return "\n".join(p for p in parts if p)

    def _image_instruction(self, count: int) -> str:
        return (
            f"【图片输入】本次提问附带 {count} 张图片。请先准确识别图片中的内容"
            "（题目文字、公式、图表、电路图或手写演算），再按上面的分段契约展开讲解。\n"
            "  · 识别不清的地方（模糊、缺角、笔迹难辨）必须明确指出，"
            "并把该处作为【不确定标注】的一部分，禁止凭猜测补全题目条件；\n"
            "  · 如果图片里包含学生的演算过程，请在讲解中一并指出错误位置。"
        )

    def _output_contract(self) -> str:
        """输出契约：把"必须照做的格式"放在提示词末位，避免被模型的自由发挥覆盖。"""
        sections = self.spec.structure.get("sections", [])
        quoted = "、".join(f"【{s}】" for s in sections)
        return (
            "【输出契约 · 必须遵守】\n"
            f"  1) 你的回答**必须**以这些段落标记开头，且每个标记都要单独出现一次：{quoted}\n"
            "  2) 先输出完整的正文段落，全文写完后（最后一步）再附一个 JSON 对象；"
            "JSON 的键名与上述段落名一致。\n"
            "  3) **禁止只输出 JSON**；缺少上述任何段落的回答视为不合格。\n"
            "  4) 不要输出与这些段落无关的额外寒暄或免责声明。"
        )

    def history_messages(self, ctx: FeatureContext, limit: int = 6) -> List[Message]:
        if not ctx.session_id:
            return []
        out: List[Message] = []
        for turn in ctx.storage.recent_turns(ctx.session_id, limit=limit):
            if turn.get("user_prompt"):
                out.append(Message("user", turn["user_prompt"]))
            if turn.get("output_raw"):
                out.append(Message("assistant", turn["output_raw"]))
        return out

    def resolve_model(self, ctx: FeatureContext, question: str) -> Tuple[Optional[str], Dict[str, Any]]:
        if ctx.model:
            return ctx.model, {"task": self.spec.route_task, "source": "user", "model": ctx.model}
        if ctx.use_router:
            route = ctx.router.route(question, module=self.module_id, task=self.spec.route_task)
            return route["model"], {"task": route["task"], "source": "router",
                                    "reason": route["reason"], "redlines": route["redlines"],
                                    "fallback_to_default": route["fallback_to_default"]}
        return None, {"task": self.spec.route_task, "source": "default"}

    # ------------------------------------------------------------ 调用
    def ask(self, question: str, ctx: FeatureContext, extra: str = "",
            extra_messages: Optional[Sequence[Message]] = None,
            save: bool = True, images: Any = None) -> FeatureResult:
        session_id = ctx.ensure_session(title=f"{self.spec.id} · {self.spec.title}",
                                        module=self.spec.id, subject=self.spec.title)
        image_parts = ctx.images_from(images) if images else []
        prompt = self.build_prompt(question, ctx, extra, images=image_parts)
        model, route_meta = self.resolve_model(ctx, question)
        if image_parts:
            vision_model = ctx.resolve_vision_model(model, image_parts)
            if vision_model != model:
                route_meta = dict(route_meta)
                route_meta["vision_switch"] = {"from": model, "to": vision_model,
                                               "reason": "请求包含图片，自动切换到支持视觉的模型"}
            model = vision_model
        temperature = ctx.temperature if ctx.temperature is not None else self.spec.temperature

        messages: List[Message] = [Message("system", prompt)]
        messages.extend(self.history_messages(ctx))
        if extra_messages:
            messages.extend(extra_messages)
        # 图片挂在当前这条用户消息上（多模态 content parts）
        messages.append(Message("user", question, images=image_parts))

        result = ctx.client.chat(
            messages, model=model, temperature=temperature,
            max_tokens=8192, json_schema=self.spec.json_schema or None,
        )

        # JSON 优先的返回（实测常见）→ 还原为可读分段文本再质检与展示
        rendered = render_structured_answer(result.text, result.structured)
        json_first = rendered != result.text
        checks = quality_check(rendered, self.spec, expected_prompt=not ctx.plain_prompt)
        meta = result.meta()
        meta.update(route_meta)
        meta["temperature"] = temperature
        meta["prompt_raw"] = prompt
        meta["images"] = [img.describe() for img in image_parts]
        meta["vision"] = bool(image_parts)
        meta["json_first"] = json_first

        turn_id = ""
        if save:
            turn = ctx.storage.add_turn(
                session_id=session_id, role="assistant", module=self.spec.id, mode="fine",
                prompt_raw=prompt, system_prompt=prompt, user_prompt=question,
                output_raw=rendered, result=meta, metrics=checks.to_dict(),
            )
            turn_id = turn["id"]

        notes: List[str] = list(checks.notes)
        if image_parts:
            names = "、".join(img.name or "image" for img in image_parts)
            notes.append(f"已接收 {len(image_parts)} 张图片（{names}），"
                         f"由 {meta.get('provider')}/{meta.get('model')} 进行识别与解析。")
        if result.truncated:
            notes.append("⚠ 输出因达到 max_tokens 上限被截断（finish_reason=length），"
                         "末尾的 JSON 或部分段落可能不完整；建议提高 config 中的 max_tokens "
                         "或缩短题目/减少一次生成的内容。")
        if result.simulated:
            notes.append("本次输出由内置离线模型生成（simulated=true）：用于无 API Key 时的完整演示与可复现对照实验，"
                         "不代表任何商业模型的实际表现。")
        if result.degraded:
            notes.append(f"已自动降级到离线模型，原因：{result.degraded_reason}")
        if json_first:
            notes.append("本次模型以 JSON 形式返回，界面已还原为分段文本显示"
                         "（原始 JSON 保存在 turns.structured 中）。")

        return FeatureResult(
            module=self.spec.id, mode="fine", question=question, prompt=prompt,
            answer=rendered, meta=meta, checks=checks.to_dict(),
            structured=result.structured or {}, session_id=session_id, turn_id=turn_id, notes=notes,
        )
