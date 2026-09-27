# 部署：这台机器上的真相

> 服务器地址与账号**不进仓库**（由项目负责人单独交接）。
> 本文只写「什么在哪里、怎么验、出事了怎么找」。
> 动手前先读 [`principles.md`](principles.md) 与仓库根的 [`AGENTS.md`](../AGENTS.md)。

## 0. ⚠️ 出口 IP 约束（2026-09-27 上机实测，**这条决定服务能不能用**）

学校对办事大厅的**接口调用**按出口 IP 拦：

| 出口 | 结果 |
|---|---|
| 机房 / 数据中心 IP（阿里云等） | **403** |
| 家宽 / 校园网 IP | 正常 |

**证据**（单变量对照，2026-09-27）：同一份登录态、同一份 `crb`、同样的请求头 ——

```
开发机（家宽）调用  crb doctor  → ok: true，拿到学期 2026-2027-1 与单位 400760
生产机（阿里云）调用 crb doctor  → 退出码 3，403
```

而在生产机上：**打开页面是 200、调接口是 403**。所以**登录态是好的**，
被拦的是「接口调用」这一层。换个 UA、先用 httpx 预热页面、换 curl
（完全不同的 TLS 栈）都仍然是 403 —— 排除指纹类原因，只剩出口 IP。

**所以：扫码解决不了它。** 界面上把两者分开了（`crb_status` 的 `kind`）：

| `kind` | 界面行为 |
|---|---|
| `not_logged_in` | 跳到扫码页（这条扫码**有用**） |
| `waf_blocked` | 显示一个说明页并讲清出路（**`/agent/auth` 不挂在这条路上**） |

混成一句「登录态不可用」会让人对着解决不了的问题反复扫码 —— 实测踩过。

**它挡住的不止一件事**：提交 / 查询 / **审批结果轮询**都调学校接口，全受影响。
`crb-agent-notify.service` 的日志会一直显示
`跳过这一轮：取申请列表失败（退出码 3）（学校按出口 IP 拦）` —— 那是**预期的降级**，
不是故障（它不崩、不刷屏，等出口干净了自然就开始工作）。

**出路**（挑一条）：

1. **给服务配一个非机房的出口**。`httpx` 默认读 `HTTPS_PROXY`，所以在单元里加一行即可：
   ```
   Environment=HTTPS_PROXY=http://<你的家宽/校园网代理>
   ```
   注意这个代理要能到 `ehallapp.nju.edu.cn` 与 `authserver.nju.edu.cn`。
2. **把服务挪到干净的网络上**（家宽 / 校园网里的机器）。
3. **用同学的浏览器插件**：它跑在用户自己的浏览器里，出口就是用户的网络，
   天然不受这条限制（见 [`compat-browser-plugin.md`](compat-browser-plugin.md)）。

> 代理配置好之后，`docs/design.md` §5 的扫码流程也会跟着走同一个出口 ——
> 两边出口一致很重要，否则登录态与接口调用来自两个 IP，风控更容易起疑。

## 1. 目录布局

| 路径 | 是什么 |
|---|---|
| `/opt/crb-agent` | 代码检出（`origin` = 上游）。**只允许快进** |
| `/var/lib/crb-agent/workspace` | 会话留痕（`sessions/<id>.jsonl`）+ `ops.log` |
| `/var/lib/crb-agent/.uv-cache` | uv 缓存 |
| `/etc/systemd/system/crb-agent.service` | 生效的单元（权威副本在 `deploy/`）：Web 界面 |
| `/etc/systemd/system/crb-agent-notify.service` | 生效的单元：审批结果轮询（账本**唯一**的写者） |
| `/var/lib/yuque-agent/workspace/<repo>/outbox/approval/` | 审批结果的产物（账本 / 通知文档 / unmatched） |
| `/home/yuque/.crb/auth.json` | 学校登录态 ← **和 `crb` 用的是同一个文件** |
| `/home/yuque/.yuque/agent.env` | `DEEPSEEK_API_KEY`、`YQA_REPO`（复用 `yuque-agent` 那份） |
| `/home/yuque/.crb-agent/env` | 本项目自己的：只有 `CRBA_KEY` |
| `/var/lib/yuque-agent/workspace/` | 语雀侧的产出（只读）。`plan.json` 从这里取 |

