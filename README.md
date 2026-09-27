# crb-agent — 教室借用的下游 agent

> **上游**：[`yuque-agent`](https://github.com/Aalas1111/nju-yuque-agent) 把社员在语雀写的申请
> 判定、结构化成 `plan.json`；这个项目**把它变成学校系统里的借用申请**，
> 并让你在一个网页上和它对话。
>
> 分工见上游的 [`docs/handoff.md`](https://github.com/Aalas1111/nju-yuque-agent/blob/main/docs/handoff.md)：
> **上游不碰学校系统**（它没有学校登录态），所以「这间教室到底借不借得到」
> 只有这里在提交那一刻才知道。

![version](https://img.shields.io/badge/version-0.0.0-orange)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 它是什么

一个**单页 agent 界面 + 一条流式 agent loop**，能力边界由工具注册表画死（`src/crb_agent/tools.py`）：

| 能力 | 工具 | 依赖 |
|---|---|---|
| 自检登录态 / 学期 | `crb_status` | `crb doctor` |
| 查空闲教室 | `crb_free_rooms` | `crb free` |
| 校区 / 教学楼字典 | `crb_campus` / `crb_buildings` | `crb campus` / `crb buildings` |
| 查**已提交**的申请及状态 | `crb_list_borrows` | `crb borrow list` |
| 读语雀侧清单 | `read_plan` / `list_outbox` | 直接读 outbox 里的产物 |
| 刷新语雀侧清单 | `yqa_refresh_plan` | `yqa export-plan` |
| 批量出方案 / 存草稿 / 提交 | `crb_plan` | `crb plan [--save\|--submit]` |
| 单条提交 / 撤回 / 删除 / 修改 | `crb_borrow_action` | `crb borrow …` |

界面按**时间顺序**把一轮里的东西摊开画：**思考过程 → 工具调用卡 → 最终回复**，
全部流式。这一层没有魔法——服务端把事件按发生顺序发出来，前端照着顺序追加
（见 `src/crb_agent/events.py` 的说明与 `web/static/feed.js`）。

---

## 快速开始

```bash
uv sync

# 1. 三个必须的配置（见 docs/deploy.md §3）
export CRBA_KEY='你的访问密钥'
export DEEPSEEK_API_KEY='…'          # 或 CRBA_LLM_KEY
export YQA_REPO='<group>/<repo>'     # 语雀知识库，如 ghxd00_jsjysq

# 2. 自检
uv run crba doctor

# 3. 起服务
uv run crba serve                     # → http://127.0.0.1:8788/agent?key=…
```

打开 `http://127.0.0.1:8788/agent?key=<密钥>`：

* 密钥错了、或者只打开 `/agent` 不带密钥 → 只显示「密钥错误」；
* 密钥对了但学校登录态失效 → 自动跳到扫码页，用**南京大学 APP**扫一下，
  确认后自动回到 agent 界面；
* 密钥对了、登录态也在 → 直接进界面。

> 用 `?key=` 打开之后会立刻把 URL 里的密钥换成 Cookie 再重定向——
> 密钥待在地址栏里就会进浏览器历史、进截图。

---

## 登录态（扫码怎么做到不需要浏览器）

`crb login` 在本机弹一个有头浏览器让人扫码。服务器上没有浏览器，也不该有；
而且**「跳转到南大统一认证页」这条路根本走不通**：CAS 的 `CASTGC` /
`MOD_AUTH_CAS` 是下发到扫码那个浏览器的 HttpOnly Cookie，用户扫完码，
票据在他手上，我们的服务器什么都拿不到。

所以反过来做：**二维码由服务器自己取下来显示**（`GET /authserver/qrCode/getToken`
→ `getCode` → 轮询 `getStatus.htl`）。谁取的码，谁就是「那个浏览器」——
用户扫的就是这一张，登录态自然下到我们的 cookie jar 里。
细节与实测记录见 [`src/crb_agent/njuqr.py`](src/crb_agent/njuqr.py) 的文件头。

命令行版本（网页进不去时用）：`uv run crba auth` —— 它打印二维码图片地址，
任何一台能上网的设备打开都能扫。

---

## 规矩

* **默认只存草稿**（`crb plan` 的 `mode=save`）；`submit` 要用户明确要求才做。
* **去重**：`plan.json` 装的是一个周期内**所有**申请，必然包含已提交过的。
  `crb plan` 出方案时会把清单与「我的申请」逐条比对，时间重叠的标成
  `duplicate` 并跳过——**这不是失败，是「上周已经交过了」**。
* **不编事实**：接口没返回的信息就说没查到，绝不自造一间教室填回去。
* 写提示词的规矩与「判断归 LLM、程序不越界」的纪律，照搬上游
  [`docs/principles.md`](https://github.com/Aalas1111/nju-yuque-agent/blob/main/docs/principles.md)：
  工具注册表是粗粒度的能力边界（有 / 没有），语义判断只写在提示词里。

---

## 开发

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest                    # 不联网、不碰生产机的登录态
uv run crba doctor
uv run crba serve
```

* 前端**没有构建步骤**：`src/crb_agent/web/` 下的 HTML/CSS/ES module 就是产物。
* 提示词在 `src/crb_agent/prompts/`，改动它要一并想清楚正例/反例（见上游 principles §6）。

## 部署

见 [`docs/deploy.md`](docs/deploy.md)。服务器上是一个 systemd 单元
（`deploy/crb-agent.service`），改单元要同步文档那份。

## License

[MIT](LICENSE) © 2026 NOVA Contributors
