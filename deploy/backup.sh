#!/bin/bash
# data/bot.db 를 안전하게 복사해 data/backups/ 에 날짜별로 보관한다 (14일치).
# 봇이 실행 중이어도 sqlite3 의 .backup 은 일관된 사본을 만든다.
# S3 에도 올리려면 아래 S3_BUCKET 을 채우고 서버에 aws cli + IAM 역할(S3 쓰기)을 설정한다.
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/raidbot}"
S3_BUCKET="${S3_BUCKET:-}"          # 예: s3://my-raidbot-backup (비우면 로컬 백업만)
KEEP_DAYS=14

mkdir -p "$APP_DIR/data/backups"
out="$APP_DIR/data/backups/bot-$(date +%Y%m%d-%H%M).db"
sqlite3 "$APP_DIR/data/bot.db" ".backup '$out'"
gzip -f "$out"

find "$APP_DIR/data/backups" -name 'bot-*.db.gz' -mtime +"$KEEP_DAYS" -delete

if [ -n "$S3_BUCKET" ]; then
  aws s3 cp "$out.gz" "$S3_BUCKET/" --only-show-errors
fi