**两个单元**：`crb-agent.service`（网页界面 + agent 循环）与 `crb-agent-notify.service`
（审批结果轮询）。后者单独一个进程是刻意的 —— Web 重启不该打断跟踪审批，
而且「谁是账本唯一的写者」要一眼可见。

## 2. 依赖

三样东西：

```bash
# 1) crb —— 教室借用 CLI（和语雀侧的 yuque-agent 各自独立）
#    服务器能连 GitHub 时直接装；装到 /home/yuque/.local/bin/crb
sudo -u yuque env HOME=/home/yuque uv tool install \
    "git+https://github.com/Aalas1111/NJU_Classroom_Booking"

# 2) yqa —— **不要再装一份**。它已经在 /opt/yuque-agent 里，
#    单元用 `uv run --no-sync --project /opt/yuque-agent yqa` 直接指过去，
#    这样两个服务共用同一个检出，版本不会漂。
sudo -u yuque env HOME=/home/yuque /usr/local/bin/uv run \
    --no-sync --project /opt/yuque-agent yqa version     # 验证能跑

# 3) 本项目
sudo -u yuque env HOME=/home/yuque /usr/local/bin/uv sync --project /opt/crb-agent
```

`crb` 落在 `/home/yuque/.local/bin`，单元里显式加了这条 PATH
（systemd 的默认 PATH 里没有它）。

> `crb login`（Playwright 那套）在生产机上**用不到** —— 登录态由本服务的
> 扫码流程维护（§6）。所以不需要装 `[login]` 额外依赖，也不需要在服务器上
> 装浏览器。

## 3. 凭证

| 变量 | 放哪 | 怎么来 |
|---|---|---|
| `CRBA_KEY` | `/home/yuque/.crb-agent/env` | **你自己定**一个长随机串。这是这个服务唯一的防线 |
| `DEEPSEEK_API_KEY` | `/home/yuque/.yuque/agent.env` | 已存在，**复用**，不新开一份 |
| `YQA_REPO` | `/home/yuque/.yuque/agent.env` | 已存在（`ghxd00_jsjysq` 所属的知识库）——**同一个变量**，本项目直接读它，不重复配 |
| 学校登录态 | `/home/yuque/.crb/auth.json` | 网页上扫码（见 §6），或 `crba auth` |

所以本项目自己的文件里**只放一件东西**：

```bash
sudo install -d -o yuque -g yuque -m 750 /home/yuque/.crb-agent
sudo tee /home/yuque/.crb-agent/env >/dev/null <<'EOF'
CRBA_KEY=<换成一个长随机串>
EOF
sudo chown yuque:yuque /home/yuque/.crb-agent/env
sudo chmod 600 /home/yuque/.crb-agent/env
```

> `CRBA_KEY` 是**必需的**，没配服务会拒绝启动并打印一句人话。
> 这是刻意的 fail-closed：界面上能提交借用申请、能花 LLM 的 token，
> 不该有「没配就裸奔」的中间状态。
>
> `YQA_REPO` 决定 `plan.json` 在哪（`<yuque_workspace>/<repo>/outbox/plan.json`）。
> 它和 `yuque-agent` 读的是**同一个变量**，所以天然一致，没有第二处要对。

## 4. 首次部署

```bash
# ① 在开发机上，先把代码送到生产机（服务器取不到 GitHub 时也走得通）
scp -r . <user>@<地址>:/tmp/crb-agent-src
ssh <user>@<地址> 'sudo mv /tmp/crb-agent-src /opt/crb-agent && sudo chown -R yuque:yuque /opt/crb-agent'

# ② 生产机上准备依赖与凭证（见 §2 §3），然后
ssh <user>@<地址> 'sudo /opt/crb-agent/scripts/deploy.sh'
```

