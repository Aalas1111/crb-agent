#!/usr/bin/env bash
# 把生产机更新到上游 —— 这是**唯一**被允许的更新方式（见 AGENTS.md §2.3）。
#
# 它做的事，按顺序：
#   1. 取 flock（同一时刻只允许一个写者：人 / 代理 / 定时任务都算）
#   2. 确认工作区干净
#   3. 快进到 origin/main（服务器上禁止 rebase / 手工 merge）
#   4. 在**临时 HOME** 里跑测试（AGENTS.md §2.1：测试碰过生产凭证）
#   5. 把 deploy/*.service 与 /etc/systemd/system/ 对齐 + daemon-reload
#   6. 重启单元，验收（单元 active + 密钥门真的在拦 + 日志里有启动行）
#   7. 把「谁 / 什么时候 / 哪个 commit」追加进 /var/lib/crb-agent/ops.log
#
# 用法（注意 `./`：sudo 的 PATH 里通常没有当前目录，写 `sudo scripts/deploy.sh`
# 会报 command not found —— 上游实测过）：
#   sudo ./scripts/deploy.sh
#
# 首次引导：这个脚本本身要先在机器上（它自己会 fetch，但得先有它）。
# 见 docs/deploy.md §5②。
#
# 任何一步失败就停下，不改任何东西 —— 部分部署比不部署更难查。

set -euo pipefail

REPO="${REPO:-/opt/crb-agent}"
STATE="${STATE:-/var/lib/crb-agent}"
LOG="$STATE/ops.log"
LOCK="$STATE/.deploy.lock"
WHO="${WHO:-$(whoami)@$(hostname -s)}"

# 两个单元：Web 界面（密钥门）+ 审批结果轮询（账本是它唯一的写者）。
WEB_UNIT=crb-agent.service
NOTIFY_UNIT=crb-agent-notify.service
UNITS=("$WEB_UNIT" "$NOTIFY_UNIT")

# ⚠️ 所有 git 操作都**以仓库所有者（yuque）的身份**跑。
# 这个脚本是 root 执行的，但检出归 yuque、部署密钥也在 /home/yuque/.ssh ——
# root 直接跑 git 会先撞 dubious ownership，再撞 `Host key verification failed`，
# 于是 fetch 失败、退回「按当前 HEAD 继续」，**部署的其实是旧 commit**
# （首次部署实测踩到：测试跑的是旧代码，很难看出来）。
GIT=(sudo -u yuque git -C "$REPO")
PORT="${CRBA_PORT:-8788}"

