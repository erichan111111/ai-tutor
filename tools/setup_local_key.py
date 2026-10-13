"""开发辅助脚本：把 secrect.txt 里的 DeepSeek Key 写入本地 config.json（一次性）。

为什么需要它：公开仓库的 config.json 必须保持 api_key 为空，但**本地**只有填了 Key
才能用真实模型。这个脚本从 secrect.txt 读取密钥并写入本地 config.json，
同时保证 config.json 已在 .gitignore 中（避免误推公开仓库）。
"""

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read_key() -> str:
    for name in ("secrect.txt", "secret.txt"):
        f = ROOT / name
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"sk-[A-Za-z0-9]{8,}", text)
        if m:
            return m.group(0)
    raise SystemExit("未在 secrect.txt / secret.txt 中找到 sk- 开头的密钥")


def ensure_ignored(pattern: str) -> bool:
    gi = ROOT / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if any(line.strip() == pattern for line in lines):
        return False
    lines.insert(0, pattern)
    lines.insert(0, "# ---- 本地真实配置（含 API Key）：绝不入库 ----")
    gi.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def main() -> int:
    key = read_key()
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    models = cfg.setdefault("llm", {}).setdefault("models", {})
    models.setdefault("deepseek", {})["api_key"] = key
    # 顺便把带视觉能力的提供方与默认模型理顺
    models["deepseek"]["model"] = models["deepseek"].get("model") or "deepseek-flash"
    models["deepseek"]["vision"] = True
    cfg["llm"]["default"] = "deepseek"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    added = ensure_ignored("config.json")
    masked = key[:8] + "*" * 8
    print(f"已写入本地 config.json：deepseek.api_key = {masked}")
    print(f".gitignore {'已补充' if added else '已包含'} config.json 忽略规则")
    # 探针：确认推送脚本确实会跳过它
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import github_push
        will_skip = not github_push.should_include(cfg_path)
        print(f"github_push 是否会跳过 config.json：{'是 ✓' if will_skip else '否 !!'}")
    except Exception as exc:  # noqa: BLE001
        print(f"（未能加载 github_push 探测：{exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
