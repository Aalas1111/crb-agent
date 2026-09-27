# 方案（**已实现**）：把审批结果取回来并分发

> 状态：**已按本方案实现并上线**（2026-09-27）。
>
> * 本仓库：`src/crb_agent/notify.py` + `crba notify-poll / notify-once / notify-show`
>   + `deploy/crb-agent-notify.service`；工具 `approval_status`。
> * 上游 `yuque-agent`：`approvaldoc.py` + `yqa refresh-approval`
>   + 它被加进 `ignore_doc_titles`；根目录顺序插在《Agent 通知》与活跃周期目录之间。
> * **文档名《审批结果》**（负责人定）。
>
> ⚠️ **当前出口下它查不到东西**：学校按出口 IP 拦机房 IP（见
> [`deploy.md`](deploy.md) §0），所以生产机上的轮询会一直「跳过这一轮」。
> 代码路径是通的（本地与 CI 全绿），**配好干净出口后自动开始工作**。
>
> 下面是设计说明（实现与它一致；有出入的地方以代码与 commit 为准）。
>
> 需求（负责人原话）：*审批通过之后，怎么把结果返回给服务器这边进行分发通知。*
> 初步想法：auth 有效时轮询南大官网的教室申请列表，状态有变动就维护一份
> 「审批结果」文档（`nova.classroom-borrow-notification.v1`）。

---

## 0. 结论表（含本版更新）

| 问题 | 结论 | 谁定的 |
|---|---|---|
| 谁来做轮询 | **本项目（crb-agent）**。只有这一侧有学校登录态与 `crb` | 我，§2 |
| 结果落哪 | **① 语雀知识库一篇文档**（给人看 / 长期留存）**② `<outbox>/approval/` 下的文件**（给机器读，qqbot 未来用） | **负责人** |
| 文档形态 | 账本只追加 + 对外文档每轮**重生** | 负责人 |
| 认不出的兜底 | **`unmatched.json`** | **负责人** |
| 怎么关联 SQBH ↔ 语雀申请 | 按（日期 + 节次 + 标题）匹配；正确解是 `applicationId`，见 §6 | 负责人选前者 |
| `approved_unassigned` | **不存在这种情况**（负责人确认）→ 不设中间态 | **负责人** |
| 轮询间隔 | 10 分钟（带抖动），可配 | 我 |
| 进程 | 独立 systemd 单元；账本**唯一**写者 | 我 |

---

## 1. 为什么要做

现在这条链路是**单向**的：

```
语雀文档 → yqa 判定 → plan.json → crb 提交 → 学校审核 → ？？？
```

最后一段是断的：**社员问「我那个教室批了吗」，没有任何人知道答案。**
只能人工登录办事大厅一页页翻。这份方案就是把最后一段接上。

---

## 2. 为什么轮询放在本项目，而不是 yqa

负责人提到「crba 和 yqa 似乎都可以」。三条硬约束把答案定死了：

1. **yqa 没有学校登录态，而且这是它刻意守住的边界。**
   上游 `docs/handoff.md` §1.1 明确写着：「它**没有**登录南大办事大厅的能力」。
   把轮询塞进 yqa，等于让一个只读语雀的程序开始持有学校凭证 —— 那条边界是
   它的安全落点，不该破。
2. **「审批结果」这个数据只存在于学校系统里**，而只有这一侧能查
   （`crb borrow list`）。数据在哪一侧产生，就该在哪一侧取。
3. **`crb` 已经把这件难事做完了**：`crb borrow list --json` 会带回
   `SHZT` / `SHZT_DISPLAY` / `SHBZ` / `SHYJ` / `FJ` / `JASMC` 等字段。
   轮询要写的新代码只有「比对 + 留痕 + 出文档」。

**但「写进语雀」这一步归 yqa** —— 因为它管知识库的结构、持有语雀写 token，
而且它**已经有**「程序维护一篇文档」的成熟模式（《Agent 通知》，
`src/yuque_agent/noticedoc.py`）。详见 §8。

