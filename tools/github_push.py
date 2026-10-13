"""把工作区内容推送到 GitHub（纯 API 实现，不依赖本地 git 可执行文件）。

背景：本机装有 gh CLI 且已登录（token 含 repo 权限），但没有安装 git。
因此这里用 GitHub 的 Git Data API 直接构造提交：
    blob（每个文件）→ tree（目录结构）→ commit → 更新 refs/heads/main

用法：
    python tools/github_push.py --repo ai-tutor --message "初始提交" [--dry-run] [--private]

安全约定：
    · 跳过 .gitignore 列出的内容（密钥、数据库、评测中间产物）；
    · 跳过 secrect.txt / *.db / __pycache__ 等；
    · 推送前打印将要上传的文件清单与总大小，确认无误后再执行。
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent

#: 目录/文件名黑名单（这些永远不推送）
EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv", "env", "build", "dist",
                "data", "data_real", ".vscode", ".idea", "node_modules"}
EXCLUDE_FILES = {"secrect.txt", "secret.txt", "secrets.txt", "config.local.json",
                 ".DS_Store", "Thumbs.db", "desktop.ini", ".env"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".db", ".db-shm", ".db-wal", ".log", ".tmp"}
#: 评测中间产物文件名前缀（正式报告在 deliverables/ 下另有图解报告与 Excel）
EXCLUDE_PREFIX = ("risk_pair-", "repetition-", "comparison-")


@dataclass
class PushPlan:
    files: List[Path] = field(default_factory=list)
    total_bytes: int = 0

    def describe(self) -> str:
        by_dir: Dict[str, Tuple[int, int]] = {}
        for f in self.files:
            rel = f.relative_to(ROOT)
            top = rel.parts[0] if len(rel.parts) > 1 else "(根目录)"
            n, size = by_dir.get(top, (0, 0))
            by_dir[top] = (n + 1, size + f.stat().st_size)
        lines = [f"共 {len(self.files)} 个文件，{self.total_bytes / 1048576:.2f} MB", ""]
        for name, (n, size) in sorted(by_dir.items()):
            lines.append(f"  {name:<24} {n:>4} 个   {size / 1024:>9,.0f} KB")
        return "\n".join(lines)


def should_include(path: Path) -> bool:
    """是否应该上传。

    两道闸门：
      1) 内置黑名单（密钥、数据库、虚拟环境等）——即便 .gitignore 被误改也拦得住；
      2) **真正解析 .gitignore**——上一版只用了内置黑名单，导致
         `deliverables/**/risk_pair-*.json` 这类体积很大的评测中间产物被上传。
    """
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return False
    if path.name in EXCLUDE_FILES:
        return False
    if path.suffix.lower() in EXCLUDE_SUFFIX:
        return False
    if any(path.name.startswith(p) for p in EXCLUDE_PREFIX):
        return False
    return not _ignored_by_gitignore(rel.as_posix())


_IGNORE_CACHE: Optional[List[str]] = None


def _gitignore_patterns() -> List[str]:
    """读取 .gitignore 的有效模式（去掉注释、空行与取反规则）。"""
    global _IGNORE_CACHE
    if _IGNORE_CACHE is None:
        patterns: List[str] = []
        gi = ROOT / ".gitignore"
        if gi.exists():
            for line in gi.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                patterns.append(line)
        _IGNORE_CACHE = patterns
    return _IGNORE_CACHE


def _ignored_by_gitignore(rel_posix: str) -> bool:
    """按 .gitignore 的常用语法判断路径是否被忽略（支持 **、目录模式与文件名模式）。"""
    import fnmatch

    fname = rel_posix.rsplit("/", 1)[-1]
    for pattern in _gitignore_patterns():
        pat = pattern.lstrip("/")
        if pat.endswith("/"):
            base = pat.rstrip("/")
            if rel_posix == base or rel_posix.startswith(base + "/"):
                return True
            continue
        if fnmatch.fnmatch(rel_posix, pat):
            return True
        if "/" not in pat:
            # 单段模式：匹配任意层级下的同名文件或同名目录
            if fnmatch.fnmatch(fname, pat) or rel_posix == pat \
                    or rel_posix.startswith(pat + "/"):
                return True
    return False


def build_plan() -> PushPlan:
    plan = PushPlan()
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if not should_include(path):
            continue
        plan.files.append(path)
        plan.total_bytes += path.stat().st_size
    return plan


def run_gh(args: Sequence[str], *, stdin: Optional[str] = None,
           check: bool = True) -> Tuple[int, str]:
    """调用 gh，对含非 ASCII 的 JSON 走 --input 临时文件以避免命令行编码问题。"""
    cmd = ["gh"] + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          input=stdin, cwd=str(ROOT))
    out = (proc.stdout or "") + (proc.stderr or "")
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} 失败（{proc.returncode}）:\n{out[:2000]}")
    return proc.returncode, out.strip()


def gh_json(args: Sequence[str], payload: Optional[dict] = None,
            retries: int = 4, label: str = "") -> dict:
    """带 JSON body 的 gh api 调用（body 写入临时文件，避免中文与引号问题）。

    网络不稳时（`api.github.com` 偶发 EOF / 连接重置）自动指数退避重试，
    否则像上传大 PNG 这样的操作失败一次就得整体重来。
    """
    tmp = ROOT / ".gh-payload.json"
    if payload is not None:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    last_err = ""
    try:
        for attempt in range(1, retries + 1):
            cmd = ["api"] + list(args)
            if payload is not None:
                cmd += ["--input", str(tmp)]
            code, out = run_gh(cmd, check=False)
            if code == 0:
                try:
                    return json.loads(out) if out else {}
                except json.JSONDecodeError:
                    return {"_raw": out}
            last_err = out
            transient = any(k in out.lower() for k in
                            ("eof", "connection reset", "timeout", "timed out",
                             "502", "503", "504", "temporarily unavailable",
                             "unexpected end of json"))
            if not transient or attempt == retries:
                break
            wait = 2 ** attempt
            print(f"    网络异常，{wait}s 后重试（{attempt}/{retries - 1}）"
                  f"{'：' + label if label else ''}")
            time.sleep(wait)
    finally:
        if tmp.exists():
            tmp.unlink()
    if payload is not None and last_err:
        raise RuntimeError(f"gh api {args[0]} 失败：{last_err[:400]}")
    return {"_raw": last_err}


def _branch_exists(full: str, branch: str) -> bool:
    """判断目标分支是否已存在（空仓库时为 False）。"""
    code, _out = run_gh(["api", f"repos/{full}/git/ref/heads/{branch}"], check=False)
    return code == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="通过 GitHub API 推送当前工作区")
    ap.add_argument("--repo", default="ai-tutor", help="仓库名")
    ap.add_argument("--owner", default="", help="默认取当前登录账号")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="初始提交：大学生期末复习 AI 答疑助手 v1.1.0")
    ap.add_argument("--description", default="大学生期末复习 AI 答疑助手 —— 六大功能模块 · 精细提示词库 · 四类风险实证图解")
    ap.add_argument("--private", action="store_true", help="创建私有仓库")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不推送")
    ap.add_argument("--push-only", action="store_true", help="仓库已存在，只推送")
    ap.add_argument("--squash", action="store_true",
                    help="压成单个根提交（历史里不保留初始化提交）")
    args = ap.parse_args()

    plan = build_plan()
    print("=" * 70)
    print("将要上传的内容：")
    print(plan.describe())
    print("=" * 70)
    leaked = [f for f in plan.files if f.name in EXCLUDE_FILES]
    if leaked:
        print("!! 检测到不应上传的文件：", leaked)
        return 2
    if args.dry_run:
        print("（dry-run，未执行任何推送）")
        return 0

    # 1) 确定 owner 与仓库
    if not args.owner:
        user = gh_json(["user"])
        args.owner = user.get("login", "")
    if not args.owner:
        print("无法确定 GitHub 账号，请用 --owner 指定")
        return 2
    full = f"{args.owner}/{args.repo}"
    print(f"目标仓库：{full}")

    # 2) 创建仓库（已存在则跳过）
    if not args.push_only:
        code, out = run_gh(["repo", "create", full,
                            "--private" if args.private else "--public",
                            "--description", args.description], check=False)
        if code != 0 and "already exists" not in out.lower():
            print("创建仓库失败：", out[:800])
            return 1
        print("仓库已就绪")

    # 3) 空仓库需要先有一个 commit，否则 GitHub 拒绝创建 blob（HTTP 409）。
    #    这里先用 Contents API 放入一个种子文件（README.md），随后把**完整文件树**
    #    作为它的子提交覆盖上去 —— 因此不会留下多余的中间 commit。
    seed_rel = "README.md"
    seed_path = ROOT / seed_rel
    if not _branch_exists(full, args.branch):
        print("仓库为空，先用 Contents API 建立初始提交（种子文件 README.md）……")
        seed_content = (seed_path.read_text(encoding="utf-8")
                        if seed_path.exists() else "# 大学生期末复习 AI 答疑助手\n")
        gh_json(["-X", "PUT", f"repos/{full}/contents/{seed_rel}"],
                {"message": "chore: 初始化仓库", "branch": args.branch,
                 "content": base64.b64encode(seed_content.encode("utf-8")).decode("ascii")})
        print("初始提交已建立")

    # 4) 逐个文件建 blob
    print(f"上传 {len(plan.files)} 个文件……")
    tree_items: List[dict] = []
    for i, path in enumerate(plan.files, 1):
        rel = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        is_binary = b"\x00" in data[:8000]
        if is_binary:
            blob = gh_json([f"repos/{full}/git/blobs"],
                           {"encoding": "base64",
                            "content": base64.b64encode(data).decode("ascii")},
                           label=rel)
        else:
            blob = gh_json([f"repos/{full}/git/blobs"],
                           {"encoding": "utf-8", "content": data.decode("utf-8")},
                           label=rel)
        sha = blob.get("sha")
        if not sha:
            print(f"  失败：{rel} → {str(blob)[:200]}")
            return 1
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": sha})
        if i % 20 == 0 or i == len(plan.files):
            print(f"  {i}/{len(plan.files)}")

    # 4) 建 tree 与 commit
    tree = gh_json([f"repos/{full}/git/trees"], {"tree": tree_items})
    tree_sha = tree.get("sha")
    if not tree_sha:
        print("创建 tree 失败：", str(tree)[:400])
        return 1

    if args.squash:
        # 压成**根提交**（无父提交）后强制更新分支：历史里只剩一个干净提交，
        # 不会留下"初始化仓库"这类中间提交。文件内容完全相同。
        commit = gh_json([f"repos/{full}/git/commits"],
                         {"message": args.message, "tree": tree_sha, "parents": []})
        commit_sha = commit.get("sha")
        if not commit_sha:
            print("创建 commit 失败：", str(commit)[:400])
            return 1
        res = gh_json(["-X", "PATCH", f"repos/{full}/git/refs/heads/{args.branch}"],
                      {"sha": commit_sha, "force": True})
        if not res.get("object"):
            print("强制更新分支失败：", str(res)[:400])
            return 1
    else:
        # 以现有分支为父提交，正常快进
        parents: List[str] = []
        code, out = run_gh(["api", f"repos/{full}/git/ref/heads/{args.branch}"], check=False)
        if code == 0 and out:
            try:
                parents = [json.loads(out)["object"]["sha"]]
            except (json.JSONDecodeError, KeyError):
                parents = []
        commit = gh_json([f"repos/{full}/git/commits"],
                         {"message": args.message, "tree": tree_sha, "parents": parents})
        commit_sha = commit.get("sha")
        if not commit_sha:
            print("创建 commit 失败：", str(commit)[:400])
            return 1
        if parents:
            res = gh_json(["-X", "PATCH", f"repos/{full}/git/refs/heads/{args.branch}"],
                          {"sha": commit_sha, "force": False})
        else:
            res = gh_json([f"repos/{full}/git/refs"],
                          {"ref": f"refs/heads/{args.branch}", "sha": commit_sha})
        if not res.get("ref") and not res.get("object"):
            print("更新分支失败：", str(res)[:400])
            return 1

    print("=" * 70)
    print(f"推送完成：https://github.com/{full}")
    print(f"提交：{commit_sha[:12]}　分支：{args.branch}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
