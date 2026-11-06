#!/usr/bin/env bash
# 服务器端部署脚本：备份 -> 拉镜像 -> 重启 -> 健康检查 -> 失败回滚
#
# 用法（服务器 /opt/ecom）：
#   bash deploy/deploy.sh v2.0.0
#
# 依赖：docker compose、curl；镜像在 GHCR，需先 docker login ghcr.io
set -euo pipefail

TAG="${1:?用法: deploy.sh <tag>，例如 v2.0.0}"
cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/aibiyaqi/ecom-aftersale-agent}"
CONTAINER_NAME="${CONTAINER_NAME:-ecom-app}"
STATE_FILE="${STATE_FILE:-.current_tag}"

PREV_TAG="$(cat "$STATE_FILE" 2>/dev/null || true)"

echo "== 1/4 部署前备份 =="
if [ -n "$(docker compose -f "$COMPOSE_FILE" ps -q app 2>/dev/null || true)" ]; then
  bash deploy/backup.sh
else
  echo "app 尚未运行，跳过备份（首次部署）"
fi

echo "== 2/4 拉取镜像 ${IMAGE_REPO}:${TAG} =="
export APP_IMAGE="${IMAGE_REPO}:${TAG}"
docker compose -f "$COMPOSE_FILE" pull app
docker compose -f "$COMPOSE_FILE" up -d app

echo "== 3/4 健康检查 =="
STATUS="unknown"
for _ in $(seq 1 30); do
  STATUS="$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo unknown)"
  if [ "$STATUS" = "healthy" ]; then
    break
  fi
  sleep 2
done

if [ "$STATUS" != "healthy" ]; then
  echo "健康检查失败（当前状态：$STATUS）"
  echo "== 4/4 回滚 =="
  if [ -n "$PREV_TAG" ]; then
    export APP_IMAGE="${IMAGE_REPO}:${PREV_TAG}"
    docker compose -f "$COMPOSE_FILE" up -d app || true
    echo "已回滚到 ${PREV_TAG}，请查看日志：docker logs --tail 200 ${CONTAINER_NAME}"
  else
    echo "没有可回滚的历史版本，请检查日志：docker logs --tail 200 ${CONTAINER_NAME}"
  fi
  exit 1
fi

echo "$TAG" > "$STATE_FILE"

echo "== 4/4 完成 =="
echo "已部署 ${TAG}；健康检查通过。"
echo "提示：确认 Dify 的 BACKEND_BASE_URL 指向 https://<DOMAIN>"