分工因此是：

```
crb-agent：查学校 → 比对 → 写 outbox 文件（不碰任何姓名手机号之外的东西）
yqa      ：读 outbox 文件 → 重建语雀里那篇《教室借用审批结果》
qqbot    ：未来读 outbox 文件发通知（本轮不做，负责人已说明）
```

---

## 3. 轮询

```
每 10 分钟（±60s 抖动）：
  1. crb_status —— 登录态不可用就跳过这一轮（不是错误，别刷日志）
  2. crb borrow list --json（当前学期）
  3. 与账本比对 → 新结束的申请 → 记账 → 重生 outbox 文件 → 通知 yqa 刷新语雀文档
```

* **登录态失效不算故障**：跳过，日志留一行，等负责人扫码（`/agent/auth`）。
  这就是负责人说的「如果 crb 内的 auth 有效」。
* **只读**：轮询永远不写学校系统，只调 `borrow list`。这一点写进代码注释，
  免得后人顺手加个「自动重试提交」。
* 间隔可配 `CRBA_NOTIFY_INTERVAL`。

---

## 4. 数据：账本与对外文档

### 4.1 只追加的账本（内部）

`<outbox>/approval/ledger.jsonl`，一行一条，**只追加，永不重写**：

```jsonc
{"sqbh": "6ded436268ee478cb4b03e3f0b9d2788",
 "first_seen_ended": "2026-09-27T10:25:30+08:00",
 "outcome": "rejected",
 "snapshot": { /* 那一轮的原始记录，原样存 */ }}
```

`schemaVersion` / `applicationId` / `sourceDoc` **不存进账本** —— 它们是重生
对外文档时现算的。账本只记事实：「这个 SQBH 在什么时候第一次被看到『已结束』，
结束成什么样」。

`snapshot` 是唯一能事后复盘的东西（字段名会变），也是我们**目前**用来确认
§5 判定规则的地方。

### 4.2 对外文档（每轮重生）

写两处，内容同源：

| 落点 | 给谁 | 形态 |
|---|---|---|
| `<outbox>/approval/notifications.json` | 机器（qqbot 未来） | 下面的 JSON |
| 语雀知识库《教室借用审批结果》 | 人 / 长期留存 | 同一份内容渲染成 Markdown 表 |

```jsonc
{
  "schemaVersion": "nova.classroom-borrow-notification.v1",
  "batchId": "notification-2026-09-27T10:30:00.000Z",
  "generatedAt": "2026-09-27T10:30:00.000Z",
  "notifications": [ /* 全部已结束的结果，按 detectedAt 排序 */ ]
}
```

* 里面**永远是全部**已结束的结果（满足「追加所有已经审批结束的结果」），
  但文件本身是重生的 —— 因为 `notificationId` 稳定（§7），下游按 id 去重，
  所以**重跑、重启、手抖都不会重复通知**。
* 与《Agent 通知》同一个思路：**内容是账本的纯函数**，不是一份被反复追加的流水。

`<outbox>/approval/unmatched.json`：认不出的（见 §6）单独一份，等人处理。
它**不进** `notifications.json` —— 宁可让人多看一眼，不可让下游收到猜出来的东西。

---

## 5. 字段判定（**已按真实样本写定**）

样本来自负责人转来的同学记录（一条**审核不通过**的）：

```jsonc
"SQBH": "6ded436268ee478cb4b03e3f0b9d2788",   "SHZT": "-68",
"SHZT_DISPLAY": "学生工作处审核不通过",        "SHBZ": "不通过",
"SHYJ": null,          // 审核意见
"FJ": null, "JASMC": null,                      // 教室（未分配）
"KSJC_DISPLAY": "第4节(11:10-12:00)",  "JSJC_DISPLAY": "第4节(11:10-12:00)",
"JYRXM": "谷和平",     "JYDWDM_DISPLAY": "电子科学与工程学院",
"JYYTMS": "GHP",       "JASJYLXDM_DISPLAY": "学生工作处",
"XXXQDM_DISPLAY": "仙林校区",  "KSRQ": "2026-09-10",  "SQRQ": "2026-09-08 15:17:23.0"
```

