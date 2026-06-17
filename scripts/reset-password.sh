#!/bin/bash
# reset-password.sh - 重置 Mavis Dashboard 密码
#
# 用法:
#   bash reset-password.sh "new-password"
#   bash reset-password.sh                    # 自动生成随机密码
#
# 流程:
#   1. 生成 sha256("mavis-dashboard-v1" + password)
#   2. 推到 server /etc/systemd/system/mavis-dashboard.service
#   3. daemon-reload + restart 服务
#   4. 打印新密码

set -e

SALT="mavis-dashboard-v1"
SERVICE_FILE="/etc/systemd/system/mavis-dashboard.service"
SSH_KEY="${SSH_PRIVATE_KEY_PATH:-/root/.ssh/id_rsa}"
SSH_HOST_VAL="${SSH_HOST:-163.7.3.92}"
SSH_USER_VAL="${SSH_USER:-root}"
SSH_PORT_VAL="${SSH_PORT:-22}"

# 1. 拿密码
if [ -n "$1" ]; then
  PASSWORD="$1"
else
  PASSWORD=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
  echo "🔑 自动生成密码: $PASSWORD"
fi

# 2. 算 hash
HASH=$(python3 -c "
import hashlib, sys
pwd = sys.argv[1]
print(hashlib.sha256(('${SALT}' + pwd).encode()).hexdigest())
" "$PASSWORD")

echo "🔐 SHA-256 hash: $HASH"

# 3. 推 server
if [ -f "$SSH_KEY" ] && [ -n "$SSH_HOST_VAL" ]; then
  echo "🚀 推送到 $SSH_USER_VAL@$SSH_HOST_VAL..."
  ssh -i "$SSH_KEY" -p "$SSH_PORT_VAL" -o StrictHostKeyChecking=no \
    "$SSH_USER_VAL@$SSH_HOST_VAL" "
      set -e
      # 备份
      [ -f $SERVICE_FILE ] && cp $SERVICE_FILE $SERVICE_FILE.bak.\$(date +%Y%m%d-%H%M%S)
      # 替换 hash
      sed -i 's|^Environment=\"DASHBOARD_PASSWORD_HASH=.*|Environment=\"DASHBOARD_PASSWORD_HASH=$HASH\"|' $SERVICE_FILE
      # 验证
      grep DASHBOARD_PASSWORD_HASH $SERVICE_FILE
      # 重启
      systemctl daemon-reload
      systemctl restart mavis-dashboard
      sleep 1
      systemctl is-active mavis-dashboard
    "
  echo ""
  echo "✅ 密码已更新到 server"
  echo ""
  echo "📋 新登录信息:"
  echo "   URL:      http://$SSH_HOST_VAL:8443/"
  echo "   用户名:    admin"
  echo "   密码:     $PASSWORD"
  echo ""
  echo "💡 建议立刻登录测试，并把密码存到密码管理器"
else
  echo "❌ 没找到 SSH_KEY=$SSH_KEY 或 SSH_HOST=$SSH_HOST_VAL"
  echo "   请手动操作:"
  echo "   1. 复制 hash: $HASH"
  echo "   2. 编辑 $SERVICE_FILE 把 DASHBOARD_PASSWORD_HASH 改成上面"
  echo "   3. systemctl daemon-reload && systemctl restart mavis-dashboard"
fi
