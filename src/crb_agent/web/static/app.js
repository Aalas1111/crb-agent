/* 界面控制器：左侧会话、中间对话、底部输入。
 *
 * 状态只有一个来源——服务端的留痕。前端不自己攒「历史」，切换会话就是
 * 重新拉一次 `/api/sessions/<id>`，然后喂给同一个 Feed 重放一遍。
 */

import { getJson, postJson, deleteJson, streamMessage } from "./api.js";
import { Feed, FeedView, clock } from "./feed.js";

const $ = (id) => document.getElementById(id);

const state = {
  sessionId: null,
  feed: new Feed(),
  running: false,
  stream: null,
  stopped: false,
};

const view = new FeedView($("feed-inner"));

/* ------------------------------------------------------------------ 侧栏 */
async function loadStatus() {
  const dot = $("status-dot");
  const text = $("status-text");
  try {
    const status = await getJson("/api/status");
    dot.className = `dot ${status.auth_ok ? "ok" : "bad"}`;
    if (status.auth_ok) {
      text.textContent = `${status.term || "学期未知"} · ${status.model}`;
      text.parentElement.onclick = null;
      return;
    }
    // 「没登录」和「出口被拦」要分开说 —— 后者的出路不是扫码。
    if (status.kind === "waf_blocked") {
      text.textContent = "学校风控拦了服务器出口（扫码没用）—— 点这里看原因";
    } else {
      text.textContent = "登录态失效，点这里重新登录";
    }
    text.parentElement.onclick = () => {
      location.href = status.kind === "waf_blocked" ? "/agent" : "/agent/auth";
    };
  } catch (error) {
    dot.className = "dot bad";
    text.textContent = String(error.message || error);
  }
}

function relative(seconds) {
  if (!seconds) return "";
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return "刚刚";
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  if (delta < 86400 * 7) return `${Math.floor(delta / 86400)} 天前`;
  const date = new Date(seconds * 1000);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

async function loadSessions() {
  const list = $("session-list");
  try {
    const { sessions } = await getJson("/api/sessions");
    list.textContent = "";
    if (!sessions.length) {
      list.appendChild(el("div", "session-group", "还没有会话"));
      return;
    }
    list.appendChild(el("div", "session-group", "历史会话"));
    for (const session of sessions) {
      const item = el("button", "session-item");
      item.type = "button";
      item.classList.toggle("active", session.id === state.sessionId);
      item.appendChild(el("span", null, session.title || "（未命名会话）"));
      const meta = el("span", "meta");
      meta.textContent = `${relative(session.updated_at)} · ${session.turns} 轮`;
      item.appendChild(meta);
      item.title = session.title || "";
      item.addEventListener("click", () => openSession(session.id));
      list.appendChild(item);
    }
  } catch (error) {
    list.textContent = "";
    list.appendChild(el("div", "session-group", String(error.message || error)));
  }
}

/* ------------------------------------------------------------------ 会话 */
async function openSession(sessionId) {
  if (state.running) return;
  state.sessionId = sessionId;
  const { feed } = await getJson(`/api/sessions/${sessionId}`);
  replay(feed);
  await loadSessions();
}

function replay(records) {
  state.feed = new Feed();
  for (const record of records) {
    if (record.role === "user") state.feed.addUser(record.content, record.at);
    else if (record.role === "run_start") state.feed.addRunStart(record.run_id);
    else if (record.role === "event") state.feed.applyEvent(record.event);
    else if (record.role === "run_end") state.feed.addRunEnd(record.status);
  }
  view.setFeed(state.feed.items);
  syncEmptyState();
  scrollToBottom(true);
}

/** 开场那几张卡片只在**真的一轮都没有**的时候出现。
 *  谁改了 feed 谁就得调它——否则打开历史会话时，卡片会盖在对话上。 */
function syncEmptyState() {
  const empty = state.feed.items.length === 0;
  $("empty-state").classList.toggle("hidden", !empty);
}

async function newSession() {
  if (state.running) return;
  const { id } = await postJson("/api/sessions", {});
  state.sessionId = id;
  state.feed = new Feed();
  view.clear();
  syncEmptyState();
  await loadSessions();
  $("composer-input").focus();
}

/* ------------------------------------------------------------------ 渲染 */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function nearBottom() {
  const feed = $("feed");
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
}

function scrollToBottom(force = false) {
  const feed = $("feed");
  if (force || nearBottom()) {
    requestAnimationFrame(() => {
      feed.scrollTop = feed.scrollHeight;
    });
  }
}

/* ------------------------------------------------------------------ 发送 */
async function send() {
  const input = $("composer-input");
  const text = input.value.trim();
  if (!text || state.running) return;
  if (!state.sessionId) await newSession();

  const sessionId = state.sessionId;
  input.value = "";
  autoGrow();

  const userItem = state.feed.addUser(text);
  view.update(userItem);
  syncEmptyState();
  scrollToBottom(true);

  setRunning(true);
  state.stream = streamMessage(sessionId, text, (name, payload) => {
    if (name === "done") return; // done 的收尾放在 finally 里统一做
    const changed = state.feed.applyEvent(payload);
    for (const item of changed) view.update(item);
    scrollToBottom();
  });

  try {
    await state.stream.done;
  } catch (error) {
    if (String(error?.name) !== "AbortError") {
      const item = {
        id: `err${Date.now()}`,
        kind: "error",
        message: String(error.message || error),
      };
      state.feed.items.push(item);
      view.update(item);
    }
  } finally {
    // 用户按了停止：这一轮的收尾得在**本地**画出来。服务端确实会把状态记成
    // cancelled，但那条事件发不回来了——连接就是被我们断掉的。
    const stopped = state.stopped;
    state.stopped = false;
    state.stream = null;
    setRunning(false);
    state.feed.addRunEnd(stopped ? "cancelled" : null);
    view.setFeed(state.feed.items);
    syncEmptyState();
    scrollToBottom();
    void loadSessions();
  }
}

function setRunning(running) {
  state.running = running;
  $("btn-send").classList.toggle("hidden", running);
  $("btn-stop").classList.toggle("hidden", !running);
  $("composer-input").disabled = running;
  if (!running) $("composer-input").focus();
}

function autoGrow() {
  const input = $("composer-input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

/* ------------------------------------------------------------------ 启动 */
function boot() {
  const input = $("composer-input");
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void send();
    }
  });
  $("btn-send").addEventListener("click", () => void send());
  $("btn-stop").addEventListener("click", () => {
    if (!state.stream) return;
    state.stopped = true;
    state.stream.abort();
  });
  $("new-session").addEventListener("click", () => void newSession());

  // 开场那几张卡片：点一下就把话填进输入框，省得用户自己想怎么开口。
  for (const card of document.querySelectorAll(".intro-card")) {
    card.addEventListener("click", () => {
      input.value = card.dataset.prompt || "";
      autoGrow();
      input.focus();
    });
  }

  $("empty-state").classList.remove("hidden");
  syncEmptyState();
  void loadStatus();
  void loadSessions();
}

document.addEventListener("DOMContentLoaded", boot);

export { send, openSession, newSession };
export { clock };
