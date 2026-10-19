"""生成渲染预览页：把一段真实模型回答经 md.js 渲染后展示，用于人工检查排版。

用途：改完渲染器或样式后，跑一次这个脚本 + 截图，就能确认长回答的实际观感。
运行：python tools/make_preview.py [回答文件]
"""

import html as html_mod
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "tests" / "fixtures" / "answer_sample.txt"
OUT = ROOT / "data" / "preview.html"

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>渲染预览 · 长回答排版检查</title>
<link rel="stylesheet" href="style.css">
<style>
  body {{ padding: 28px; max-width: 1000px; margin: 0 auto; }}
  .preview-head {{ font-size: 13px; color: #6b7280; margin-bottom: 16px; }}
  .preview-note {{ font-size: 12.5px; color: #6b7280; margin-top: 18px; }}
</style>
</head>
<body>
<div class="preview-head">
  下面这段是<b>真实模型回答</b>经 <code>md.js</code> 渲染后的效果（本地预览页，非应用页面）。
</div>
<div class="msg ai">
  <div class="msg-head"><span>M1 · 习题解析</span><span class="badge sim">deepseek</span></div>
  <div class="answer" id="ans"></div>
</div>
<div class="preview-note">原始回答 {chars} 字 · 渲染后 {html_chars} 字符 HTML</div>
<script src="md.js"></script>
<script type="text/plain" id="raw">{raw}</script>
<script>
  // 数据块必须在前：脚本一旦执行就要能读到 #raw 的文本内容
  document.getElementById("ans").innerHTML =
    renderRichText(document.getElementById("raw").textContent);
</script>
</body>
</html>
"""


def main() -> int:
    sample_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else SAMPLE
    if not sample_path.exists():
        raise SystemExit(f"找不到回答文件：{sample_path}")
    raw = sample_path.read_text(encoding="utf-8")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 预览页要引用同目录下的 style.css 与 md.js，因此把这两个文件复制过去
    for name in ("style.css", "md.js"):
        src = ROOT / "ai_tutor" / "web" / name
        (OUT.parent / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    page = TEMPLATE.format(
        raw=html_mod.escape(raw).replace("</script>", "<\\/script>"),
        chars=len(raw),
        html_chars=0,          # 渲染在浏览器里发生，这里占位
    )
    OUT.write_text(page, encoding="utf-8")
    print(f"预览页已生成：{OUT}（原文 {len(raw)} 字）")
    print(f"用浏览器打开：file:///{OUT.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
