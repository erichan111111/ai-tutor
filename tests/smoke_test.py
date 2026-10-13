"""快速自检：验证核心链路（提示词 → 调用 → 质检 → 落库 → 指标）。"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_tutor.prompts import get_module, list_modules, prompt_library_markdown  # noqa: E402
from ai_tutor.service import AgentService  # noqa: E402
from ai_tutor.utils import mean_pairwise_similarity  # noqa: E402


def main() -> int:
    print("== 1. 提示词库（只提供精细提示）==")
    for spec in list_modules():
        fine = spec.fine_prompt()
        assert len(fine) > 100, f"{spec.id} 精细提示过短"
        assert not hasattr(spec, "coarse_prompt"), f"{spec.id} 仍残留粗放提示字段"
        print(f"  {spec.id} {spec.title}: fine={len(fine)}字符 pattern={spec.pattern} "
              f"段落={len(spec.structure.get('sections', []))}")
    md = prompt_library_markdown()
    assert "M6" in md and "五要素" in md
    print(f"  提示词库文档长度: {len(md)} 字符")

    print("== 2. 服务与模块调用 ==")
    from ai_tutor.selftest import offline_service
    svc = offline_service(AgentService())   # 冒烟测试固定走离线模型：可复现、不消耗额度
    print("  health:", {k: v for k, v in svc.health().items()
                        if k in ("default_model", "available_models", "modules")})
    cases = [
        ("M1", "帮我解这道题：lim(x→0) (sin x - x) / x³"),
        ("M2", "帮我整理一下 TCP 协议的知识点"),
        ("M3", "帮我分析一下这个错题：这道题我第2步把 cos x - 1 直接替换成了 0"),
        ("M4", "帮我解释一下递归是什么"),
        ("M5", "帮我做个高等数学的复习计划，还剩 5 天"),
        ("M6", "陪我练一下计算机网络三次握手的背诵"),
    ]
    for mid, q in cases:
        res = svc.ask(q, module=mid, save=False)
        checks = res["checks"]
        print(f"  {mid}: {len(res['answer'])}字符 结构={checks['structure_score']:.2f} "
              f"诚实={checks['honesty_score']:.2f} 教学={checks['teaching_score']:.2f} "
              f"覆盖率={checks['section_coverage']:.2f} 模型={res['meta']['model']}")

    print("== 3. 提示词消融对照（评测层内部机制）==")
    cmp = svc.compare("帮我整理一下 TCP 协议的知识点", module="M2")
    print("  无约束:", len(cmp["plain"]["answer"]), "字符, 结构分", cmp["plain"]["checks"]["structure_score"])
    print("  精细  :", len(cmp["constrained"]["answer"]), "字符, 结构分",
          cmp["constrained"]["checks"]["structure_score"])
    print("  差异:", cmp["delta"])

    print("== 4. 风险判定 ==")
    from ai_tutor.eval import HALLUCINATION_CASES, LOGIC_CASES, judge
    for case in (HALLUCINATION_CASES[0], HALLUCINATION_CASES[1], LOGIC_CASES[0], LOGIC_CASES[2], LOGIC_CASES[3]):
        for plain in (True, False):
            tag = "plain" if plain else "fine"
            question = (case.plain_prompt or case.question) if plain else case.question
            r = svc.ask(question, module=case.module, save=False, plain_prompt=plain)
            v = judge(case, r["answer"])
            print(f"  {case.id}[{tag}] risk={v['risk_hit']} compliance={v['compliance']} "
                  f"conclusion={v['conclusion_ok']}")

    print("== 5. 重复度算法 ==")
    a = svc.ask("请为「高等数学·多元函数微分学」生成一份期末复习提纲。", module="M5", save=False)["answer"]
    b = svc.ask("请为「高等数学·多元函数微分学」生成一份期末复习提纲。", module="M5", save=False)["answer"]
    print("  完全相同两次:", mean_pairwise_similarity([a, b]))

    from ai_tutor.eval import ANTI_REPETITION_PROMPTS, REPETITION_PROMPTS
    base = [svc.ask(q, module="M5", save=False)["answer"] for q in REPETITION_PROMPTS]
    anti = [svc.ask(q, module="M5", save=False)["answer"] for q in ANTI_REPETITION_PROMPTS]
    base_rep = mean_pairwise_similarity(base)["overall"]
    anti_rep = mean_pairwise_similarity(anti)["overall"]
    print(f"  默认精细提示（换措辞问 3 次）重复度: {base_rep:.0%}")
    print(f"  加入反同质化约束后重复度:        {anti_rep:.0%}")
    assert base_rep > anti_rep, "反同质化约束应显著降低重复度"
    assert base_rep > 0.8, "默认精细提示应表现出明显的模板化倾向"
    assert anti_rep < 0.6, "反同质化后重复度应明显下降"

    print("== 6. 图片输入链路 ==")
    from ai_tutor import selftest as _st
    img = svc.data_dir / "smoke-question.png"
    _st._make_test_image(img)
    res = svc.ask("请看图片中的题目并解析。", module="M1", images=[str(img)], save=False)
    print("  vision:", res["meta"].get("vision"), "| images:", len(res["meta"].get("images", [])),
          "| 模型:", res["meta"].get("provider"))
    assert res["meta"]["vision"] is True
    assert "【图片输入】" in res["prompt"]

    print("\n全部自检通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