映射到通知字段：

| 通知字段 | 取自 | 备注 |
|---|---|---|
| `sqbh` | `SQBH` | 32 位十六进制 |
| `type` | `SHBZ` 优先，`SHZT_DISPLAY` 关键词兜底 | 见下 |
| `result.feedback` | `SHYJ` | 样本里是 `null` → 通知里给 `""` |
| `result.actualRooms` | `FJ` / `JASMC` | 通过时才有值 |
| `activity.slotStart` / `slotEnd` | `KSJC_DISPLAY` / `JSJC_DISPLAY` | **格式正好就是 schema 要的** `第N节(HH:MM-HH:MM)` |
| `activity.date` | `KSRQ` | |
| `activity.campus` | `XXXQDM_DISPLAY` | 已经是「仙林校区」这种写法 |
| `activity.organizer` | `JYRXM` | ⚠️ 是**人名**（见 §9 的隐私说明） |
| `activity.title` | `JYYTMS` | 剥掉「（意向：xxx）」 |
| `detectedAt` | 本地首次看到的时刻 | 不是学校的时间 |
| `applicationId` / `sourceDoc` | 关联得来 | §6 |

**判定规则**（照抄插件 `recordLifecycle()` 的关键词法，它在生产里跑着）：

```
"00" 或含「暂存」            → 草稿（不算结束，不通知）
含 撤回|不通过|驳回|拒绝|退回|取消 → rejected
含 待|审核                    → 进行中（不算结束）
含 通过|提交|完成             → approved   ← 需实测：通过时 SHBZ 到底写什么
其它                          → unmatched（**不猜**）
```

**待实测的两点**（样本只有「不通过」一种）：

1. **通过时 `SHBZ` / `SHZT_DISPLAY` 到底长什么样。** `crb` README 记的是
   `SHZT=99` = 已通过，但那是从别处整理的，没有样本佐证。
   负责人手里目前也没有已通过的记录 → **等第一条真通过时，`snapshot` 会把它
   记下来**，那时把规则补实。这正是账本留 `snapshot` 的用途。
2. `SHZT` 的**完整码表**。样本给了 `-68`（不通过）、README 说 `99`（通过），
   中间还有 `65`（待审核）、`00`（草稿）、`1`（已撤回）。
   所以判定用中文关键词 + `SHBZ` 比用码表稳 —— 码表是学校内部的，会变。

字段名**只出现在一处**（`notify.FIELD_MAP` + `notify.classify()`），
实测后改进那张表即可，逻辑不动。

### 5.1 「通过但没教室」怎么办

负责人说这种情况应该不存在。所以不设 `approved_unassigned` 中间态；
但**万一出现**（`SHBZ` 是通过而 `FJ`/`JASMC` 都空），归到 `unmatched.json`
并注明「已通过但读不到教室」—— 不静默丢，也不编一间教室出来。

---

## 6. 关联：SQBH ↔ applicationId

学校记录里**没有**语雀的任何 id，只有 `JYYTMS`（用途描述）、日期、节次。
而通知里要带 `applicationId` 与 `sourceDoc`。所以要建映射。

**当前做法（按负责人选定）**：匹配键 =（日期 + 节次 + 标题）。

1. 读 `<outbox>/plan.json` 的 `activities`，那里有溯源键
   `_application_id` / `_doc_id`（上游 `handoff.md` §2.2 的约定）；
2. 周期翻转后 plan.json 会被搬进 `outbox/archive/<周期>/`，所以还要**扫归档**；
3. 标题对齐：`crb` 提交时把 `JYYTMS` 写成 `"<title>（意向：<room>）"`，
   匹配时剥掉「（意向：…）」再比；