say() { printf '\n== %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "需要 root（要 install 单元、restart systemd）"

say "取部署锁（$LOCK）"
mkdir -p "$STATE"
# $STATE 必须归 yuque：这个脚本以 root 跑，不 chown 的话目录是 root:root 755，
# 而服务以 yuque 起、要在里面建 workspace/ —— 一启动就
# `PermissionError: [Errno 13] Permission denied: /var/lib/crb-agent/workspace`
# （首次部署实测踩到，服务每隔 15s 重启一次）。
chown yuque:yuque "$STATE"
# uv 的缓存目录：单元里用 UV_CACHE_DIR 指到这里（别去写 yuque 用户的 ~/.cache）。
mkdir -p "$STATE/.uv-cache"
chown yuque:yuque "$STATE/.uv-cache"
exec 9>"$LOCK"
flock -n 9 || die "另一个部署正在进行（$LOCK 被占用）。等它结束再来，别抢同一个工作区。"

cd "$REPO"
[ -f deploy/crb-agent.service ] || die "$REPO 看起来不是这个项目的检出"

say "工作区必须干净（跟踪的文件）"
if [ -n "$("${GIT[@]}" status --porcelain --untracked-files=no)" ]; then
  "${GIT[@]}" status --short --untracked-files=no >&2
  die "有未提交的改动：先提交或 stash。部署只走「上游里有的 commit」。"
fi
UNTRACKED="$("${GIT[@]}" ls-files --others --exclude-standard)"
if [ -n "$UNTRACKED" ]; then
  # 未跟踪的常常是本地/机密文件（.env、草稿），不该逼人提交；但也不能装作没看见。
  printf '⚠ 有未跟踪文件（不影响本次部署，但请收拾）：\n%s\n' "$UNTRACKED"
fi

say "快进到上游"
if timeout 45 "${GIT[@]}" fetch origin; then
  "${GIT[@]}" merge --ff-only origin/main
else
  # 这台机器到 GitHub 时通时不通（见 docs/deploy.md §5①）：取不到就按当前 HEAD
  # 部署，但要让人看见这件事，别假装同步过了。
  echo "⚠ 取不到 origin（网络问题？）—— 跳过快进，按当前 HEAD 继续"
fi
COMMIT="$("${GIT[@]}" rev-parse --short HEAD)"
SUBJECT="$("${GIT[@]}" log -1 --pretty=%s)"

say "测试（HOME 关进临时目录）"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
HOME="$SANDBOX" PYTHONPATH=src .venv/bin/python -m pytest -q

say "systemd 单元与仓库对齐"
for unit in "${UNITS[@]}"; do
  install -m 644 "deploy/$unit" /etc/systemd/system/
  if [ -d "/etc/systemd/system/${unit}.d" ]; then
    echo "⚠ $unit 还有 drop-in：$(ls "/etc/systemd/system/${unit}.d")"
    echo "  它会覆盖单元里的设置——确认后删掉"
  fi
done
systemctl daemon-reload

say "重启单元"
for unit in "${UNITS[@]}"; do
  systemctl enable "$unit" >/dev/null
  systemctl restart "$unit"
done
sleep 8

say "验收"
RC=0

for unit in "${UNITS[@]}"; do
  state="$(systemctl is-active "$unit" || true)"
  printf '%-26s %s\n' "$unit" "$state"
  [ "$state" = "active" ] || RC=1
done

# 启动行：证明它真的跑到了「开始干活」那一步，而不只是进程还在。
# ⚠️ 不要写成 `journalctl … | grep -q "…"`：`set -o pipefail` 下，
# grep 一找到就退出 → journalctl 吃到 SIGPIPE(141) → 管道整体非 0 → 条件判为假，
# 于是间歇性误报「启动行没有」（日志量大的时候才触发，实测踩到）。用命令替换把
# 输出接住再判空，`|| true` 兜住退出码。
WEB_START="$(journalctl -u "$WEB_UNIT" --since "-3min" --no-pager 2>/dev/null | grep "crba 已启动" || true)"
if [ -n "$WEB_START" ]; then
  printf '%-26s %s\n' "web 启动行" "有"
else
  printf '%-26s %s\n' "web 启动行" "没有（journalctl -u $WEB_UNIT -n 50 看原因）"
  RC=1
fi
NOTIFY_START="$(journalctl -u "$NOTIFY_UNIT" --since "-3min" --no-pager 2>/dev/null | grep "notify 轮询开始" || true)"
if [ -n "$NOTIFY_START" ]; then
  printf '%-26s %s\n' "notify 启动行" "有"
else
  printf '%-26s %s\n' "notify 启动行" "没有（journalctl -u $NOTIFY_UNIT -n 50 看原因）"
  RC=1
fi

# 密钥门：不带密钥必须被拦。这是这个服务唯一的防线，所以要验它真的在拦，
# 而不是「进程活着」就算过。
if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/agent")" = "403" ]; then
  printf '%-26s %s\n' "密钥门" "在拦（不带密钥 → 403）"
else
  printf '%-26s %s\n' "密钥门" "「不带密钥」没被拦！立刻检查（journalctl -u $WEB_UNIT -n 50）"
  RC=1
fi
if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz")" = "200" ]; then
  printf '%-26s %s\n' "/healthz" "ok"
else
  printf '%-26s %s\n' "/healthz" "没反应"
  RC=1
fi

TRACEBACK="$(journalctl -u "$WEB_UNIT" --since "-3min" --no-pager 2>/dev/null | grep "Traceback" || true)"
if [ -n "$TRACEBACK" ]; then
  echo "⚠ web 日志里有 Traceback —— 看 journalctl -u $WEB_UNIT -n 80" >&2
  RC=1
fi
NOTIFY_TB="$(journalctl -u "$NOTIFY_UNIT" --since "-3min" --no-pager 2>/dev/null | grep "Traceback" || true)"
if [ -n "$NOTIFY_TB" ]; then
  echo "⚠ notify 日志里有 Traceback —— 看 journalctl -u $NOTIFY_UNIT -n 80" >&2
  RC=1
fi

printf '%s deploy %s %s %s\n' "$(date -Is)" "$WHO" "$COMMIT" "$SUBJECT" | tee -a "$LOG"
if [ "$RC" = "0" ]; then
  say "✓ 部署完成：$COMMIT $SUBJECT"
else
  say "✗ 部署不完整：$COMMIT $SUBJECT（上面有没过的检查）"
fi
exit "$RC"
