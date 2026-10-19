/* 渲染器离线测试：在 Node 里加载 ai_tutor/web/md.js（不依赖浏览器），
 * 用真实模型回答 + 手工构造的边界用例验证渲染结果。
 *
 * 运行：node tests/md_test.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(root, "ai_tutor/web/md.js"), "utf8"), sandbox);
const { renderRichText, convertMath } = sandbox.window;

let pass = 0;
let fail = 0;

function check(name, cond, detail) {
  if (cond) {
    pass++;
    console.log(`  [✓] ${name}`);
  } else {
    fail++;
    console.log(`  [✗] ${name}${detail ? " → " + detail : ""}`);
  }
}

console.log("=== 1. Markdown 结构 ===");
let html = renderRichText("# 标题\n\n正文一段。\n\n- 项目一\n- 项目二\n\n1. 第一步\n2. 第二步");
check("标题渲染为 h4", /<h4 class="md-h">标题<\/h4>/.test(html), html.slice(0, 80));
check("无序列表", /<ul class="md-list">[\s\S]*项目一[\s\S]*<\/ul>/.test(html));
check("有序列表", /<ol class="md-list">[\s\S]*第一步[\s\S]*<\/ol>/.test(html));
check("段落", /<p class="md-p">正文一段。<\/p>/.test(html));

console.log("\n=== 2. 行内元素 ===");
html = renderRichText("这是**粗体**和*斜体*，还有`code`与==高亮==。");
check("粗体", html.includes("<strong>粗体</strong>"));
check("斜体", html.includes("<em>斜体</em>"));
check("行内代码", html.includes('<code class="inline">code</code>'));
check("高亮", html.includes('<mark class="hl">高亮</mark>'));

console.log("\n=== 3. 安全（XSS）===");
html = renderRichText('<script>alert(1)</script> 与 <img src=x onerror=alert(1)>');
check("脚本标签被转义", !html.includes("<script>") && html.includes("&lt;script&gt;"));
check("危险属性被转义", !/onerror=/.test(html) || html.includes("&lt;img"));
html = renderRichText("[点我](javascript:alert(1))");
check("javascript: 链接不生成 a 标签",
  !html.includes("<a ") && html.includes("javascript:"), html);

console.log("\n=== 4. LaTeX 公式 ===");
check("行内公式", renderRichText("原式 $x^2 + y^2 = z^2$").includes('class="math-inline"'));
check("分式", convertMath("\\frac{\\sin x - x}{x^3}").includes("mx-frac"));
check("嵌套分式", (() => {
  const m = convertMath("\\frac{\\frac{1}{2}}{3}");
  return (m.match(/mx-frac/g) || []).length === 2;
})());
check("根号", convertMath("\\sqrt{x^2+1}").includes("mx-sqrt"));
check("希腊字母", convertMath("\\alpha \\beta \\Delta").includes("α"));
check("上下标", convertMath("a_{n+1} = a_n + 1").includes("<sub>n+1</sub>"));
check("未识别命令保留可见", convertMath("\\unknowncmd{x}").includes("mx-raw"));
check("独立公式块", renderRichText("$$\nE = mc^2\n$$").includes('class="math-block"'));

console.log("\n=== 5. ASCII 图（必须等宽保留）===");
const art = [
  "客户端                    服务器",
  "  |  ① SYN=1, seq=x        |",
  "  |----------------------->|",
  "  |  ② SYN=1, ACK=1        |",
  "  |<-----------------------|",
];
html = renderRichText(art.join("\n"));
check("识别为 ASCII 图", html.includes('class="md-art"'));
check("保留换行与空格", html.includes("                    服务器"));
check("使用 pre 标签", /<pre class="md-art">/.test(html));

console.log("\n=== 6. 表格 ===");
html = renderRichText("| 概念 | 说明 |\n| --- | --- |\n| TCP | 面向连接 |\n| UDP | 无连接 |");
check("表格结构", html.includes("<table class=\"md-table\"") && html.includes("<th>概念</th>"));
check("表格数据", html.includes("<td>面向连接</td>"));

console.log("\n=== 7. 代码块 ===");
html = renderRichText("```python\nprint('hi')\n```");
check("围栏代码块", html.includes('<pre class="md-code" data-lang="python">'));
check("代码内容保留", html.includes("print(&#39;hi&#39;)") || html.includes("print('hi')"));

console.log("\n=== 8. 段落标记 【】 ===");
html = renderRichText("【题型判断】这是一道极限题。");
check("段落标记变标签", html.includes('<span class="sec-tag">题型判断</span>'));

console.log("\n=== 9. 真实模型回答回归 ===");
const samplePath = path.join(root, "tests/fixtures/answer_sample.txt");
if (fs.existsSync(samplePath)) {
  const sample = fs.readFileSync(samplePath, "utf8");
  html = renderRichText(sample);
  check("真实回答渲染不抛异常且非空", html.length > 200, `长度 ${html.length}`);
  check("真实回答中的公式被渲染", !/\\frac\{/.test(html), "仍存在裸 LaTeX");
  check("真实回答中的 ASCII 图被识别", html.includes("md-art") || !/\|\s{4,}\|/.test(sample));
} else {
  console.log("  (跳过：缺少 tests/fixtures/answer_sample.txt)");
}

console.log("\n=== 10. 空与异常输入 ===");
check("空字符串", renderRichText("") === "");
check("null", renderRichText(null) === "");
check("仅空白", renderRichText("   \n\n  ") === "");

console.log(`\n结果：${pass} 通过，${fail} 失败`);
process.exit(fail ? 1 : 0);
