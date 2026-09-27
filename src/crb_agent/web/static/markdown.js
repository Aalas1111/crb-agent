/* markdown 渲染：**先转义，再做语法**。
 *
 * 为什么不引第三方库：这台服务器没有域名、也不一定有外网可达的 CDN，而
 * 「渲染 LLM 的输出」正是不该把 XSS 交给运气的地方。这里的规矩很简单 ——
 * 先把 & < > " 全部转义，之后所有的替换都只在**已经安全**的文本上做标记，
 * 于是模型写什么都不可能变成可执行的 HTML。（唯一的例外是链接的 href，
 * 见 safeUrl：只放行 http/https，挡住 javascript: 之类。）
 *
 * 支持的是提示词里要求模型用的那几种：标题、列表、表格、围栏代码、引用、
 * 分割线、粗体/斜体/行内代码/链接。够用就行。
 */

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

function safeUrl(url) {
  const trimmed = url.trim();
  if (/^https?:\/\//i.test(trimmed) || /^#/.test(trimmed) || /^\//.test(trimmed)) {
    return trimmed;
  }
  return null;
}

/* 行内语法。输入必须已经过 escapeHtml。 */
function inline(text) {
  let out = text;
  // 行内代码先做，并把它「抠出来」：不然代码里的 * 会被当成斜体。
  const codes = [];
  out = out.replace(/`([^`]+)`/g, (_m, body) => {
    codes.push(body);
    return `\u0000${codes.length - 1}\u0000`;
  });
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label, url) => {
    const href = safeUrl(url);
    return href
      ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : match;
  });
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/\u0000(\d+)\u0000/g, (_m, idx) => `<code>${codes[Number(idx)]}</code>`);
  return out;
}

const reFence = /^\s*(```|~~~)\s*([\w+-]*)\s*$/;
const reHeading = /^(#{1,4})\s+(.*)$/;
const reHr = /^\s*([-*_])(\s*\1){2,}\s*$/;
const reUl = /^\s*[-*+]\s+(.*)$/;
const reOl = /^\s*(\d+)[.)]\s+(.*)$/;
const reQuote = /^\s*&gt;\s?(.*)$/;
const reTableSep = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function splitRow(line) {
  return line
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

export function renderMarkdown(source) {
  const lines = escapeHtml(source).split(/\r?\n/);
  const html = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    const fence = line.match(reFence);
    if (fence) {
      const closer = new RegExp(`^\\s*${fence[1]}\\s*$`);
      const body = [];
      index += 1;
      while (index < lines.length && !closer.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1;
      html.push(`<pre><code>${body.join("\n")}</code></pre>`);
      continue;
    }

    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (reHr.test(line)) {
      html.push("<hr>");
      index += 1;
      continue;
    }

    const heading = line.match(reHeading);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`);
      index += 1;
      continue;
    }

    // 表格：本行是表头，下一行是 |---|---| 才算数。
    if (line.includes("|") && index + 1 < lines.length && reTableSep.test(lines[index + 1])) {
      const head = splitRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(splitRow(lines[index]));
        index += 1;
      }
      const thead = head.map((cell) => `<th>${inline(cell)}</th>`).join("");
      const tbody = rows
        .map((cells) => `<tr>${cells.map((cell) => `<td>${inline(cell)}</td>`).join("")}</tr>`)
        .join("");
      html.push(`<table><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`);
      continue;
    }

    if (reQuote.test(line)) {
      const body = [];
      while (index < lines.length && reQuote.test(lines[index])) {
        body.push(lines[index].match(reQuote)[1]);
        index += 1;
      }
      html.push(`<blockquote>${body.map(inline).join("<br>")}</blockquote>`);
      continue;
    }

    const isUl = (text) => reUl.test(text);
    const isOl = (text) => reOl.test(text);
    if (isUl(line) || isOl(line)) {
      const ordered = isOl(line);
      const items = [];
      while (
        index < lines.length &&
        lines[index].trim() &&
        (ordered ? isOl(lines[index]) : isUl(lines[index]))
      ) {
        const matched = ordered ? lines[index].match(reOl) : lines[index].match(reUl);
        items.push(`<li>${inline(matched[ordered ? 2 : 1])}</li>`);
        index += 1;
      }
      html.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }

    // 段落：一直吃到空行、或遇到下一个块级语法。
    const paragraph = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !reFence.test(lines[index]) &&
      !reHeading.test(lines[index]) &&
      !reHr.test(lines[index]) &&
      !reQuote.test(lines[index]) &&
      !reUl.test(lines[index]) &&
      !reOl.test(lines[index])
    ) {
      paragraph.push(lines[index]);
      index += 1;
    }
    html.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
  }

  return html.join("\n");
}
