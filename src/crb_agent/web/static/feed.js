/* 对话流：把事件按时间顺序摊成一个列表，并画出来。
 *
 * 这个文件是整个界面最要紧的地方，规矩只有一条：
 *
 *   **事件按到达顺序追加；已经在列表里的块只做「就地更新」，绝不换位置。**
 *
 * 于是「思考 → 调工具 → 再思考 → 回复」天然就是从上往下的时间线，不需要
 * 任何重排逻辑。两个具体的做法：
 *
 * 1. `reasoning_delta` / `assistant_delta` 追加到**当前**那块上（流式合并）；
 * 2. `tool_result` 按 id 找到那张卡**原地**补上结果。
 *
 * 还有一个容易漏的细节（SilverTavern 也踩过）：`tool_call` 一来就把
 * 「当前正在流的块」指针清空。否则「调工具前的一句铺垫」和「工具结果之后的
 * 正文」会被拼成同一段，时间顺序就假了。
 *
 * 历史回放走的是同一个 `applyEvent`——服务端存的 `event` 记录就是实时收到
 * 的那条载荷。两条路径共用一段代码，才不会越长越不一样。
 */

import { renderMarkdown } from "./markdown.js";

let seq = 0;
const nextId = () => `it${++seq}`;

/* ------------------------------------------------------------------ 数据 */
export class Feed {
  constructor() {
    this.items = [];
    this.reasoningId = null;
    this.assistantId = null;
    this.runId = null;
    // 一开始就当「没有正在跑的轮次」，免得空会话收尾时冒出一条中断提示。
    this.runClosed = true;
  }

  reset() {
    this.items = [];
    this.reasoningId = null;
    this.assistantId = null;
    this.runId = null;
    this.runClosed = true;
  }

  addUser(content, at) {
    this.items.push({ id: nextId(), kind: "user", content, at: at ?? nowSec() });
    return this.items[this.items.length - 1];
  }

  addNote(text) {
    this.items.push({ id: nextId(), kind: "note", text, at: nowSec() });
    return this.items[this.items.length - 1];
  }

  addRunStart(runId) {
    this.runId = runId;
    this.runClosed = false;
    this._closeReasoning();
    this.assistantId = null;
  }

  addRunEnd(status) {
    const last = this.items[this.items.length - 1];
    if (last && last.kind === "assistant") last.streaming = false;
    this._closeReasoning();
    // 失败/中断的提示只加一次。回放时 run_end 记录与 done 事件都会走到这儿，
    // 不去重的话历史会话里会冒出两条「这一轮没有跑完」。
    if (status && status !== "completed" && !this.runClosed) {
      this.addNote(status === "cancelled" ? "— 已停止 —" : "— 这一轮没有跑完 —");
    }
    this.runClosed = true;
    this.reasoningId = null;
    this.assistantId = null;
  }

  /** 收尾当前思考块：摘掉「还在想」的脉冲，并把指针清空。
   *  每个会打断思考的事件（工具调用、最终回复、错误、轮次结束）都要调它——
   *  漏一个，那块思考就会一直闪下去。 */
  _closeReasoning() {
    if (!this.reasoningId) return;
    const item = this._find(this.reasoningId);
    if (item) item.pending = false;
    this.reasoningId = null;
  }

  /* 事件 → 列表。返回「哪几条变了」，供视图做增量刷新。 */
  applyEvent(event) {
    const at = Number(event.at) || nowSec();
    switch (event.type) {
      case "run_start":
        // 一轮的开始。**只在留痕里有、SSE 里没有的话，现场与回放就会分家**：
        // 回放时失败/中断的提示正常出现，现场时它压根不出现（实测踩过）。
        this.addRunStart(event.run_id ?? "");
        return [];
      case "reasoning_delta": {
        if (!event.content) return [];
        let item = this._find(this.reasoningId);
        if (!item) {
          item = { id: nextId(), kind: "reasoning", content: "", at, pending: true };
          this.items.push(item);
          this.reasoningId = item.id;
        }
        item.content += event.content;
        return [item];
      }
      case "assistant_delta": {
        if (!event.content) return [];
        let item = this._find(this.assistantId);
        if (!item) {
          item = { id: nextId(), kind: "assistant", content: "", at, streaming: true };
          this.items.push(item);
          this.assistantId = item.id;
        }
        item.content += event.content;
        return [item];
      }
      case "assistant_message": {
        let item = this._find(this.assistantId);
        if (item) {
          item.content = event.content ?? item.content;
          item.streaming = false;
        } else {
          item = {
            id: nextId(),
            kind: "assistant",
            content: event.content ?? "",
            at,
            streaming: false,
          };
          this.items.push(item);
        }
        this._closeReasoning();
        this.assistantId = null;
        return [item];
      }
      case "tool_call": {
        // 见文件头注释：这里必须把「正在流的块」断开。
        this._closeReasoning();
        this.assistantId = null;
        const item = {
          id: nextId(),
          kind: "tool",
          callId: event.id,
          name: event.name,
          input: event.input,
          risk: event.risk,
          running: true,
          result: null,
          elapsedMs: null,
          at,
        };
        this.items.push(item);
        return [item];
      }
      case "tool_result": {
        const item = this.items.find((entry) => entry.kind === "tool" && entry.callId === event.id);
        if (!item) return [];
        item.running = false;
        item.elapsedMs = event.elapsed_ms ?? null;
        item.result = {
          status: event.status,
          summary: event.summary,
          risk: event.risk,
          data: event.data,
        };
        return [item];
      }
      case "error": {
        this._closeReasoning();
        this.assistantId = null;
        const item = { id: nextId(), kind: "error", message: event.message ?? "未知错误", at };
        this.items.push(item);
        return [item];
      }
      case "done":
        this.addRunEnd(event.status);
        return this.items.slice(-1);
      default:
        return [];
    }
  }

