"""数据采集层：把风险实证的测试结果整理成绘图需要的数据包。

三种数据来源，按优先级：
  1) **在线实测**（配置了 API Key）：现场跑 pair / repetition / comparison 三类测试；
  2) **已存报告**：从 `eval_runs` 表或 `data/reports/*.json` 读取之前批次的结果；
  3) **离线复现**：用内置离线模型重跑，保证任何环境下都能出图。

无论走哪条路径，生成的数据包都会带上 `source` 标注，图上会如实写明
"在线实测 / 离线复现 / 历史批次"，不会让人误以为是真实模型的成绩。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..eval.cases import (
    COMPARISON_CASES,
    HALLUCINATION_CASES,
    LOGIC_CASES,
    REPETITION_CASE,
    RiskCase,
    judge,
)
from ..eval.runner import RiskRunner
from ..features.base import FeatureContext
from ..service import AgentService
from ..utils import now_iso, similarity_report


@dataclass
class CaseRow:
    """一条用例在两个提示词条件下的完整记录。"""

    case_id: str
    kind: str                      # hallucination | logic
    title: str
    module: str
    question: str
    plain_answer: str = ""
    constrained_answer: str = ""
    plain_flags: List[str] = field(default_factory=list)
    constrained_flags: List[str] = field(default_factory=list)
    plain_risk: bool = False
    constrained_risk: bool = False
    plain_compliance: bool = False
    constrained_compliance: bool = False
    plain_conclusion: Optional[bool] = None
    constrained_conclusion: Optional[bool] = None

    @property
    def conclusion(self) -> str:
        if self.constrained_risk:
            return "仍有风险"
        if self.constrained_compliance:
            return "已抑制/纠正"
        return "不确定"


@dataclass
class ConditionSummary:
    """一个提示词条件的汇总指标。"""

    key: str                       # plain | constrained
    label: str
    model: str = ""
    simulated: bool = False
    hallucination_rate: float = 0.0
    logic_error_rate: float = 0.0
    compliance_rate: float = 0.0
    avg_structure: float = 0.0
    avg_uncertainty: float = 0.0
    avg_chars: int = 0
    hallucination_hits: int = 0
    hallucination_cases: int = 0
    logic_hits: int = 0
    logic_cases: int = 0


@dataclass
class RepetitionBlock:
    """同质化测试的一组结果。"""

    label: str
    texts: List[str] = field(default_factory=list)
    pair_matrix: List[List[float]] = field(default_factory=list)
    overall: float = 0.0
    ngram: float = 0.0
    lcs: float = 0.0
    structure: float = 0.0
    avg_chars: int = 0
    model: str = ""
    simulated: bool = False


@dataclass
class ToolColumn:
    """多工具对比表的一列。"""

    name: str
    vendor: str
    available: bool = False
    note: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AssetBundle:
    """绘图所需的全部数据。"""

    created_at: str = field(default_factory=now_iso)
    source: str = "offline"                 # online | reports | offline
    source_label: str = "离线复现（内置模型）"
    model: str = "mock"
    simulated: bool = True
    cases: List[CaseRow] = field(default_factory=list)
    summaries: List[ConditionSummary] = field(default_factory=list)
    repetition: List[RepetitionBlock] = field(default_factory=list)
    repetition_case: RiskCase = field(default_factory=lambda: REPETITION_CASE)
    tools: List[ToolColumn] = field(default_factory=list)
    comparison_cases: List[RiskCase] = field(default_factory=list)
    extra_notes: List[str] = field(default_factory=list)
    image_test: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ 便捷访问
    def case(self, case_id: str) -> Optional[CaseRow]:
        for row in self.cases:
            if row.case_id == case_id:
                return row
        return None

    def condition(self, key: str) -> Optional[ConditionSummary]:
        for s in self.summaries:
            if s.key == key:
                return s
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created_at": self.created_at,
            "source": self.source,
            "source_label": self.source_label,
            "model": self.model,
            "simulated": self.simulated,
            "summaries": [s.__dict__ for s in self.summaries],
            "cases": [c.__dict__ for c in self.cases],
            "repetition": [r.__dict__ for r in self.repetition],
            "tools": [t.__dict__ for t in self.tools],
            "extra_notes": list(self.extra_notes),
            "image_test": dict(self.image_test),
        }


# ----------------------------------------------------------------------
# 采集
# ----------------------------------------------------------------------

def _rows_from_records(records: Sequence[Dict[str, Any]]) -> List[CaseRow]:
    """把 runner 的逐条记录按键（case_id）合并成两个条件的对照行。"""
    by_case: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for rec in records:
        by_case.setdefault(rec["case_id"], {})[rec["mode"]] = rec

    case_index = {c.id: c for c in (HALLUCINATION_CASES + LOGIC_CASES)}
    rows: List[CaseRow] = []
    for case_id in [c.id for c in (HALLUCINATION_CASES + LOGIC_CASES)]:
        pair = by_case.get(case_id)
        if not pair:
            continue
        case = case_index[case_id]
        plain = pair.get("plain", {})
        constr = pair.get("constrained", {})
        pv = plain.get("verdict") or {}
        cv = constr.get("verdict") or {}
        rows.append(CaseRow(
            case_id=case_id,
            kind="hallucination" if case.type == "hallucination" else "logic",
            title=case.title, module=case.module, question=case.question,
            plain_answer=plain.get("answer", ""),
            constrained_answer=constr.get("answer", ""),
            plain_flags=list(pv.get("flags") or []),
            constrained_flags=list(cv.get("flags") or []),
            plain_risk=bool(pv.get("risk_hit")),
            constrained_risk=bool(cv.get("risk_hit")),
            plain_compliance=bool(pv.get("compliance")),
            constrained_compliance=bool(cv.get("compliance")),
            plain_conclusion=pv.get("conclusion_ok"),
            constrained_conclusion=cv.get("conclusion_ok"),
        ))
    return rows


def _summaries_from_report(report: Dict[str, Any]) -> List[ConditionSummary]:
    out: List[ConditionSummary] = []
    for s in report.get("summaries", []):
        key = "plain" if not s.get("prompt_strict", True) else "constrained"
        out.append(ConditionSummary(
            key=key,
            label=s.get("label") or key,
            model=s.get("model", ""),
            simulated=bool(s.get("simulated")),
            hallucination_rate=float(s.get("hallucination_rate", 0.0)),
            logic_error_rate=float(s.get("logic_error_rate", 0.0)),
            compliance_rate=float(s.get("compliance_rate", 0.0)),
            avg_structure=float(s.get("avg_structure", 0.0)),
            avg_uncertainty=float(s.get("avg_uncertainty", 0.0)),
            avg_chars=int(s.get("avg_chars", 0)),
            hallucination_hits=int(s.get("hallucination_hits", 0)),
            hallucination_cases=int(s.get("hallucination_cases", 0)),
            logic_hits=int(s.get("logic_hits", 0)),
            logic_cases=int(s.get("logic_cases", 0)),
        ))
    # 保证 plain 在前
    out.sort(key=lambda s: 0 if s.key == "plain" else 1)
    return out


def _pair_matrix(texts: Sequence[str]) -> List[List[float]]:
    n = len(texts)
    m = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            m[i][j] = 1.0 if i == j else similarity_report(texts[i], texts[j])["overall"]
    return m


def _repetition_block(records: Sequence[Dict[str, Any]], label: str,
                      model: str) -> RepetitionBlock:
    texts = [r["answer"] for r in records]
    avg = {"overall": 0.0, "ngram_jaccard": 0.0, "lcs_ratio": 0.0, "structure_sim": 0.0}
    pairs = 0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            rep = similarity_report(texts[i], texts[j])
            for k in avg:
                avg[k] += rep[k]
            pairs += 1
    if pairs:
        for k in avg:
            avg[k] /= pairs
    return RepetitionBlock(
        label=label, texts=texts, pair_matrix=_pair_matrix(texts),
        overall=round(avg["overall"], 4), ngram=round(avg["ngram_jaccard"], 4),
        lcs=round(avg["lcs_ratio"], 4), structure=round(avg["structure_sim"], 4),
        avg_chars=int(sum(len(t) for t in texts) / max(1, len(texts))),
        model=model, simulated=bool(records and records[0].get("simulated")),
    )


#: 第三阶段汇报 Slide 10 记录的参考结果（用于未配置 Key 时对照展示）
PPT_REFERENCE_TOOLS: List[Dict[str, Any]] = [
    {"name": "GPT-4o", "vendor": "OpenAI（PPT 主模型）", "available": False,
     "metrics": {"事实幻觉出现率": "约 20%（1/5）", "逻辑错误率": "约 20%（1/5）",
                 "结构完整性": "优秀，三部分齐全", "中文表达自然度": "良好，偏书面",
                 "重复度": "约 85%"}},
    {"name": "Claude 3.5 Sonnet", "vendor": "Anthropic（PPT 对照）", "available": False,
     "metrics": {"事实幻觉出现率": "约 10%（0.5/5）", "逻辑错误率": "约 20%（1/5）",
                 "结构完整性": "良好，偶有遗漏", "中文表达自然度": "良好，偏直译感",
                 "重复度": "约 70%"}},
    {"name": "文心一言 4.0", "vendor": "百度（PPT 对照）", "available": False,
     "metrics": {"事实幻觉出现率": "约 40%（2/5）", "逻辑错误率": "约 50%（2.5/5）",
                 "结构完整性": "良好，偏简略", "中文表达自然度": "优秀，符合中文习惯",
                 "重复度": "约 75%"}},
]


def collect_assets(svc: AgentService, *, mode: str = "auto", rounds: int = 3,
                   output_dir: Optional[Path | str] = None,
                   include_image_test: bool = True) -> AssetBundle:
    """采集绘图数据。

    mode:
      · "auto"    —— 有可用真实模型就实测，否则用已存报告，再否则离线复现
      · "online"  —— 强制现场实测（需要 API Key）
      · "offline" —— 强制使用内置离线模型

    注意：评测过程会产出 `risk_pair-*` / `repetition-*` 这类**中间报告**，
    体积大且属于过程数据，因此统一写到 `output_dir/_raw/` 子目录，
    避免与真正要交付的图解读物（PNG / Excel / 图文报告）混在一起。
    """
    outdir = Path(output_dir) if output_dir else None
    raw_dir = (outdir / "_raw") if outdir else None
    usable = [n for n in svc.client.usable_names() if n != "mock"]
    real_model = usable[0] if usable else None

    bundle: AssetBundle

    use_online = mode == "online" or (mode == "auto" and real_model is not None)
    if mode == "offline":
        use_online = False

    if use_online and real_model:
        bundle = _collect_online(svc, real_model, rounds, raw_dir)
    else:
        stored = load_bundle_from_reports(svc)
        if mode == "auto" and stored is not None and stored.source == "online":
            bundle = stored
        else:
            bundle = _collect_offline(svc, rounds, raw_dir)

    if include_image_test:
        bundle.image_test = run_image_test(svc, real_model if use_online else None)
    bundle.tools = build_tool_columns(bundle, usable)
    bundle.comparison_cases = list(COMPARISON_CASES)
    return bundle


def _collect_online(svc: AgentService, model: str, rounds: int,
                    outdir: Optional[Path]) -> AssetBundle:
    runner = svc.risk_runner(str(outdir) if outdir else None)
    pair = runner.run_risk_pair(model=model)
    rep = runner.run_repetition(rounds=rounds, model=model)
    details = pair.details
    summaries = _summaries_from_report(pair.to_dict())
    rep_blocks = _repetition_blocks_from_details(rep.details, model)
    return AssetBundle(
        source="online",
        source_label=f"在线实测（{model}）",
        model=model,
        simulated=False,
        cases=_rows_from_records(details),
        summaries=summaries,
        repetition=rep_blocks,
    )


def _collect_offline(svc: AgentService, rounds: int,
                     outdir: Optional[Path]) -> AssetBundle:
    from ..llm.mock import MockProvider
    from ..llm.registry import LLMClient

    client = LLMClient({"mock": MockProvider("mock-offline")}, default="mock")
    offline = AgentService(config=svc.config, storage=svc.storage, client=client)
    runner = offline.risk_runner(str(outdir) if outdir else None)
    pair = runner.run_risk_pair(model="mock")
    rep = runner.run_repetition(rounds=rounds, model="mock")
    return AssetBundle(
        source="offline",
        source_label="离线复现（内置模型）",
        model="mock",
        simulated=True,
        cases=_rows_from_records(pair.details),
        summaries=_summaries_from_report(pair.to_dict()),
        repetition=_repetition_blocks_from_details(rep.details, "mock"),
        extra_notes=[
            "本次图解使用**内置离线模型**生成：指标可复现、不消耗额度，"
            "但不代表任何商业模型的实际表现；配置 API Key 后重新生成即可得到真实数据。",
        ],
    )


def _repetition_blocks_from_details(details: Sequence[Dict[str, Any]],
                                    model: str) -> List[RepetitionBlock]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for d in details:
        groups.setdefault(d.get("variant", "默认精细提示"), []).append(d)
    blocks = []
    for label, records in groups.items():
        blocks.append(_repetition_block(records, label, model))
    blocks.sort(key=lambda b: 0 if "反同质化" not in b.label else 1)
    return blocks


def load_bundle_from_reports(svc: AgentService) -> Optional[AssetBundle]:
    """从 `eval_runs` 表读取最近一批 pair / repetition 结果。"""
    runs = svc.eval_runs(limit=50)
    pair_run = next((r for r in runs if r.get("kind") == "risk_pair"), None)
    rep_run = next((r for r in runs if r.get("kind") == "repetition"), None)
    if not pair_run:
        return None
    try:
        pair_detail = pair_run.get("detail") or {}
        pair_metrics = pair_run.get("metrics") or {}
        details = pair_detail.get("details") or []
        if not details:
            return None
        summaries = _summaries_from_report({"summaries": pair_metrics.get("summaries", [])})
        model = (pair_run.get("config") or {}).get("model_default", "")
        simulated = bool(pair_metrics.get("simulated", True))
        rep_blocks: List[RepetitionBlock] = []
        if rep_run:
            rep_detail = (rep_run.get("detail") or {}).get("details") or []
            rep_blocks = _repetition_blocks_from_details(rep_detail, model)
        return AssetBundle(
            source="online" if not simulated else "reports",
            source_label=("历史批次（真实模型）" if not simulated
                          else "历史批次（离线模型）"),
            model=model,
            simulated=simulated,
            cases=_rows_from_records(details),
            summaries=summaries,
            repetition=rep_blocks,
            extra_notes=[f"数据来自已存评测批次：{pair_run.get('title', '')}"
                         f"（{pair_run.get('created_at', '')}）"],
        )
    except Exception:  # noqa: BLE001 - 报告格式异常时退回离线复现
        return None


# ----------------------------------------------------------------------
# 多工具对比列
# ----------------------------------------------------------------------

def build_tool_columns(bundle: AssetBundle, usable: Sequence[str]) -> List[ToolColumn]:
    """构造多工具对比表：已配置的模型给真实数字，未配置的引用 PPT 参考值。"""
    cols: List[ToolColumn] = []
    plain = bundle.condition("plain")
    constr = bundle.condition("constrained")

    if plain and constr:
        for key, summary in (("plain", plain), ("constrained", constr)):
            name = f"{bundle.model}（{'无约束提示' if key == 'plain' else '精细提示'}）"
            cols.append(ToolColumn(
                name=name,
                vendor=f"实测 · {bundle.source_label}",
                available=True,
                note="本次实测",
                metrics={
                    "事实幻觉出现率": f"{summary.hallucination_rate:.0%}"
                                      f"（{summary.hallucination_hits}/{summary.hallucination_cases}）",
                    "逻辑错误率": f"{summary.logic_error_rate:.0%}"
                                  f"（{summary.logic_hits}/{summary.logic_cases}）",
                    "结构完整度": f"{summary.avg_structure:.2f}",
                    "不确定标记": f"{summary.avg_uncertainty:.2f}",
                    "平均字数": summary.avg_chars,
                },
            ))

    for ref in PPT_REFERENCE_TOOLS:
        cols.append(ToolColumn(
            name=ref["name"], vendor=ref["vendor"], available=False,
            note="PPT 参考值（未配置 Key）", metrics=dict(ref["metrics"]),
        ))
    return cols


# ----------------------------------------------------------------------
# 图片输入链路实测
# ----------------------------------------------------------------------

def run_image_test(svc: AgentService, model: Optional[str] = None) -> Dict[str, Any]:
    """用一张程序生成的题目图片实测图片输入链路，返回可绘图的记录。"""
    from ..selftest import _make_test_image

    out: Dict[str, Any] = {"ok": False}
    try:
        img = svc.data_dir / "risk-image-test.png"
        _make_test_image(img)
        result = svc.ask("请识别图片中的内容，并按模块要求解析。", module="M1",
                         images=[str(img)], model=model, save=True)
        out.update({
            "ok": True,
            "image_name": img.name,
            "image_bytes": img.stat().st_size,
            "vision": bool(result["meta"].get("vision")),
            "provider": result["meta"].get("provider", ""),
            "model": result["meta"].get("model", ""),
            "elapsed_ms": result["meta"].get("elapsed_ms", 0),
            "structure": result["checks"].get("structure_score", 0.0),
            "coverage": result["checks"].get("section_coverage", 0.0),
            "answer": result["answer"],
            "notes": list(result.get("notes", [])),
            "simulated": bool(result["meta"].get("simulated")),
            "image_path": str(img),
        })
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return out
