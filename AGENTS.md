# AGENTS.md —— 在这个仓库 / 这台机器上干活的规矩

> 这里有些规矩是从**同一台机器**上的事故换来的（那次是 `yuque-agent` 的测试真的
> 删掉了扫码换来的凭证）。凡标了「事故」的，别当成风格偏好。
> 动手之前先读完本文件，再读 [`docs/deploy.md`](docs/deploy.md)。

## 0. 先读这几份

| 文件 | 它是什么 |
|---|---|
| [`docs/principles.md`](docs/principles.md) | **改代码前必读**：判断归 LLM，程序不越界 |
| [`docs/design.md`](docs/design.md) | 为什么长这样：agent loop / 事件契约 / 登录态 / 前端取舍 |
| [`docs/deploy.md`](docs/deploy.md) | 部署真相：目录布局、凭证、验收清单、**暴露面**（§10） |
| `AGENTS.md`（本文件） | 干活规矩：怎么改、怎么验、什么绝对不许做 |

上游是 [`yuque-agent`](https://github.com/Aalas1111/nju-yuque-agent)：
它把语雀的文档判定成 `plan.json`，**这个项目把 plan 变成学校系统里的申请**。
两边通过 `plan.json` 这个文件交接（契约在上游的 `docs/handoff.md`）。

## 1. 仓库与提交

* **这个仓库还没有上游。** 加 remote 的那天，请把这条规矩接上：
  上游 = 那个 `origin`；干活顺序 `git fetch` → 从 `origin/main` 开 feature 分支 →
  推到自己的 fork → 请上游合并。**服务器上永远不许 rebase / 手工 merge**
  （`/opt/crb-agent` 只允许快进，`deploy.sh` 会拦）。
* **不许在生产机（`/opt/crb-agent`）上写代码、直接 `git commit`。**
  同一个事故（上游的 `AGENTS.md` §1 记着）：有人在生产机上堆了 5 个提交，
  `main` 就此和上游分叉，那批改动没推 GitHub（没备份、没法 review），
  下次 `git pull` 直接被拒。正确做法：本地改 → 走 PR → `scripts/sync-server.sh` 上线。
* 收工前**跟踪的文件**必须干净（`git status --porcelain --untracked-files=no` 为空）。
  未跟踪文件（`.env`、草稿、临时产物）不用提交，但要收拾。
* 作者 = 真正写这段代码的人；提交者 = 把它放进仓库的人/代理。**代理提交时写明是代理执行。**
* 提交信息第一行说清「改了什么」，正文说「为什么 + 依据」（现象 / 日志 / 测试结果）。
* 大改动先跟人确认；别顺手重构别人的文件。

## 2. 前端改动必须在浏览器里点一遍

**这是这个项目的硬性要求，不是建议。** 界面是「事件按顺序摊开画」，
它的 bug 全是时序与重画的 bug，单测（pytest）一个都抓不到。实测抓到过的：

* 整表重画时思考块与正文**内容全丢**（骨架建了、没填内容）；
* 打开历史会话时开场卡片盖在对话上；
* 点「停止」后没有任何反馈（服务端的状态发不回来了——连接就是被客户端断掉的）。

所以改 `src/crb_agent/web/**` 或 `feed.js` 的事件处理之后，跑起来点一遍：

```bash
uv run crba serve        # 然后按 docs/deploy.md §7 的清单走
```

至少覆盖：新会话发一条带工具调用的消息、**打开历史会话**（回放路径）、
点一次「停止」。没有浏览器就明说没验，别默认它是对的。

## 3. 生产机上的铁律

### 3.1 不许在生产机上裸跑测试（事故）

* **事故**（上游记的）：在服务器上跑 `pytest`，里面一条 `logout --yes` 真的执行了，
  把扫码换来的凭证连备份一起删掉。**删一次 = 重扫一次码**，当天发生了两次。
* **闸门不是许可证**：测试优先在本地跑；必须在机器上跑时，走
  `scripts/deploy.sh`（它显式把 `HOME` 指向临时目录）。
* 新写的测试**不许**假设「默认路径下没有东西」，更不许真的调 `logout`：
  要覆盖「没有凭证」就用 `tmp_path` 显式指定路径。
* 本项目的测试**一个都不联网**：`crb` / `yqa` 用 `tests/fake_cli.py` 顶替，
  authserver 用 `httpx.MockTransport` 顶替。别往里加真请求。

### 3.2 常驻进程必须是仓库里的 systemd 单元

* 只有**两个**单元（权威副本在 `deploy/`，生效位置在 `/etc/systemd/system/`）：
  `crb-agent.service`（Web 界面）与 `crb-agent-notify.service`（审批结果轮询）。
  改单元 = 改 `deploy/*.service` → `install` 到 `/etc` → `daemon-reload`
  → 同步 `docs/deploy.md` 里那份。
* **审批结果的账本只有一个写者**：`crb-agent-notify.service`。别在 Web 侧加
  「点一下刷新账本」那种功能 —— 那会引入第二个写者。要手动跑就 `crba notify-once`
  （与常驻共用一把 flock）。
* **轮询只读学校系统**（只调 `borrow list`）。别顺手加「自动重试提交」。
* **不许** `nohup` / `setsid` / `&` 起常驻进程。实测踩过（上游）：有人手工起了
  一个轮询，和 systemd 里那个抢同一个工作区，两边互相覆盖状态。
* **端口**：这个服务用 **8788**。**8787 是 `yuque-agent-plan.service` 的下载口**
  （公开、无鉴权），别去占它，也别往那个服务里加路由。
* **新服务默认绑 `127.0.0.1`。** 要绑 `0.0.0.0` 必须先在 `docs/deploy.md` 里写清
  暴露什么、靠什么鉴权、为什么接受。本服务的例外就是这么写下来的（§10）。

### 3.3 一次只能有一个写者

* 所有对生产机的改动走 **`scripts/deploy.sh`**：取 `flock` 锁、只允许快进、
  在沙箱 `HOME` 里跑测试、对齐单元、重启、验收，最后把
  「谁 / 什么时候 / 哪个 commit」追加进 `/var/lib/crb-agent/ops.log`。
* 需要人肉操作时，先去 `ops.log` 记一笔「我要做什么、大概多久」。

## 4. 凭证（删一次 = 重扫一次码）

| 位置 | 作用 | 谁管 |
|---|---|---|
| `~/.crb/auth.json` | 学校登录态 | 网页扫码或 `crba auth` 写；**别手删手改** |
| `~/.yuque/agent.env` | `DEEPSEEK_API_KEY` | `yuque-agent` 的，本项目**只读复用** |
| `~/.crb-agent/env` | `CRBA_KEY`、`YQA_REPO` | 本项目自己的 |

* 密钥不落日志、不贴聊天、不进提交。要验证 LLM key 能不能用，用 `crba doctor`
  或打一发最小请求，**别把密钥打出来**。
* `CRBA_KEY` 没配时服务**拒绝启动**（fail closed）。别为了「先跑起来」给它一个默认值。

## 5. 交付前自检

1. `uv run ruff check . && uv run ruff format --check .`
2. `uv run pytest`（本地；在机器上则必须经由 `scripts/deploy.sh`）
3. 碰了 `src/crb_agent/prompts/**` → `uv run pytest tests/test_prompt_guard.py`
4. 碰了 `src/crb_agent/web/**` → **在浏览器里点一遍**（见 §2）
5. 碰了 `deploy/*.service` → 同步 `docs/deploy.md` §1 与 §10 里那两份说明
6. 部署后：单元 `systemctl is-active`、日志里有 `crba 已启动`、
   **不带密钥访问 `/agent` 要 403**、`ops.log` 多一行

## 6. 出事了怎么办

1. **先留证据再动手**：`journalctl -u crb-agent`、`ops.log`、`git log`、
   `stat -c '%y %n' <文件>`（文件被谁何时改的，mtime 常常是唯一线索）。
2. 登录态失效**不是故障**——按 [`docs/deploy.md`](docs/deploy.md) §6 扫一次码。
   别急着重启（重启不解决它）。
3. 结论写进提交信息或 `docs/`，**不要只留在聊天里**——下一个代理看不到聊天。
