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

UNIT=crb-agent.service
PORT="${CRBA_PORT:-8788}"

say() { printf '\n== %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "需要 root（要 install 单元、restart systemd）"

say "取部署锁（$LOCK）"
mkdir -p "$STATE"
# uv 的缓存目录：单元里用 UV_CACHE_DIR 指到这里（别去写 yuque 用户的 ~/.cache）。
mkdir -p "$STATE/.uv-cache"
chown yuque:yuque "$STATE/.uv-cache" 2>/dev/null || true
exec 9>"$LOCK"
flock -n 9 || die "另一个部署正在进行（$LOCK 被占用）。等它结束再来，别抢同一个工作区。"

cd "$REPO"
[ -f deploy/crb-agent.service ] || die "$REPO 看起来不是这个项目的检出"

say "工作区必须干净（跟踪的文件）"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no >&2
  die "有未提交的改动：先提交或 stash。部署只走「上游里有的 commit」。"
fi
UNTRACKED="$(git ls-files --others --exclude-standard)"
if [ -n "$UNTRACKED" ]; then
  # 未跟踪的常常是本地/机密文件（.env、草稿），不该逼人提交；但也不能装作没看见。
  printf '⚠ 有未跟踪文件（不影响本次部署，但请收拾）：\n%s\n' "$UNTRACKED"
fi

say "快进到上游"
if timeout 45 git fetch origin; then
  git merge --ff-only origin/main
else
  # 这台机器到 GitHub 时通时不通（见 docs/deploy.md §5①）：取不到就按当前 HEAD
  # 部署，但要让人看见这件事，别假装同步过了。
  echo "⚠ 取不到 origin（网络问题？）—— 跳过快进，按当前 HEAD 继续"
fi
COMMIT="$(git rev-parse --short HEAD)"
SUBJECT="$(git log -1 --pretty=%s)"

say "测试（HOME 关进临时目录）"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
HOME="$SANDBOX" PYTHONPATH=src .venv/bin/python -m pytest -q

say "systemd 单元与仓库对齐"
install -m 644 "deploy/$UNIT" /etc/systemd/system/
if [ -d "/etc/systemd/system/${UNIT}.d" ]; then
  echo "⚠ 还有 drop-in：$(ls "/etc/systemd/system/${UNIT}.d")"
  echo "  它会覆盖单元里的设置——确认后删掉"
fi
systemctl daemon-reload

say "重启单元"
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"
sleep 6

say "验收"
RC=0

state="$(systemctl is-active "$UNIT")"
printf '%-24s %s\n' "$UNIT" "$state"
[ "$state" = "active" ] || RC=1

# 启动行：证明它真的跑到了「开始监听」那一步，而不只是进程还在。
if journalctl -u "$UNIT" --since "-3min" --no-pager 2>/dev/null | grep -q "crba 已启动"; then
  printf '%-24s %s\n' "启动行" "有"
else
  printf '%-24s %s\n' "启动行" "没有（journalctl -u $UNIT -n 50 看原因）"
  RC=1
fi

# 密钥门：不带密钥必须被拦。这是这个服务唯一的防线，所以要验它真的在拦，
# 而不是「进程活着」就算过。
if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/agent")" = "403" ]; then
  printf '%-24s %s\n' "密钥门" "在拦（不带密钥 → 403）"
else
  printf '%-24s %s\n' "密钥门" "「不带密钥」没被拦！立刻检查（journalctl -u $UNIT -n 50）"
  RC=1
fi
if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz")" = "200" ]; then
  printf '%-24s %s\n' "/healthz" "ok"
else
  printf '%-24s %s\n' "/healthz" "没反应"
  RC=1
fi

if journalctl -u "$UNIT" --since "-3min" --no-pager 2>/dev/null | grep -q "Traceback"; then
  echo "⚠ 日志里有 Traceback —— 看 journalctl -u $UNIT -n 80" >&2
  RC=1
fi

printf '%s deploy %s %s %s\n' "$(date -Is)" "$WHO" "$COMMIT" "$SUBJECT" | tee -a "$LOG"
if [ "$RC" = "0" ]; then
  say "✓ 部署完成：$COMMIT $SUBJECT"
else
  say "✗ 部署不完整：$COMMIT $SUBJECT（上面有没过的检查）"
fi
exit "$RC"
