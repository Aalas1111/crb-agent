/* 和服务端说话。
 *
 * 发消息用 fetch + ReadableStream 自己读 SSE，而不是 EventSource：
 * EventSource 只能 GET（我们要 POST 一个 JSON body），也不能中途 abort。
 * `: ping` 这种注释行（服务端在工具跑很久时发的心跳）在这里被丢掉。
 */

const BASE = "/agent";

async function readError(response) {
  try {
    const payload = await response.json();
    if (payload && payload.error) return payload.error;
  } catch {
    /* 不是 JSON 就走下面的兜底 */
  }
  return `请求失败（HTTP ${response.status}）`;
}

export async function getJson(path) {
  const response = await fetch(BASE + path, { headers: { Accept: "application/json" } });
  if (response.status === 403) throw new Error("密钥错误");
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

export async function postJson(path, body) {
  const response = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (response.status === 403) throw new Error("密钥错误");
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

export async function deleteJson(path) {
  const response = await fetch(BASE + path, { method: "DELETE" });
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

/**
 * 发一条消息，边收边交给 onEvent。
 *
 * 返回 ``{ abort, done }``：``abort()`` 给「停止」用（断开连接，服务端据此
 * 停下 agent）；``done`` 在这一轮真的结束时 resolve/reject，调用方 await 它
 * 做收尾（摘掉「正在流」的标记、刷新侧栏标题）。
 */
export function streamMessage(sessionId, text, onEvent) {
  const controller = new AbortController();

  const done = (async () => {
    const response = await fetch(`${BASE}/api/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ text }),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(await readError(response));
    if (!response.body) throw new Error("服务端没有返回流");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const handleBlock = (block) => {
      let name = "message";
      const dataLines = [];
      for (const line of block.split("\n")) {
        if (!line || line.startsWith(":")) continue; // 空行与心跳注释
        const colon = line.indexOf(":");
        if (colon < 0) continue;
        const field = line.slice(0, colon);
        let value = line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") name = value;
        else if (field === "data") dataLines.push(value);
      }
      if (!dataLines.length) return;
      try {
        onEvent(name, JSON.parse(dataLines.join("\n")));
      } catch {
        /* 半条 JSON（理论上不该出现）——跳过，不要让整个流断掉 */
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";
      for (const block of blocks) handleBlock(block);
    }
    if (buffer.trim()) handleBlock(buffer);
  })();

  return { abort: () => controller.abort(), done };
}
