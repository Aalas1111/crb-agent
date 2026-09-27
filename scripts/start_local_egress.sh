#!/usr/bin/env bash
# 把**这台机器**借给服务器用：本机出口 + crb 执行器（见 docs/deploy.md §0.1）。
#
# 为什么需要它：服务器（机房 IP）调不动学校办事大厅 —— 实测**与出口 IP 无关**
# （用隧道把服务器出口换成校园网也一样 403），而是「请求必须从这台机器发出」。
# 所以本机跑一个只绑回环的执行器，服务器经 SSH 隧道把 `crb ...` 发过来执行。
#
# 用法：
#   scripts/start_local_egress.sh --server lihe@<服务器地址>
#   CRBA_SERVER=lihe@<服务器地址> scripts/start_local_egress.sh
#
#   --server <user@host>  必填（地址不进仓库）
#   --port <端口>         默认 18890，要和服务器侧的 CRBA_EXEC_PORT 一致
#
# 前台运行，Ctrl-C 停。隧道断了会自动重连。
# 这台机器关机时服务器上仍然能用界面，只是调学校的工具会提示「执行器没连上」。
set -uo pipefail

SERVER="${CRBA_SERVER:-}"
PORT="${CRBA_EXEC_PORT:-18890}"
CRB_CMD="${CRBA_LOCAL_CRB:-crb}"
PY="${PYTHON:-python}"

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:?--server 后面要跟 user@host}"; shift 2 ;;
    --port) PORT="${2:?--port 后面要跟端口}"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "不认识的参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

[ -n "$SERVER" ] || {
  echo "必须给 --server（或设 CRBA_SERVER）—— 地址不进仓库" >&2
  exit 2
}

HERE="$(cd "$(dirname "$0")" && pwd)"
say() { printf '\n== %s\n' "$*"; }

say "1/3 检查本机的 crb"
if ! "$CRB_CMD" --version >/dev/null 2>&1; then
  cat >&2 <<EOF
✗ 跑不了 \`$CRB_CMD\`。
  用 CRBA_LOCAL_CRB 指定路径，例如：
    CRBA_LOCAL_CRB=/path/to/.venv/Scripts/crb.exe $0 --server $SERVER
EOF
  exit 1
fi
"$CRB_CMD" --version
"$CRB_CMD" doctor --json >/dev/null 2>&1 || {
  echo "⚠ 本机 crb 现在读不通学校（多半是登录态失效）。在本机跑一次 \`crb login\` 即可。"
}

say "2/3 起 crb 执行器（只绑 127.0.0.1:$PORT）"
"$PY" "$HERE/local_crb_executor.py" "$PORT" &
EXEC_PID=$!
# shellcheck disable=SC2064
trap "kill $EXEC_PID 2>/dev/null; rm -f /tmp/crba_egress_$$.log" EXIT
sleep 1
if ! kill -0 "$EXEC_PID" 2>/dev/null; then
  echo "✗ 执行器没起来（端口 $PORT 被占？换个 --port）" >&2
  exit 1
fi
echo "✓ 执行器在跑（pid $EXEC_PID）"

say "3/3 建隧道并保持（→ $SERVER）"
echo "  这个窗口请保持开着；Ctrl-C 停。"
while true; do
  ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -R "$PORT:127.0.0.1:$PORT" "$SERVER"
  echo "隧道断了，5 秒后重连…（Ctrl-C 停）"
  sleep 5
done
