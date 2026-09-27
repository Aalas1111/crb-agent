# 设计：一条 agent loop 与它的事件契约

> 这份文件回答「为什么长这样」。要改代码之前的定位问题，看这里；
> 部署与凭证看 [`deploy.md`](deploy.md)；纪律看 [`principles.md`](principles.md)。

## 0. 一张图

```
浏览器 ──SSE──▶ /agent/api/sessions/<id>/messages
                    │
                    ▼
              Agent.run()  ──▶ tools.Toolbox ──▶ crb / yqa（subprocess，无 shell）
                    │                              │
                    │                              └─▶ 学校系统 / 语雀产出
                    ▼
            events.Event（有序）──▶ 存 session.jsonl ──▶ 前端 feed
```

一次「轮」的骨架（`agent.py`）：

```
收到用户消息
  │  模型流式输出： reasoning_delta… assistant_delta…
  ├─ 模型要调工具 → tool_call → 执行 → tool_result → 回到上面再来一轮
  └─ 模型不再调工具 → assistant_message → run 结束
```

## 1. 时间顺序即真相（这个项目最要紧的一条）

**服务端把所有事件按发生顺序交给 `emit`，中间不缓存、不重排、不合并。**
时间线是 `agent.py` 的产物；前端的职责只是把它画出来。所以「思考块在哪、
工具卡在哪」不需要两边协商——**数组顺序就是时间顺序**。

前端 `web/static/feed.js` 只做两件「就地更新」的事：

1. `reasoning_delta` / `assistant_delta` 追加到**当前**块上（流式合并）；
2. `tool_result` 按 `id` 找到那张卡**原地**补上结果。

配套的细节，每一个都是踩过才知道的：

| 做法 | 不这么做会怎样 |
|---|---|
| `tool_call` 一来就**断开**「当前正在流的块」的指针 | 「调工具前的一句铺垫」与「工具结果之后的正文」被拼成同一段 |
| 每个打断思考的事件都调 `_closeReasoning()` | 思考块一直转圈/脉冲，永远不退 |
| 失败提示（`— 这一轮没有跑完 —`）**只加一次** | 回放时 `run_end` 与 `done` 都触发，历史会话里冒出两条 |
| `FeedView.append()` 建完骨架立刻调 `refresh()` | 整表重画（打开历史会话）时思考块与正文**内容全丢**，只剩空壳 |
| 开场卡片由 `syncEmptyState()` 按 feed 长度统一控制 | 打开历史会话时卡片盖在对话上 |

最后两条是浏览器里点出来的，单测抓不到——所以**改了前端一定要真的开一次页面**。

## 2. 事件契约（`events.py`）

```
reasoning_delta  {content}                         思考的增量
assistant_delta  {content}                         正文的增量
assistant_message{content}                         正文的最终态
tool_call        {id, name, input, risk}            开始调工具
tool_result      {id, name, status, summary, risk,
                  data, elapsed_ms}                 工具结果（按 id 对上上面那条）
error            {message}
done             {run_id, status}                   completed / failed / cancelled
```

`ToolResult` 有**两副面孔**，别混：

* `to_wire()` → 给界面看的（`summary` 是卡片上那一行字）；
* `for_model()` → 回灌给模型的 JSON 文本，**永远带 `status`**（失败也是事实）。

SSE 的事件名单独一行（`event: reasoning_delta`），前端用 `addEventListener`
按名分发，比在一堆 JSON 里 switch 干净。

## 3. 留痕：实时与回放共用一条路径（`store.py`）

一个会话 = 一个只追加的 `<workspace>/sessions/<id>.jsonl`：

```
meta / user / assistant / tool / run_start / event* / run_end
```

关键取舍：**`event` 那一行存的就是前端收到的那条 SSE 载荷**。于是
「回放」和「实时」在前端走的是同一个 `applyEvent`——两条路径一旦分家，
历史会话和现场会话迟早长得不一样，而这种不一致只在翻旧账时才暴露。

