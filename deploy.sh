#!/bin/bash
# deploy.sh — 在 Mac 本地运行，拉取最新代码并重载 launchd 任务
#
# 用法：
#   ./deploy.sh          # 拉代码 + 同步依赖 + 重载 launchd
#   ./deploy.sh --pull   # 只拉代码
#   ./deploy.sh --status # 查看服务状态
#
# 覆盖默认分支：DEPLOY_BRANCH=main ./deploy.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="origin"
# 默认部署当前分支，而不是写死的 master——后者会在 feature 分支上把工作区
# 重置成 origin/master，抹掉在途改动（review finding #18）。
BRANCH="${DEPLOY_BRANCH:-$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)}"

cd "$REPO_DIR"

# Detached HEAD → "HEAD"; reset --hard origin/HEAD would silently snap to the
# remote default branch (undoing this branch's work). Refuse, require explicit branch.
if [ "$BRANCH" = "HEAD" ]; then
  echo "❌ 处于 detached HEAD，请显式指定分支：DEPLOY_BRANCH=<branch> $0"
  exit 1
fi

# reset --hard 会丢弃未提交改动且 reflog 无法找回工作区编辑——动手前先拦住。
require_clean_tree() {
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "❌ 有未提交改动，已中止部署（先 commit 或 stash）："
    git status --short
    exit 1
  fi
}

update_code() {
  echo "📥 拉取最新代码（分支 $BRANCH）..."
  git fetch "$REMOTE"
  require_clean_tree
  git reset --hard "$REMOTE/$BRANCH"
}

case "${1:-deploy}" in
  --pull)
    OLD=$(git rev-parse --short HEAD)
    update_code
    echo "✅ 代码已更新到 $(git rev-parse --short HEAD)（原 $OLD）"
    ;;
  --status)
    echo "=== 服务状态 ==="
    if launchctl list | grep -q "com.chat-daily-tg"; then
      echo "✅ launchd 已加载："
      launchctl list | grep "com.chat-daily-tg"
    else
      echo "❌ launchd 未加载"
    fi
    echo ""
    echo "=== Git 状态 ==="
    git log --oneline -3
    echo "本地: $(git rev-parse --short HEAD)"
    echo "远程: $(git rev-parse --short "$REMOTE/$BRANCH" 2>/dev/null || echo '未知')"
    ;;
  deploy)
    OLD=$(git rev-parse --short HEAD)
    update_code
    NEW=$(git rev-parse --short HEAD)
    if [ "$OLD" = "$NEW" ]; then
      echo "✅ 代码无变化 ($OLD)"
    else
      echo "✅ 代码已更新: $OLD → $NEW"
      git log --oneline "$OLD..$NEW"
    fi

    # 同步依赖到 uv 管理的 .venv。失败必须中止——旧版用裸 pip + `|| true`，
    # pip 不在 PATH 时被静默吞成 no-op，引入新依赖后下次运行 ImportError（finding #21）。
    if [ -f pyproject.toml ]; then
      echo "📦 uv sync 同步依赖..."
      uv sync
    fi

    # 重载 launchd——委托给 install-launchd.sh（渲染占位符 + 安装 agent 与 channels
    # 两个 label）。旧版用错误 label com.chat-daily.tg，从不真正重载（finding #17）。
    echo "🔄 重载 launchd..."
    bash "$REPO_DIR/scripts/install-launchd.sh"

    echo "✅ 部署完成"
    ;;
  *)
    echo "用法: $0 [--pull|--status|deploy]"
    exit 1
    ;;
esac
