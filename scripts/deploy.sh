#!/bin/bash
# deploy.sh - 把 mavis-dashboard 仓库部署到远程 server
#
# 用法:
#   bash deploy.sh                # 默认推到 wcaca 仓库对应的 server
#   bash deploy.sh --no-restart   # 只推代码不重启服务
#   bash deploy.sh --host user@x  # 自定义目标
#
# 流程:
#   1. git pull 沙箱里的最新代码
#   2. rsync 到 server:/opt/mavis-dashboard/
#   3. （可选）systemctl restart mavis-dashboard
#   4. 健康检查 curl /health

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SSH_KEY="${SSH_PRIVATE_KEY_PATH:-/root/.ssh/id_rsa}"
SSH_HOST_VAL="${SSH_HOST:-163.7.3.92}"
SSH_USER_VAL="${SSH_USER:-root}"
SSH_PORT_VAL="${SSH_PORT:-22}"
REMOTE_DIR="/opt/mavis-dashboard"

RESTART=true
for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART=false ;;
    --host) SSH_HOST_VAL="${2#*@}"; SSH_USER_VAL="${2%@*}"; shift 2 ;;
  esac
done

SSH_TARGET="$SSH_USER_VAL@$SSH_HOST_VAL"
SSH_OPTS="-i $SSH_KEY -p $SSH_PORT_VAL -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

# 0. 检查 ssh key
if [ ! -f "$SSH_KEY" ]; then
  echo "❌ SSH 私钥不存在: $SSH_KEY"
  echo "   请先跑: bash /tmp/mavis-ssh-helper.sh fetch production"
  exit 1
fi

# 1. git pull（如果是 git 仓库）
if [ -d "$REPO_DIR/.git" ]; then
  echo "📥 git pull..."
  cd "$REPO_DIR" && git pull --rebase 2>&1 | tail -3
fi

# 2. rsync
echo "🚀 rsync → $SSH_TARGET:$REMOTE_DIR"
rsync -avz --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='logs/*.log' \
  -e "ssh $SSH_OPTS" \
  "$REPO_DIR/" "$SSH_TARGET:$REMOTE_DIR/" 2>&1 | tail -10

# 3. restart
if $RESTART; then
  echo "🔄 重启服务..."
  ssh $SSH_OPTS "$SSH_TARGET" "systemctl restart mavis-dashboard && sleep 1 && systemctl is-active mavis-dashboard"
fi

# 4. 健康检查
echo ""
echo "💓 健康检查:"
sleep 1
ssh $SSH_OPTS "$SSH_TARGET" "curl -s http://127.0.0.1:8443/health" 2>&1

echo ""
echo "✅ 部署完成"
echo "   URL: http://$SSH_HOST_VAL:8443/"