  _find(id) {
    return id ? this.items.find((item) => item.id === id) : undefined;
  }
}

/* ------------------------------------------------------------------ 视图 */
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const NS = "http://www.w3.org/2000/svg";

function icon(path, size = 14) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const node = document.createElementNS(NS, "path");
  node.setAttribute("d", path);
  svg.appendChild(node);
  return svg;
}

const CHEVRON = "M9 18l6-6-6-6";

function clock(sec) {
  if (!sec) return "";
  const date = new Date(sec * 1000);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function nowSec() {
  return Date.now() / 1000;
}

function pretty(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export class FeedView {
  constructor(container) {
    this.container = container;
    this.nodes = new Map();
    this.onToggleTool = null;
  }

  clear() {
    this.container.textContent = "";
    this.nodes.clear();
  }

  setFeed(items) {
    this.clear();
    for (const item of items) this.append(item);
  }

  append(item) {
    const node = this.build(item);
    this.nodes.set(item.id, { node, item });
    this.container.appendChild(node);
    // build 只搭骨架，内容由 refresh 填。凡是「进列表」的路径（实时新增、
    // 历史回放、整表重画）都必须走这里，否则骨架会空着——思考块与正文的
    // 内容就是这么丢的。
    this.refresh({ node, item });
    return node;
  }

  /** 条目内容变了：只刷新内容，不动位置。 */
  update(item) {
    const entry = this.nodes.get(item.id);
    if (!entry) return this.append(item);
    entry.item = item;
    this.refresh(entry);
    return entry.node;
  }

  build(item) {
    switch (item.kind) {
      case "user": {
        const wrap = el("div", "item item-user");
        wrap.appendChild(el("div", "bubble", item.content));
        return wrap;
      }
      case "reasoning": {
        const wrap = el("div", "item item-reasoning");
        wrap.appendChild(el("div", "label", "思考过程"));
        const body = el("div", "body");
        wrap.appendChild(body);
        wrap._body = body;
        return wrap;
      }
      case "assistant": {
        const wrap = el("div", "item item-assistant");
        const body = el("div", "body md");
        wrap.appendChild(body);
        wrap._body = body;
        return wrap;
      }
      case "error": {
        return el("div", "item item-error", item.message);
      }
      case "note": {
        return el("div", "item item-note", item.text);
      }
      case "tool": {
        return this.buildTool(item);
      }
      default:
        return el("div", "item");
    }
  }

  buildTool(item) {
    const wrap = el("div", "item tool");
    const head = el("div", "tool-head");
    head.appendChild(el("div", "tool-name", item.name));
    const summary = el("div", "tool-summary");
    head.appendChild(summary);
    const flag = el("span", "tool-flag hidden", "写操作");
    head.appendChild(flag);
    const spin = el("div", "tool-spin");
    head.appendChild(spin);
    head.appendChild(icon(CHEVRON, 13)).setAttribute("class", "tool-chevron");
    wrap.appendChild(head);

    const detail = el("div", "tool-detail");
    wrap.appendChild(detail);

    wrap._summary = summary;
    wrap._flag = flag;
    wrap._spin = spin;
    wrap._detail = detail;
    wrap._open = false;

    head.addEventListener("click", () => {
      wrap._open = !wrap._open;
      wrap.classList.toggle("open", wrap._open);
      if (wrap._open) this.fillDetail(wrap, item);
    });
    return wrap;
  }

  fillDetail(wrap, item) {
    if (wrap._filled) return;
    wrap._filled = true;
    const detail = wrap._detail;
    detail.appendChild(el("h4", null, "参数"));
    detail.appendChild(el("pre", null, pretty(item.input) || "（无）"));
    if (item.result) {
      detail.appendChild(el("h4", null, "结果"));
      const body = item.result.data === undefined ? "" : pretty(item.result.data);
      detail.appendChild(el("pre", null, body || item.result.summary || "（无）"));
    }
  }

  refresh(entry) {
    const { node, item } = entry;
    if (item.kind === "reasoning") {
      node._body.textContent = item.content;
      node.classList.toggle("pending", Boolean(item.pending));
      return;
    }
    if (item.kind === "assistant") {
      node._body.innerHTML = renderMarkdown(item.content);
      node.classList.toggle("streaming", Boolean(item.streaming));
      return;
    }
    if (item.kind === "tool") {
      const result = item.result;
      node.classList.toggle("running", Boolean(item.running));
      node.classList.toggle("error", Boolean(result && result.status === "error"));
      node.classList.toggle("dangerous", item.risk === "dangerous");
      node._flag.classList.toggle("hidden", item.risk !== "dangerous");
      node._spin.classList.toggle("hidden", !item.running);

      if (item.running) {
        node._summary.textContent = "执行中…";
      } else if (result) {
        const seconds = item.elapsedMs != null ? ` · ${(item.elapsedMs / 1000).toFixed(1)}s` : "";
        node._summary.textContent = `${result.summary}${seconds}`;
      }
      if (node._open) {
        node._filled = false;
        node._detail.textContent = "";
        this.fillDetail(node, item);
      }
    }
  }
}

export { clock };