4. `sourceDoc.repo` = `YQA_REPO` 的 repo 段；`sourceDoc.dir` = 找到该 activity
   的那份 plan.json 的 `cycle`（**不是当前周期** —— 可借窗口 9 天会跨周期）。

**匹配不上的** → `unmatched.json`，带原始记录与「为什么认不出」。
**绝不猜**：猜错会把 A 活动的结果安到 B 头上。

> **更好但未采纳**：用 `applicationId` 当稳定去重键。同学的浏览器插件正是这么做的
> ——它明确要求「缺少 `application_id` 无法稳定去重」。我们可以在提交时
> 记一条 `SQBH ↔ applicationId` 的映射，之后关联就是精确查表，不需要模糊匹配。
> 代价：**已经提交的历史申请没有这条映射**（只能走上面的匹配兜底），
> 而且要新增一处落盘。见 §11 的问题 4。

---

## 7. `notificationId` 的稳定生成

同一个结果重复生成必须得到同一个 id，否则下游去重失效：

```
notificationId = "notify-" + sha1(f"{sqbh}|{outcome}|{实际教室列表}").hexdigest()[:8]
```

* **不含时间戳** —— 含了就每轮都变，去重立刻失效（最容易犯的错）。
* **含「实际教室列表」**：教室后来变了（或从无到有）就是一条**新**通知。
* 例：`notify-a13f52c8` 这种形状，与负责人给的示例一致。

`type` / `result.status` 映射（按负责人给的语义）：

| 学校侧 | `type` | `result.status` | `actualRooms` | `feedback` |
|---|---|---|---|---|
| 通过 | `"approved"` | `"approved_assigned"` | `[FJ/JASMC]` | `""` |
| 退回 | `"rejected"` | `"rejected"` | `[]` | `SHYJ` |

---

## 8. 语雀那一份：**yqa 侧的改动**（本版新增，这是负责人上次点出的问题）

负责人定的是「文档挂在语雀知识库里」。好消息是 **yqa 已经把路铺好了**，
不用新造机制：

1. **它已经有「程序维护一篇文档」的成熟模式**：《Agent 通知》由
   `src/yuque_agent/noticedoc.py` 维护，内容是「本周期通知」目录的**纯函数**、
   每轮重建。我们的《教室借用审批结果》照这个模子加一个 `approvaldoc.py` 即可。
2. **它已经有「忽略某些文档」的机制**：`config.py` 里
   `ignore_doc_titles: tuple[str, ...] = (NOTICE_TITLE, GUIDE_TITLE)` ——
   这正是负责人说的「yqa 归档时要忽略『审批结果』文档」：
   **把标题加进这个元组**，轮询就不会去判定它、归档也不会碰它。
3. `cli.py` 里 `_SYSTEM_DOC_TITLES`（永不删）也要加一项。

所以要改的是**四处，都很小**：

| # | 改动 | 为什么 |
|---|---|---|
| Y1 | `config.py`：新增 `APPROVAL_TITLE = "教室借用审批结果"`，并把它加进 `ignore_doc_titles` 与 `cli._SYSTEM_DOC_TITLES` | 不让 yqa 把这篇文档当申请去判定，也别在清理时删掉它 |
| Y2 | 新增 `src/yuque_agent/approvaldoc.py`（照 `noticedoc.py`）：读 `<outbox>/approval/notifications.json` + `unmatched.json` → 重建那篇文档（Markdown 表） | 人能在语雀里直接看 |
| Y3 | 新增命令 `yqa refresh-approval` | 让既能被定时调用，也能被 crb-agent 在检测到变化时调用 |
| Y4 | `docs/design.md` 的「知识库结构」一节补一篇文档的位置 | 那是「程序产出的固定位置」之一，得写清楚 |

**触发方式**：crb-agent 轮询完，若账本有变化就调一次 `yqa refresh-approval`
（就像它已经在调 `yqa export-plan` 一样）。这样不需要给 yqa 加第二个常驻单元。

