"""服务门面（Service Facade）。

CLI、Web UI、评测脚本都通过这一层访问能力，保证"同一套提示词、同一套路由、
同一套记录"在三个入口完全一致——这是阶段三"控制变量法"要求的基础设施。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import APP_NAME, TEAM, __version__
from .config import load_config, resolve_data_dir
from .eval import RiskRunner
from .features import get_feature
from .features.base import FeatureContext, quality_check
from .features.plan import normalize_module
from .llm.registry import LLMClient, get_client
from .llm.router import TaskRouter
from .prompts import FIVE_ELEMENTS, STRUCTURE_PATTERNS, detect_module, get_module, list_modules
from .storage import Storage
from .study import StudyWorkspace


class AgentService:
    """一个进程内共享的服务实例。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 storage: Optional[Storage] = None, client: Optional[LLMClient] = None) -> None:
        self.config = config or load_config()
        self.data_dir = resolve_data_dir(self.config)
        self.storage = storage or Storage(self.data_dir / "ai_tutor.db")
        self.client = client or get_client(self.config)
        self.router = TaskRouter(self.client)
        self.study = StudyWorkspace(self.storage)

    # ------------------------------------------------------------ 基础信息
    def health(self) -> Dict[str, Any]:
        config = self.config.get("image", {}) or {}
        return {
            "app": APP_NAME,
            "team": TEAM,
            "version": __version__,
            "default_model": self.client.default,
            "available_models": self.client.available_names(),
            "usable_models": self.client.usable_names(),
            "vision_model": self.client.pick_vision_model(),
            "vision_models": self.client.vision_names(),
            "image": {
                "enabled": bool(config.get("enabled", True)),
                "max_images": int(config.get("max_images", 4)),
                "max_bytes": int(config.get("max_bytes", 8 * 1024 * 1024)),
                "allowed_types": config.get("allowed_types", ["png", "jpg", "jpeg", "webp", "gif"]),
            },
            "modules": [m.id for m in list_modules()],
            "storage": self.storage.stats(),
            "call_stats": self.client.stats_snapshot(),
            "router_enabled": True,
        }

    def models(self) -> List[Dict[str, Any]]:
        return self.client.describe()

    def routes(self) -> List[Dict[str, Any]]:
        return self.router.table_snapshot()

    def modules(self) -> List[Dict[str, Any]]:
        return [m.to_dict(include_prompts=False) for m in list_modules()]

    def prompt_library(self, module: Optional[str] = None) -> Dict[str, Any]:
        """返回提示词库（应用只提供精细提示模板）。"""
        specs = [get_module(module)] if module else list_modules()
        items = [s.to_dict(include_prompts=True) for s in specs if s]
        return {
            "methodology": {"five_elements": FIVE_ELEMENTS,
                            "structures": STRUCTURE_PATTERNS},
            "modules": items,
        }

    def detect(self, text: str) -> Dict[str, Any]:
        """自动识别模块与任务类型。

        注意：任务类型由**文本内容**决定（不把模块当先验），
        这样"某个模块里出现的文献类问题"仍会被路由到事实核查模型。
        """
        mid = detect_module(text)
        spec = get_module(mid)
        route = self.router.route(text)
        return {"module": mid, "title": spec.title if spec else "",
                "pain_point": spec.pain_point if spec else "",
                "route": route}

    # ------------------------------------------------------------ 会话
    def create_session(self, title: str = "", subject: str = "", module: str = "") -> Dict[str, Any]:
        return self.storage.create_session(title=title, subject=subject, module=module,
                                           model=self.client.default)

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.storage.list_sessions(limit=limit)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self.storage.get_session(session_id)

    # ------------------------------------------------------------ 问答主入口
    def ask(self, question: str, module: str = "auto",
            model: Optional[str] = None, session_id: Optional[str] = None,
            use_router: bool = False, temperature: Optional[float] = None,
            images: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """统一问答。module='auto' 时按关键词自动路由到六大模块。

        提示词固定使用精细模板（应用对外不提供提示词模式开关）。
        图片通过 `images` 传入：支持本地路径、base64、data URI 或它们的列表。
        """
        question = (question or "").strip()
        session = self.storage.get_session(session_id) if session_id else None
        if session_id and not session:
            raise LookupError("会话不存在，请开始新会话")
        course_id = kwargs.get("course_id") or None
        if session:
            bound_course = session["meta"].get("course_id")
            if course_id and course_id != bound_course:
                raise ValueError("切换课程需要开始新会话")
            course_id = bound_course
        course = self.study.course(course_id) if course_id else None
        has_images = bool(images)
        if not question and not has_images:
            raise ValueError("question 不能为空")
        if not question and has_images:
            question = "请识别图片中的题目并按模块要求解析。"
        module_id = detect_module(question) if module in ("auto", "", None) else normalize_module(module)
        ctx = FeatureContext(
            storage=self.storage, client=self.client, config=self.config, router=self.router,
            model=model, session_id=session_id, use_router=use_router, temperature=temperature,
            plain_prompt=bool(kwargs.get("plain_prompt", False)),
            study_context=(f"课程：{course['name']}；考试日期：{course['exam_date']}；"
                           f"目标：{course['goal']}；复习范围：{'、'.join(course['topics'])}。"
                           "结合范围讲解；先引导学生尝试，不将计划完成度当作掌握程度。") if course else "",
        )
        feature = get_feature(module_id)
        common = {"images": images}
        save = kwargs.get("save", True)

        # 按模块分发到支持多轮/额外参数的 run 方法
        if module_id == "M1":
            result = feature.run(question, ctx, save=save, **common)
        elif module_id == "M2":
            result = feature.run(question, ctx, subject=kwargs.get("subject", "自动识别"),
                                 audience=kwargs.get("audience", ""), save=save, **common)
        elif module_id == "M3":
            result = feature.run(question, ctx, student_answer=kwargs.get("student_answer", ""),
                                 save=save, **common)
        elif module_id == "M4":
            result = feature.run(question, ctx, level=kwargs.get("level", "零基础"),
                                 save=save, **common)
        elif module_id == "M5":
            result = feature.run(question, ctx, subject=kwargs.get("subject", ""),
                                 days=int(kwargs.get("days", 5) or 5),
                                 answers=kwargs.get("answers", ""),
                                 stage=kwargs.get("stage", "auto"),
                                 save=save, **common)
        elif module_id == "M6":
            result = feature.run(question, ctx, student_reply=kwargs.get("student_reply", ""),
                                 scenario=kwargs.get("scenario", ""),
                                 round_index=int(kwargs.get("round_index", 0) or 0),
                                 save=save, **common)
        else:
            result = feature.ask(question, ctx, save=save, **common)

        if course and result.session_id:
            ctx.update_session_meta({"course_id": course_id})
            self.storage._exec("UPDATE sessions SET subject=? WHERE id=?", [course["name"], result.session_id])
        if not session_id and result.session_id:
            self.storage._exec("UPDATE sessions SET title=? WHERE id=?", [question[:60], result.session_id])
        payload = result.to_dict()
        payload["session"] = self.storage.get_session(result.session_id)
        return payload

    # ------------------------------------------------------------ 批处理
    def compare(self, question: str, module: str = "auto", model: Optional[str] = None,
                use_router: bool = False) -> Dict[str, Any]:
        """提示词消融对照（评测层内部机制，用户界面不暴露）。

        同一问题、同一模型，比较两种提示词条件下的输出：
          ① 完整精细提示（应用正式使用的模板）
          ② 无约束提示（只给角色，不给格式/红线/诚实边界）
        用于回答"提示词结构本身对风险缓解有多少贡献"。
        """
        from .utils import similarity_report

        out: Dict[str, Any] = {"question": question, "module": None}
        for key, plain in (("constrained", False), ("plain", True)):
            result = self.ask(question, module=module, model=model, use_router=use_router,
                              save=True, plain_prompt=plain)
            out[key] = result
            out["module"] = result["module"]
        out["similarity"] = similarity_report(out["plain"]["answer"], out["constrained"]["answer"])
        out["delta"] = {
            "chars": len(out["constrained"]["answer"]) - len(out["plain"]["answer"]),
            "structure": round(out["constrained"]["checks"].get("structure_score", 0)
                               - out["plain"]["checks"].get("structure_score", 0), 4),
            "uncertainty": round(out["constrained"]["checks"].get("honesty_score", 0)
                                 - out["plain"]["checks"].get("honesty_score", 0), 4),
            "teaching": round(out["constrained"]["checks"].get("teaching_score", 0)
                              - out["plain"]["checks"].get("teaching_score", 0), 4),
        }
        return out

    # ------------------------------------------------------------ 风险实证
    def risk_runner(self, output_dir: Optional[str] = None) -> RiskRunner:
        from pathlib import Path
        out = Path(output_dir) if output_dir else (self.data_dir / "reports")
        ctx = FeatureContext(storage=self.storage, client=self.client, config=self.config,
                             router=self.router)
        return RiskRunner(ctx, output_dir=out)

    def run_risk(self, kind: str = "all", model: Optional[str] = None, rounds: int = 3,
                 models: Optional[Sequence[str]] = None, output_dir: Optional[str] = None) -> Dict[str, Any]:
        runner = self.risk_runner(output_dir)
        if kind == "all":
            reports = runner.run_all(model=model, rounds=rounds, models=models)
            return {k: v.to_dict() for k, v in reports.items()}
        if kind == "pair":
            return runner.run_risk_pair(model=model).to_dict()
        if kind == "repetition":
            return runner.run_repetition(rounds=rounds, model=model).to_dict()
        if kind == "comparison":
            return runner.run_comparison(models=models).to_dict()
        raise ValueError(f"未知的测试类型: {kind}（可选 pair / repetition / comparison / all）")

    def eval_runs(self, kind: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        return self.storage.list_eval_runs(kind=kind, limit=limit)

    def risk_records(self, case_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        return self.storage.list_risk_interactions(case_id=case_id, limit=limit)

    # ------------------------------------------------------------ 周计划（应用层编排）
    def weekly_plan(self, subject: str = "高等数学", days: int = 5,
                    weak_points: Optional[Sequence[str]] = None,
                    model: Optional[str] = None) -> Dict[str, Any]:
        """M5 的便捷封装：直接生成一份按天计划（跳过交互式诊断，用于批量导出）。"""
        question = f"帮我做一个{subject}的复习计划，还剩 {days} 天。"
        weak = "；".join(weak_points or ["可微性判别", "抽象函数二阶偏导漏项"]) 
        answers = (f"（离线批量模式）已知薄弱点：{weak}。请直接按这些薄弱点排计划，"
                   f"薄弱点时间占比不低于 40%。")
        return self.ask(question, module="M5", days=days, subject=subject, answers=answers,
                        stage="plan", model=model, save=True)

    def report(self, limit: int = 10) -> str:
        """生成一份 Markdown 汇总报告（阶段四"完整实践报告"的雏形）。"""
        from .utils import table

        stats = self.storage.stats()
        lines = [
            f"# {APP_NAME} · 运行报告",
            "",
            f"- 团队：{TEAM}",
            f"- 版本：{__version__}",
            f"- 默认模型：`{self.client.default}`　可用模型：{', '.join(self.client.available_names())}",
            "",
            "## 一、数据统计",
            "",
            f"- 会话：{stats['sessions']}　对话轮次：{stats['turns']}　产出物：{stats['artifacts']}",
            f"- 风险测试记录：{stats['risk_interactions']}　评测批次：{stats['eval_runs']}",
            f"- 数据库：`{stats['db_path']}`",
            "",
            "## 二、最近评测批次",
            "",
        ]
        runs = self.storage.list_eval_runs(limit=limit)
        if runs:
            lines.append(table(["时间", "类型", "标题", "文件"],
                               [[r["created_at"], r["kind"], r["title"], r["file_path"] or "—"] for r in runs],
                               width=30))
        else:
            lines.append("（暂无评测记录，可运行 `python -m ai_tutor risk run --kind all`）")

        lines += ["", "## 三、最近会话", ""]
        sessions = self.storage.list_sessions(limit=limit)
        if sessions:
            lines.append(table(["时间", "标题", "模块", "轮次"],
                               [[s["updated_at"], s["title"], s["module"] or "—", s["turn_count"]]
                                for s in sessions], width=28))
        else:
            lines.append("（暂无会话记录）")
        lines += ["", "## 四、模型调用统计", ""]
        stats_call = self.client.stats_snapshot()
        if stats_call:
            lines.append(table(["提供方", "成功", "失败"],
                               [[k, v.get("ok", 0), v.get("fail", 0)] for k, v in stats_call.items()]))
        else:
            lines.append("（暂无调用）")
        lines.append("")
        return "\n".join(lines)


_DEFAULT_SERVICE: Optional[AgentService] = None


def get_service(reload: bool = False) -> AgentService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None or reload:
        _DEFAULT_SERVICE = AgentService()
    return _DEFAULT_SERVICE
