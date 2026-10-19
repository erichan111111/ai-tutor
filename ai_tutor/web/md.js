/* ============================================================================
 * 零依赖的「Markdown + LaTeX 子集」渲染器
 *
 * 为什么需要：真实模型的回答里有三类内容，直接塞进 <div> 会很难读——
 *   1. Markdown（标题 / 列表 / 表格 / 粗体 / 代码块）
 *   2. LaTeX 公式（$\frac{\sin x-x}{x^3}$、$$...$$）
 *   3. 用空格和竖线画的 ASCII 图（例如 TCP 三次握手时序图）
 *
 * 设计要点：
 *   · 零依赖（项目坚持不引入第三方库），全部逻辑可读可改；
 *   · **先占位再解析**：行内代码与公式先换成占位符，避免公式里的 * _ \ 被 Markdown 规则破坏；
 *   · **一切文本先转义**：HTML 只由本文件拼装，模型输出无法注入脚本；
 *   · LaTeX 用递归下降解析（支持 \frac 嵌套），未识别的命令以等宽样式原样显示。
 * ==========================================================================*/
(function (global) {
  "use strict";

  // ------------------------------------------------------------------ 工具
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** 用于 <pre>/<code> 内部：引号无需转义，只处理会破坏结构的 & < >，
   *  这样 JSON 代码块里就不会出现 &quot; 这种噪音。 */
  function escCode(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  var GREEK = {
    alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε",
    zeta: "ζ", eta: "η", theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ",
    lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π", rho: "ρ", sigma: "σ",
    tau: "τ", upsilon: "υ", phi: "φ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω",
    Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
    Sigma: "Σ", Phi: "Φ", Psi: "Ψ", Omega: "Ω"
  };

  var SYMBOLS = {
    times: "×", div: "÷", cdot: "·", pm: "±", mp: "∓",
    leq: "≤", le: "≤", geq: "≥", ge: "≥", neq: "≠", ne: "≠", approx: "≈", equiv: "≡",
    to: "→", rightarrow: "→", leftarrow: "←", Rightarrow: "⇒", Leftarrow: "⇐",
    leftrightarrow: "↔", implies: "⟹", iff: "⟺",
    infty: "∞", partial: "∂", nabla: "∇", sum: "∑", prod: "∏", int: "∫",
    in: "∈", notin: "∉", subset: "⊂", subseteq: "⊆", cup: "∪", cap: "∩",
    emptyset: "∅", forall: "∀", exists: "∃", wedge: "∧", vee: "∨", neg: "¬",
    propto: "∝", sim: "∼", angle: "∠", perp: "⊥", parallel: "∥",
    cdots: "⋯", ldots: "…", dots: "…", prime: "′",
    quad: "\u2003", qquad: "\u2003\u2003",
    lim: "lim", sin: "sin", cos: "cos", tan: "tan", cot: "cot", sec: "sec", csc: "csc",
    ln: "ln", log: "log", lg: "lg", exp: "exp", max: "max", min: "min",
    det: "det", arg: "arg", deg: "deg", mod: "mod"
  };

  /** 按层级取一个 {…} 分组（支持嵌套）；返回 [内容, 新位置] */
  function readGroup(s, i) {
    while (i < s.length && /\s/.test(s[i])) i++;
    if (s[i] !== "{") {
      if (i < s.length) return [s[i], i + 1];      // \frac12 这种简写
      return ["", i];
    }
    var depth = 0, start = i + 1;
    for (var j = i; j < s.length; j++) {
      if (s[j] === "{") depth++;
      else if (s[j] === "}") {
        depth--;
        if (depth === 0) return [s.slice(start, j), j + 1];
      }
    }
    return [s.slice(start), s.length];
  }

  /**
   * LaTeX 子集 → HTML（递归下降，支持 \frac 嵌套）。
   * 未识别的 \command 会渲染成等宽小标签，保证"看得见原始写法"而不是静默丢失。
   */
  function convertMath(tex) {
    var s = String(tex || "");
    var out = "";
    var i = 0;

    while (i < s.length) {
      var ch = s[i];

      // 转义字符
      if (ch === "\\") {
        var m = /^\\([A-Za-z]+)/.exec(s.slice(i));
        if (!m) {                                   // \{ \} \, \; 等单字符命令
          var nxt = s[i + 1] || "";
          if (nxt === "," || nxt === ";" || nxt === " ") out += " ";
          else if (nxt === "{" || nxt === "}" || nxt === "%" || nxt === "_" || nxt === "&") out += esc(nxt);
          else if (nxt === "\\") out += "<br>";
          i += 2;
          continue;
        }
        var name = m[1];
        i += m[0].length;

        if (name === "frac" || name === "dfrac" || name === "tfrac") {
          var a = readGroup(s, i); i = a[1];
          var b = readGroup(s, i); i = b[1];
          out += '<span class="mx-frac"><span class="mx-num">' + convertMath(a[0]) +
                 '</span><span class="mx-den">' + convertMath(b[0]) + "</span></span>";
          continue;
        }
        if (name === "sqrt") {
          var idx = "";
          if (s[i] === "[") {                        // \sqrt[3]{x}
            var close = s.indexOf("]", i);
            if (close > 0) { idx = s.slice(i + 1, close); i = close + 1; }
          }
          var x = readGroup(s, i); i = x[1];
          out += '<span class="mx-sqrt">' + (idx ? "<sup>" + convertMath(idx) + "</sup>" : "") +
                 convertMath(x[0]) + "</span>";
          continue;
        }
        if (name === "text" || name === "mathrm" || name === "operatorname") {
          var t = readGroup(s, i); i = t[1];
          out += '<span class="mx-text">' + esc(t[0]) + "</span>";
          continue;
        }
        if (name === "left" || name === "right") {   // \left( \right) → 直接取后面的括号
          if (i < s.length && /[()\[\]|.]/.test(s[i])) {
            if (s[i] !== ".") out += esc(s[i]);
            i++;
          }
          continue;
        }
        // 排版类命令：只影响尺寸，忽略即可（否则会以 mx-raw 原样露出）
        if (name === "displaystyle" || name === "textstyle" ||
            name === "scriptstyle" || name === "limits" || name === "nolimits") {
          continue;
        }
        if (Object.prototype.hasOwnProperty.call(GREEK, name)) { out += GREEK[name]; continue; }
        if (Object.prototype.hasOwnProperty.call(SYMBOLS, name)) {
          var sym = SYMBOLS[name];
          if (/^[a-z]{2,}$/.test(sym)) out += '<span class="mx-fn">' + sym + "</span>";
          else out += sym;
          continue;
        }
        out += '<span class="mx-raw">\\' + esc(name) + "</span>";
        continue;
      }

      // 上下标
      if (ch === "^" || ch === "_") {
        var g = readGroup(s, i + 1);
        i = g[1];
        var tag = ch === "^" ? "sup" : "sub";
        out += "<" + tag + ">" + convertMath(g[0]) + "</" + tag + ">";
        continue;
      }

      // 花括号仅作分组
      if (ch === "{" || ch === "}") { i++; continue; }

      // 数学里保留空格
      if (ch === " ") { out += " "; i++; continue; }

      out += esc(ch);
      i++;
    }

    return out.trim();
  }

  // ------------------------------------------------------- 行内元素解析
  /** 行内：`code`、$公式$、**粗体**、*斜体*、==高亮==、~~删除~~、[文字](链接)、【段落名】 */
  function inline(text) {
    var slots = [];
    function stash(html) {
      slots.push(html);
      return "\u0000" + (slots.length - 1) + "\u0001";
    }

    var s = esc(text);

    // 行内代码优先：内部不再解析任何标记
    s = s.replace(/`([^`\n]+)`/g, function (_m, code) {
      return stash('<code class="inline">' + code.trim() + "</code>");
    });

    // 公式 $...$、\(...\)、\[...\] —— 真实模型（DeepSeek）常用的定界符是 \( \) 与 \[ \]
    s = s.replace(/\\\(([\s\S]+?)\\\)/g, function (_m, tex) {
      return stash('<span class="math-inline">' + convertMath(tex) + "</span>");
    });
    s = s.replace(/\\\[([\s\S]+?)\\\]/g, function (_m, tex) {
      return stash('<span class="math-inline math-display-inline">' + convertMath(tex) + "</span>");
    });
    s = s.replace(/\$([^$\n]+?)\$/g, function (_m, tex) {
      return stash('<span class="math-inline">' + convertMath(tex) + "</span>");
    });

    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/==([^=\n]+)==/g, '<mark class="hl">$1</mark>');
    s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    // 【段落标记】→ 小标签样式，便于扫读
    s = s.replace(/【([^】\n]{1,24})】/g, '<span class="sec-tag">$1</span>');

    return s.replace(/\u0000(\d+)\u0001/g, function (_m, idx) { return slots[Number(idx)]; });
  }

  // ------------------------------------------------------------ 结构判定
  function isFence(line) { return /^\s*(```|~~~)/.test(line); }

  /** ASCII 图 / 时序图检测：连续多行且大量出现 | + - = → 之类制表字符 */
  function isAsciiArt(lines) {
    var joined = lines.join("\n");
    if (joined.trim().length < 24) return false;
    var boxy = (joined.match(/[|+\-=<>→←↑↓]/g) || []).length;
    var hasPipeRow = lines.filter(function (l) { return /\|/.test(l) }).length >= 2;
    var spaces = (joined.match(/ {2,}/g) || []).length;
    return hasPipeRow && (boxy / Math.max(1, joined.length) > 0.12 || spaces > 4);
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|")
      .map(function (c) { return c.trim(); });
  }
  function isTableSep(line) {
    return /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && /-/.test(line);
  }

  // ------------------------------------------------------------ 主渲染
  /**
   * 把模型回答渲染成 HTML。
   * @param {string} text 原始回答
   * @returns {string} 可直接 innerHTML 的安全 HTML
   */
  function renderRichText(text) {
    var src = String(text == null ? "" : text).replace(/\r\n?/g, "\n").trim();
    if (!src) return "";

    // ── 第零步：把跨行的行内公式 \(...\) 压成一行
    //    模型常把 \( 与 \) 拆到多行（实测有跨三行的长公式），若只按单行匹配，
    //    会留下一个裸 "\)"，随后被转义成 &lt;br&gt; 之类的垃圾字符。
    src = src.replace(/\\\(([\s\S]+?)\\\)/g, function (_m, body) {
      return "\\(" + body.replace(/\s*\n\s*/g, " ") + "\\)";
    });

    // ── 第一步：把块级公式抽出来换成 ASCII 占位符（@@B0@@ 这种）
    //    必须用纯 ASCII：占位符之后会经过 esc()，若含特殊字符会被破坏。
    var blocks = [];
    function extractBlocks(str) {
      var lines = str.split("\n");
      var res = [];
      var inFence = false;
      for (var k = 0; k < lines.length; k++) {
        var l = lines[k];
        if (isFence(l)) { inFence = !inFence; res.push(l); continue; }
        if (inFence) { res.push(l); continue; }          // 代码块内不提取
        var t = l.trim();
        // 单行 $$...$$ / \[...\]
        var one = t.match(/^\$\$([\s\S]+?)\$\$$/) || t.match(/^\\\[([\s\S]+?)\\\]$/);
        if (one) {
          blocks.push(convertMath(one[1]));
          res.push("@@B" + (blocks.length - 1) + "@@");
          continue;
        }
        // 多行块：起始行是 $$ 或 \[，向下找到结束行
        if (/^\$\$$/.test(t) || /^\\\[$/.test(t)) {
          var closeRe = /^\$\$$/.test(t) ? /^\$\$$/ : /^\\\]$/;
          var buf = [];
          k++;
          while (k < lines.length && !closeRe.test(lines[k].trim())) { buf.push(lines[k]); k++; }
          blocks.push(convertMath(buf.join(" ")));
          res.push("@@B" + (blocks.length - 1) + "@@");
          continue;
        }
        res.push(l);
      }
      return res.join("\n");
    }
    src = extractBlocks(src);

    var lines = src.split("\n");
    var out = [];
    var i = 0;
    var BADGE = /^\s*@@B(\d+)@@\s*$/;

    while (i < lines.length) {
      var line = lines[i];

      // ---- 块级公式占位符
      var bm = line.match(BADGE);
      if (bm) {
        out.push('<div class="math-block">' + (blocks[Number(bm[1])] || "") + "</div>");
        i++;
        continue;
      }

      // ---- 围栏代码块
      if (isFence(line)) {
        var fence = line.trim().slice(0, 3);
        var lang = line.trim().slice(3).trim();
        var buf = [];
        i++;
        while (i < lines.length && lines[i].trim().indexOf(fence) !== 0) { buf.push(lines[i]); i++; }
        i++;
        // 代码块是被围栏包住的纯文本：只做必要的 HTML 转义（& < >），不再解析 Markdown；
        // 引号不转义，避免 JSON 代码块里出现一行行 &quot; 噪音。
        var codeText = buf.join("\n")
          .replace(/&quot;/g, '"')
          .replace(/&#39;|&apos;/g, "'");
        out.push('<pre class="md-code"' + (lang ? ' data-lang="' + esc(lang) + '"' : "") +
                 "><code>" + escCode(codeText) + "</code></pre>");
        continue;
      }

      // ---- $$ 与 \[ \] 已在 extractBlocks 阶段统一抽出，这里不需要再兜底

      if (!line.trim()) { i++; continue; }

      // ---- 标题（回答里的 # 降级，避免压过页面标题）
      var hm = line.match(/^\s*(#{1,6})\s+(.*)$/);
      if (hm) {
        var lv = Math.min(6, hm[1].length + 3);
        out.push("<h" + lv + ' class="md-h">' + inline(hm[2].replace(/<br\s*\/?>/gi, " ")) + "</h" + lv + ">");
        i++;
        continue;
      }

      // ---- 分隔线
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push('<hr class="md-hr">'); i++; continue; }

      // ---- 表格
      if (/\|/.test(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        var head = splitRow(line);
        i += 2;
        var rows = [];
        while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        out.push('<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
          head.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") +
          "</tr></thead><tbody>" +
          rows.map(function (r) {
            return "<tr>" + r.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>";
          }).join("") +
          "</tbody></table></div>");
        continue;
      }

      // ---- 引用块
      if (/^\s*>\s?/.test(line)) {
        var quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        out.push('<blockquote class="md-quote">' + renderRichText(quote.join("\n")) + "</blockquote>");
        continue;
      }

      // ---- ASCII 图（时序图 / 结构图）：整段等宽保留
      var art = [];
      var j = i;
      while (j < lines.length && lines[j].trim() !== "") { art.push(lines[j]); j++; }
      if (isAsciiArt(art)) {
        out.push('<pre class="md-art">' + esc(art.join("\n")) + "</pre>");
        i = j;
        continue;
      }

      // ---- 列表（有序 / 无序，支持一层缩进）
      //      模型常在两个列表项之间插入空行或块级公式；若按"连续行"分组，
      //      每个 item 会变成独立 <ol>，浏览器就各自从 1 开始编号（表现为 1. 1. 1.）。
      //      这里允许跨过空行与公式占位符继续收集，并把公式插回所属项。
      var um = line.match(/^(\s*)[-*+]\s+(.*)$/);
      var om = line.match(/^(\s*)(\d+)[.、)]\s+(.*)$/);
      if (um || om) {
        var ordered = !!om;
        var items = [];
        while (i < lines.length) {
          var l2 = lines[i];
          var m2 = ordered ? l2.match(/^(\s*)(\d+)[.、)]\s+(.*)$/) : l2.match(/^(\s*)[-*+]\s+(.*)$/);
          var b2 = l2.match(/^\s*@@B(\d+)@@\s*$/);
          if (m2) {
            items.push({ indent: m2[1].length, text: ordered ? m2[3] : m2[2], blocks: [] });
            i++;
          } else if (b2 && items.length) {
            items[items.length - 1].blocks.push(b2[1]);
            i++;
          } else if (!l2.trim() && items.length) {
            var k2 = i + 1;
            while (k2 < lines.length && !lines[k2].trim()) k2++;
            if (k2 < lines.length) {
              var w2 = lines[k2];
              var cont = (ordered ? /^\s*(\d+)[.、)]\s+/ : /^\s*[-*+]\s+/).test(w2)
                      || /^\s*@@B(\d+)@@\s*$/.test(w2);
              if (cont) { i++; continue; }
            }
            break;
          } else if (l2.trim() && /^\s{2,}/.test(l2) && items.length) {
            items[items.length - 1].text += " " + l2.trim();
            i++;
          } else { break; }
        }
        var deepest = items.some(function (it) { return it.indent >= 2; });
        var body = items.map(function (it) {
          var tag = (deepest && it.indent >= 2) ? '<li class="md-li-sub">' : "<li>";
          var html = tag + inline(it.text);
          if (it.blocks.length) {
            html += it.blocks.map(function (idx) {
              return '<div class="math-block in-list">' + (blocks[Number(idx)] || "") + "</div>";
            }).join("");
          }
          return html + "</li>";
        }).join("");
        out.push("<" + (ordered ? "ol" : "ul") + ' class="md-list">' + body +
                 "</" + (ordered ? "ol" : "ul") + ">");
        continue;
      }

      // ---- 普通段落：连续非空行合并（遇到块级公式占位符／表格分隔行也要断开）
      var para = [];
      while (i < lines.length && lines[i].trim() &&
             !BADGE.test(lines[i]) && !isFence(lines[i]) &&
             !/^\s*#{1,6}\s/.test(lines[i]) &&
             !/^\s*>\s?/.test(lines[i]) && !/^\s*[-*+]\s+/.test(lines[i]) &&
             !/^(\s*)(\d+)[.、)]\s+/.test(lines[i])) {
        para.push(lines[i]);
        i++;
      }
      if (!para.length) { i++; continue; }
      // 段落内换行：用纯 ASCII 占位符拼接，等 inline() 渲染完再回填 <br>。
      // 不能直接 inline(para.join("<br>"))：inline 内部会先转义，<br> 会变成 &lt;br&gt;。
      var body = inline(para.join("@~LB~@")).split("@~LB~@").join("<br>");
      // 若某行以【段落名】开头，标签与后续文字之间补一个换行，
      // 避免 <br> 被塞进 <span class="sec-tag"> 内部把标签撑变形。
      body = body.replace(/<span class="sec-tag">([^<]*)<\/span><br>/g,
                          '<span class="sec-tag">$1</span><br>');
      out.push('<p class="md-p">' + body + "</p>");
    }

    return out.join("");
  }

  global.renderRichText = renderRichText;
  global.convertMath = convertMath;
})(window);