`deploy.sh` 会：取 flock → 确认工作区干净 → 快进 → **在临时 HOME 里跑测试** →
对齐单元 → 重启 → 验收（含「不带密钥必须被拦」）→ 记 `ops.log`。

## 5. 日常更新

```bash
# 开发机侧：把当前 commit 送过去并部署（推荐）
scripts/sync-server.sh --server <user>@<地址>

# 或者：生产机自己能取到 GitHub 时，直接在机器上
sudo /opt/crb-agent/scripts/deploy.sh
```

① **这台机器到 GitHub 时通时断**（和 `yuque-agent` 遇到的是同一个网络）。
`deploy.sh` 取不到 origin 时会「按当前 HEAD 继续」——那等于悄悄部署旧 commit，
所以要看着日志里的那行 `⚠ 取不到 origin`。`sync-server.sh` 就是为这条路准备的：
先把 commit 送到生产检出，再调 `deploy.sh`。

### 测试**不要**在生产机上裸跑

```bash
# ❌ 不要这样
cd /opt/crb-agent && uv run pytest

# ✅ 要跑就走 deploy.sh（它把 HOME 关进临时目录）
sudo /opt/crb-agent/scripts/deploy.sh
```

理由与 `yuque-agent` 那次事故同源（见其 `AGENTS.md` §2.1）：测试一旦碰到
默认路径下的真凭证，代价是重扫一次码。我们的测试全部不联网、`crb`/`yqa`
都用替身（`tests/fake_cli.py`），但**闸门不是许可证**——照旧走沙箱。

## 6. 学校登录态（auth）怎么恢复

登录态失效时，打开界面会被自动送到扫码页：

```
http://<地址>:8788/agent?key=<CRBA_KEY>
        ↓ 登录态失效
http://<地址>:8788/agent/auth     ← 页面上就是二维码
```

用**南京大学 APP** 或微信扫一下、手机上点确认，页面自动跳回界面。
全程不需要浏览器、不需要密码，二维码是服务器自己取下来的（原理见
[`njuqr.py`](../src/crb_agent/njuqr.py) 的文件头）。

拿不到网页时，还有命令行版本：

```bash
sudo -u yuque HOME=/home/yuque /usr/local/bin/uv run --no-sync crba auth
```

它会打印二维码图片的地址，**任何一台能上网的设备**打开那个地址都能扫
（图片本身是 authserver 的公开 GET，而真正完成登录的是你手机上的确认）。

> 登录态能撑多久由学校策略决定，我们不做假设。失效就再扫一次。

## 7. 验收清单

```bash
systemctl is-active crb-agent.service            # active
journalctl -u crb-agent -n 20 --no-pager | grep "crba 已启动"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/healthz        # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/agent          # 403（不带密钥）
```

**接口真的通吗**（这一步才验得到 §0 那条约束，前面几条都验不到）：

```bash
sudo -u yuque env HOME=/home/yuque PATH=/home/yuque/.local/bin:/usr/local/bin:/usr/bin:/bin \
    crb doctor --json; echo "退出码=$?"
# 0  → 通
# 2  → 没有登录态，扫码即可
# 3  → 被风控拦（出口 IP 的问题，扫码没用）→ 回到 §0
```

再打开 `http://<地址>:8788/agent?key=…`：

1. 界面出现，左下角显示「学期 · 模型」而不是红点；
2. 发一句话，能看到**思考过程 → 工具卡 → 回复**按顺序流出来；
3. 左侧栏有历史会话，点进去内容完整（含思考块与工具卡）。

`ops.log` 里应多一行 `deploy`。

## 8. 出事了怎么办

1. **先留证据再动手**：`journalctl -u crb-agent -n 100`、`ops.log`、`git log`、
   `stat -c '%y %n' <文件>`（文件被谁何时改的，mtime 常常是唯一线索）。
