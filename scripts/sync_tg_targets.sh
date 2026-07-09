#!/bin/bash
# sync_tg_targets.sh — 在 Mac 本地运行，把唯一事实源 ~/qwenproxy/.tg-notify-targets.json
# 推送到 fleet 各机器（r4s、bwg），让全 fleet 的 TG 话题路由表保持一致。
#
# Mac 副本是唯一可编辑的事实源；改完这张表（含 createForumTopic 后回写新 thread）
# 就跑一次本脚本。脚本会：
#   1. 校验本地 JSON 合法——绝不把坏表推向 fleet；
#   2. 逐台（串行）拉远端现状 → 显示 diff → scp 推送 → 回读校验一致；
#   3. 任一步失败即停，并打印已同步/未同步的机器。
#
# 用法：
#   ./scripts/sync_tg_targets.sh          # 推送到全部目标机
#   ./scripts/sync_tg_targets.sh --check  # 只显示各机 diff，不推送

set -euo pipefail

SRC="$HOME/qwenproxy/.tg-notify-targets.json"
REMOTE_PATH="qwenproxy/.tg-notify-targets.json"   # 相对远端 $HOME（root → /root/qwenproxy/…）
HOSTS="r4s bwg"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# 1) 本地事实源必须存在且是合法 JSON。
if [ ! -f "$SRC" ]; then
  echo "❌ 本地路由表不存在：$SRC"
  exit 1
fi
if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$SRC" >/dev/null 2>&1; then
  echo "❌ 本地路由表不是合法 JSON，已中止：$SRC"
  exit 1
fi
echo "✅ 本地路由表 JSON 合法：$SRC"

synced_list=""
synced_count=0

for host in $HOSTS; do
  echo ""
  echo "=== $host ==="
  tmp="$(mktemp)"
  # 拉远端现状（文件可能不存在——首次同步）。
  if ssh -o ConnectTimeout=8 "$host" "cat '$REMOTE_PATH'" >"$tmp" 2>/dev/null; then
    if diff -u "$tmp" "$SRC" >/dev/null; then
      echo "✓ 已一致，跳过"
      rm -f "$tmp"
      synced_list="$synced_list $host"
      synced_count=$((synced_count + 1))
      continue
    fi
    echo "变更（远端 → 本地）："
    diff -u "$tmp" "$SRC" || true
  else
    echo "远端暂无该文件（首次同步）"
  fi
  rm -f "$tmp"

  if [ "$CHECK_ONLY" = "1" ]; then
    continue
  fi

  # 推送：先确保目录存在，再 scp，再回读校验。
  ssh -o ConnectTimeout=8 "$host" "mkdir -p qwenproxy"
  scp -o ConnectTimeout=8 "$SRC" "$host:$REMOTE_PATH" >/dev/null
  if ssh -o ConnectTimeout=8 "$host" "cat '$REMOTE_PATH'" | diff -q - "$SRC" >/dev/null; then
    echo "✅ 已推送并回读校验一致"
    synced_list="$synced_list $host"
    synced_count=$((synced_count + 1))
  else
    echo "❌ 回读校验不一致，已中止（$host 未确认）"
    echo "已同步:${synced_list:- 无}"
    exit 1
  fi
done

echo ""
if [ "$CHECK_ONLY" = "1" ]; then
  echo "🔍 check 完成（未推送）"
else
  echo "✅ 同步完成:${synced_list:- 无}（共 $synced_count 台）"
fi
