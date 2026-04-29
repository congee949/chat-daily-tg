#!/bin/bash
# deploy.sh — 在 Mac 本地运行，拉取最新代码并重启 launchd 任务
#
# 用法：
#   ./deploy.sh          # 拉代码 + 重启 launchd
#   ./deploy.sh --pull   # 只拉代码
#   ./deploy.sh --status # 查看服务状态

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="origin"
BRANCH="master"

cd "$REPO_DIR"

PLIST_LABEL="com.chat-daily.tg"
PLIST_SRC="$REPO_DIR/launchd/$PLIST_LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

case "${1:-deploy}" in
  --pull)
    echo "📥 拉取最新代码..."
    git fetch "$REMOTE"
    git reset --hard "$REMOTE/$BRANCH"
    echo "✅ 代码已更新到 $(git rev-parse --short HEAD)"
    ;;
  --status)
    echo "=== 服务状态 ==="
    launchctl list | grep "$PLIST_LABEL" 2>/dev/null && echo "✅ launchd 已加载" || echo "❌ launchd 未加载"
    echo ""
    echo "=== Git 状态 ==="
    git log --oneline -3
    echo "本地: $(git rev-parse --short HEAD)"
    echo "远程: $(git rev-parse --short $REMOTE/$BRANCH 2>/dev/null || echo '未知')"
    ;;
  deploy)
    echo "📥 拉取最新代码..."
    OLD=$(git rev-parse --short HEAD)
    git fetch "$REMOTE"
    git reset --hard "$REMOTE/$BRANCH"
    NEW=$(git rev-parse --short HEAD)

    if [ "$OLD" = "$NEW" ]; then
      echo "✅ 代码无变化 ($OLD)"
    else
      echo "✅ 代码已更新: $OLD → $NEW"
      git log --oneline "$OLD..$NEW"
    fi

    # 安装依赖
    if [ -f pyproject.toml ]; then
      echo "📦 安装依赖..."
      pip install -q -e . 2>/dev/null || pip install -q -r requirements.txt 2>/dev/null || true
    fi

    # 更新 launchd plist
    if [ -f "$PLIST_SRC" ]; then
      cp "$PLIST_SRC" "$PLIST_DST"
      launchctl unload "$PLIST_DST" 2>/dev/null || true
      launchctl load "$PLIST_DST"
      echo "✅ launchd 已重载"
    else
      echo "⚠️ 未找到 $PLIST_SRC，跳过 launchd 配置"
    fi

    echo "✅ 部署完成"
    ;;
  *)
    echo "用法: $0 [--pull|--status|deploy]"
    exit 1
    ;;
esac