> **不采纳的替代方案**：让 crb-agent 自己用 `YQA_TOKEN` 写语雀。
> 那样 Y1 还是得改（yqa 仍会看到这篇文档），而且把「知识库结构」的知识
> 复制到了第二个仓库 —— 知识库现在有两处会写它，以后必然漂。

---

## 9. 系统设计

| 单元 | 类型 | 干什么 | 唯一写者 |
|---|---|---|---|
| `crb-agent.service` | 常驻（已存在） | 网页界面 + agent；**只读**账本 | 自己的 session 留痕 |
| `crb-agent-notify.service` | 常驻（新） | 轮询 → 比对 → 记账 → 重生 outbox 文件 → 调 `yqa refresh-approval` | `approval/ledger.jsonl` 等三份文件 |

新命令：

```bash
crba notify-poll     # 常驻轮询（给 systemd）
crba notify-once     # 只跑一轮并打印发现了什么（人工排查；同一把 flock，不写文件）
crba notify-show     # 打印账本概要（给 agent 工具用）
```

给 agent 加一个只读工具 `approval_status`：读账本，回答「这个周期哪些批了、
哪些退了、哪些还没动静」——「查审批进度」这类问题就不必每次都去打学校接口。

**隐私**：通知里带 `JYRXM`（借用人姓名，schema 要求 `organizer`）。
所以语雀那篇文档与 outbox 文件都含人名。**不含手机号**（`JYRDH` 不收）。
这条要写进 `docs/deploy.md` 的暴露面一节。

**跨周期**：审批结果**不按周期归档**（可借窗口 9 天会跨周）。账本是累计的，
文档里带上每条的活动日期，不靠目录归属区分。

---

## 10. 分阶段

| 阶段 | 做什么 | 依赖 |
|---|---|---|
| **P1** | 账本 + `notify-once` + 单元测试（用样本当夹具） | 无（字段样本已到手） |
| **P2** | `notify-poll` 单元 + `approval_status` 工具 | P1 |
| **P3** | 对外文档重生 + `unmatched.json` + 语雀那一份 | **Y1~Y4（要动 yqa）** |
| **P4** | 补实「通过」的判定规则（第一条真通过到来之后） | 现实世界里出现第一条通过 |
| **P5**（可选） | qqbot 读 outbox 发通知 | 不属本轮 |

---

## 11. 请评审的点（本版收敛到 3 个）

1. **Y1~Y4 要不要动 yqa？** 我的建议是：**要**，而且照《Agent 通知》的模子做
   （§8 那四处改动都很小）。如果你更希望「别动 yqa」，替代方案是
   crb-agent 自己写语雀 + yqa 只加一个「忽略」——但那样知识库结构会分两处维护。
2. **语雀那篇文档的标题叫什么？** 我暂定《教室借用审批结果》。
   它会成为知识库里的固定节点（和《指导文档（必读）》《Agent 通知》并列），
   位置顺序也要定。
3. **`applicationId` 精确关联要不要做？**（§6 末尾）做了之后，去重与关联都不再
   依赖标题匹配，而且和同学的插件用**同一个键**。代价是要为新提交的申请落一条
   `SQBH ↔ applicationId` 映射，历史申请仍走匹配兜底。

---

## 12. 一个顺带发现（不在本方案内，供你判断）

对照同学的浏览器插件时发现：**两边给 `JSJYLXDM` 填的值可能不是同一套字典**
（插件填 `09 团学活动`，crb 填 `02 学生社团管理部`；`01` 的含义在两边还冲突）。
详情见 [`compat-browser-plugin.md`](compat-browser-plugin.md) §3。

在确认之前，**别让两种工具交替提交同一批申请**——学校侧看到的申请类型可能不一致，
而这类差异在「审核不通过」之前不会有任何提示。
登录态恢复后调一次 `cxjsjylx.do` 就能定案（`crb` 已封装）。