2. **登录态失效**不是故障，按 §6 扫一次码即可 —— 别急着重启（重启不解决它）。
3. **端口冲突**：8787 是 `yuque-agent-plan` 的下载口。本服务用 8788，
   两者互不相干；`ss -ltnp | grep 878` 一眼能看出谁占了谁。
4. 结论写进提交信息或 `docs/`，**不要只留在聊天里**——下一个代理看不到聊天。

## 9. 回退

```bash
cd /opt/crb-agent
sudo git log --oneline -5                 # 找到上一个好 commit
sudo git checkout <commit>                # 只读地退回去（生产机禁止 rebase）
sudo systemctl restart crb-agent
```

注意 `deploy.sh` 只允许快进，所以「回退」等于临时 detached HEAD；
正式做法是发一个新的修复提交。

## 10. 暴露面（为什么可以绑 `0.0.0.0`）

单元里 `CRBA_HOST` 默认 `0.0.0.0`（监听所有网卡）。按 [`AGENTS.md`](../AGENTS.md) §2.2
的规矩，绑公网必须写清楚**暴露什么、靠什么鉴权、为什么接受**：

**暴露什么**

| 路径 | 需要密钥吗 | 内容 |
|---|---|---|
| `/agent`、`/agent/auth`、`/agent/static/*` | **要** | 界面、扫码页、前端静态文件 |
| `/agent/api/*` | **要** | 会话列表与内容、发消息（SSE）、登录态状态 |
| `/healthz` | 不要 | 只有 `ok` 两个字母 |
| `/` | 不要 | 302 到 `/agent` |

**靠什么鉴权**：一个共享密钥（`CRBA_KEY`）。两种带法——

* `?key=…`：**只用于第一次进门**。校验通过后立刻把 URL 里的 `key` 摘掉、
  换成 HttpOnly 的 `crba_session` Cookie 再重定向，所以密钥不会留在地址栏、
  浏览器历史、截图里；
* Cookie 的值是 `HMAC(CRBA_KEY, "crba-session-v1")` —— 确定（重启不掉线）、
  不可逆（拿到 Cookie 反推不出密钥）、换密钥即全体失效。
* 比较用 `hmac.compare_digest`（常数时间）。错密钥与没密钥**都只回「密钥错误」**，
  不透露这个页面上有什么。

**为什么接受这个暴露面**：这个页面能提交教室借用申请、能花 LLM 的 token，
所以不能像 `yuque-agent` 的下载口那样公开。但它也**不该只绑 127.0.0.1** ——
它要给人用，而服务器没有域名、只有明文 HTTP，绑回环等于只有 SSH 隧道能用。
于是选择是：**明文 HTTP + 一个共享密钥**，并且把「密钥不进地址栏」这条做掉。

**已知的残余风险**（写出来，不装作没有）：

* 明文 HTTP ⇒ 同一网络路径上的攻击者能看到 Cookie 与全部对话内容。
  没有域名、没有证书，这一条**无解**，只能靠「这是个内部小工具」来容忍。
  真要解决就得有域名 + HTTPS（那是另一件事）。
* 没有速率限制：拿到密钥的人可以随便打。密钥只发给自己人。
* **`plan.json` 里含借用人姓名与手机号**，agent 读它时会把这两样带进对话
  （LLM 上下文）。这**不是本项目引入的**——上游刻意把 `defaults` 内联进
  `plan.json`，并在自己的部署文档里标注了这是唯一的 PII 风险点。
  但要知道：**与 agent 的对话内容里会出现姓名与手机号**。
* 会话留痕（`/var/lib/crb-agent/workspace`）里存着完整对话，同样含上面那些信息。
  它是 `600`、归 `yuque`，但**不是加密的**。

**安全组**：放行 TCP `8788`，源留 `0.0.0.0/0`（否则外网进不来）。
**不要**为了这个服务去动 8787 的规则。