`assistant` / `tool` 那两类记录是**给模型看的上下文**（`build_messages()`
只认它们），`event` 是**给界面看的**。让模型读自己的事件流等于让它读日记。

服务重启不会丢历史：会话全在磁盘上，`GET /agent/api/sessions` 直接扫目录。

## 4. 工具层：能力边界（`tools.py`）

* 每个工具 = `{name, description, parameters, risk, handler}`；`manifest()`
  转成 OpenAI function-calling 格式，并把「用法契约」拼进 description
  ——模型选工具时读的是**那里**，不是提示词。
* **永远没有 shell**：参数逐字段校验后以**参数数组**交给 `subprocess`
  （`crb_bin` 允许写成 `uv tool run crb` 这种带参数形式，由 `shlex` 还原）。
* 风险只有两档：`read` / `dangerous`。`dangerous` 的卡片会打「写操作」标。
* 唯一的路径入口是 `_resolve_within()`：读语雀产出时把相对路径钉死在 outbox 里，
  `../` 与绝对路径都挡掉（`plan.defaults.json` 就在隔壁，里面是姓名与手机号）。
* `PYTHONIOENCODING=utf-8` 是显式钉的：crb / yqa 都是 Python，在 Windows 上
  默认按 cp936 输出中文，我们按 UTF-8 解码会得到**只在开发机上出现**的乱码。

**去重**（`crb plan` 的语义，`tools.py` 只负责转述）：

* `status="duplicate"` = 与**已有申请**时间重叠 → 已经交过了，跳过。
  这就是「按已提交的申请给 plan 去重」，`crb` 自带，不必在 agent 里重做。
* `note` 里「与同一批次里的…时间重叠」= **本批内部**两条活动同时段 →
  **照样排教室**，只报事实。合并与否是判断，归 LLM。

## 5. 登录态：为什么是「服务器自己取二维码」（`njuqr.py`）

把用户**重定向到南大统一认证页**这条路是走不通的：CAS 的 `CASTGC` /
`MOD_AUTH_CAS` 是 HttpOnly，下发到**扫码的那个浏览器**里。用户扫完，票据在
他手上，我们的服务器什么也拿不到。（把 `service` 指向自己的回调也不行：
ticket 绑在 service 上，ehallapp 会拒掉为别的 service 签发的票。）

所以反过来：**二维码由服务器自己取下来显示**。谁取的码，谁就是「那个浏览器」，
登录态自然下到我们的 cookie jar 里。流程（2026-09-27 实测）：

```
GET  /authserver/login?service=<ehallapp jsjy 入口>   拿 execution / lt
GET  /authserver/qrCode/getToken                      拿 uuid
GET  /authserver/qrCode/getCode?uuid=…                400×400 PNG
GET  /authserver/qrCode/getStatus.htl?uuid=…          轮询 0/2/1/3
POST /authserver/login?display=qrLogin&service=…      换 CASTGC，302 到 ehallapp
                                                      → ehallapp 下发 MOD_AUTH_CAS
→ 写成 crb 认的 storage_state 落到 ~/.crb/auth.json
```

全程不接触用户密码。代价是依赖学校这套 QR 端点，所以
`tests/test_njuqr.py` 用**真实登录页的片段**当夹具（`tests/fixtures/`）来盯解析器。

## 6. 访问控制

见 [`deploy.md`](deploy.md) §10（暴露面、密钥怎么带、已知残余风险）。
要点：`?key=` 只用于进门，进门即换成 `HMAC(CRBA_KEY, …)` 的 HttpOnly Cookie
并重定向到干净 URL。

## 7. 前端为什么没有构建步骤

`web/` 下的 HTML/CSS/ES module **就是产物**。没有 npm、没有 bundler、
没有 CDN 依赖（服务器没有域名，也不保证外网可达）。markdown 渲染自己写了
一个**先转义再做语法**的小实现（`markdown.js`），因为它渲染的是 LLM 的输出，
不该把 XSS 交给运气。

代价是拿不到生态里的组件；换来的是「改一行刷新即见」，以及部署时
`deploy.sh` 里不需要任何前端步骤。
