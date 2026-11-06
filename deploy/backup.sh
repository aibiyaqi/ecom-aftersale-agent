#!/usr/bin/env bash
# 部署前/每日备份 SQLite 数据库（MySQL 请改用 mysqldump）
#
# 用法：bash deploy/backup.sh
# 产物：./backups/orders-YYYYmmdd-HHMMSS.db，保留 7 天
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
STAMP="$(date +%Y%m%d-%H%M%S)"

if grep -q '^DATABASE_URL=mysql' .env 2>/dev/null; then
  echo "检测到 MySQL 连接串：请改用 mysqldump 备份，本脚本仅处理 SQLite" >&2
  exit 1
fi

mkdir -p backups

docker compose -f "$COMPOSE_FILE" exec -T app python - "$STAMP" <<'PY'
import os
import sqlite3
import sys

stamp = sys.argv[1]
src_path = os.environ.get("DB_PATH", "/data/orders.db")
dst_path = f"/data/backups/orders-{stamp}.db"
os.makedirs("/data/backups", exist_ok=True)

src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()

print(f"backup written: {dst_path}")
PY

docker compose -f "$COMPOSE_FILE" cp "app:/data/backups/orders-${STAMP}.db" "./backups/"
find ./backups -name 'orders-*.db' -mtime +7 -delete

echo "备份完成：backups/orders-${STAMP}.db"
