#!/bin/bash
# dashboard-push.sh - 推送消息到 Mavis Dashboard (增强版)
#
# 用法:
#   dashboard-push.sh alert "磁盘使用 90%"
#   dashboard-push.sh alert "redis 容器崩溃" --level critical
#   dashboard-push.sh info "部署完成"
#   dashboard-push.sh health-check --status ok
#   dashboard-push.sh state "mavis-status 变化" --status warn
#
# 事件类型:
#   alert        - 告警（红色高亮），level: critical/warning/info
#   info         - 普通信息（蓝色）
#   success      - 成功（绿色）
#   health-check - 健康检查结果，--status ok/warn/fail
#   state        - 状态变化，--status ok/warn/fail
#
# 环境变量:
#   DASHBOARD_URL          - Dashboard URL，默认 http://localhost:8443
#   DASHBOARD_USER         - 用户名，默认 admin
#   DASHBOARD_PASSWORD     - 密码（必填，否则登录失败）
#   DASHBOARD_COOKIE_FILE  - cookie 缓存文件，默认 /tmp/mavis-dashboard.cookie

set -e

TYPE="${1:-info}"
MESSAGE="${2:-}"

# 默认值
LEVEL="info"
EXTRA_JSON=""

# 解析额外参数
shift 2 2>/dev/null || true
while [ $# -gt 0 ]; do
  case "$1" in
    --level) LEVEL="$2"; shift 2 ;;
    --status) EXTRA_JSON="${EXTRA_JSON},\"status\":\"$2\""; shift 2 ;;
    --disk-pct) EXTRA_JSON="${EXTRA_JSON},\"disk_pct\":$2"; shift 2 ;;
    --source) EXTRA_JSON="${EXTRA_JSON},\"source\":\"$2\""; shift 2 ;;
    --tag) EXTRA_JSON="${EXTRA_JSON},\"tag\":\"$2\""; shift 2 ;;
    --json) EXTRA_JSON="${EXTRA_JSON},$2"; shift 2 ;;
    *) shift ;;
  esac
done

# 配置
DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8443}"
DASHBOARD_USER="${DASHBOARD_USER:-admin}"
DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-}"
COOKIE_FILE="${DASHBOARD_COOKIE_FILE:-/tmp/mavis-dashboard.cookie}"

# 鉴权（拿 cookie）
ensure_auth() {
  # 如果 cookie 文件存在且 < 6 天，尝试用
  if [ -f "$COOKIE_FILE" ] && [ -n "$(cat "$COOKIE_FILE" 2>/dev/null)" ]; then
    # 验证 cookie 还有效
    if curl -s -b "$COOKIE_FILE" "$DASHBOARD_URL/health" -o /dev/null -w "%{http_code}" 2>/dev/null | grep -q 200; then
      # 检查 history 接口是否 401（401 = cookie 失效）
      code=$(curl -s -b "$COOKIE_FILE" -o /dev/null -w "%{http_code}" "$DASHBOARD_URL/history" 2>/dev/null)
      if [ "$code" = "200" ]; then
        return 0
      fi
    fi
  fi

  # 重新登录
  if [ -z "$DASHBOARD_PASSWORD" ]; then
    echo "❌ DASHBOARD_PASSWORD 未设置，无法登录"
    exit 1
  fi

  rm -f "$COOKIE_FILE"
  http_code=$(curl -s -c "$COOKIE_FILE" -X POST "$DASHBOARD_URL/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$DASHBOARD_USER\",\"password\":\"$DASHBOARD_PASSWORD\"}" \
    -o /dev/null -w "%{http_code}" 2>/dev/null)

  if [ "$http_code" = "200" ]; then
    return 0
  fi
  echo "❌ 登录失败: HTTP $http_code"
  exit 1
}

# 构造 payload
PAYLOAD=$(cat <<EOF
{
  "type": "$TYPE",
  "data": {
    "level": "$LEVEL",
    "message": "$MESSAGE",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "host": "$(hostname)",
    "agent": "Mavis"
    $EXTRA_JSON
  }
}
EOF
)

# 鉴权 + 推送
ensure_auth

response=$(curl -s -b "$COOKIE_FILE" -X POST -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  "$DASHBOARD_URL/publish" 2>&1)

if echo "$response" | grep -q '"success":true'; then
  echo "✅ 推送成功: [$TYPE] $MESSAGE"
  echo "   $response"
else
  echo "❌ 推送失败: $response"
  exit 1
fi
