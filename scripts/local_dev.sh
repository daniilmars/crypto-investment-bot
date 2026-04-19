#!/usr/bin/env bash
# Local Mini App dev harness — runs FastAPI against a copy of the
# production database so you can iterate on the UI with real data.
#
# Safety:
#  - MINIAPP_READONLY_SERVER skips the bot, webhook, and background tasks.
#  - MINIAPP_DEV_MODE bypasses Telegram initData auth using a random key.
#  - Auth module refuses to run dev-mode if K_SERVICE / GCP_PROJECT_ID /
#    SERVICE_URL are set, so this can never flip on in production.
#
# Usage:
#   ./scripts/local_dev.sh                 # fresh DB copy + uvicorn
#   SKIP_DB_COPY=1 ./scripts/local_dev.sh  # reuse existing local DB
set -euo pipefail

cd "$(dirname "$0")/.."

VM=${BOT_VM:-crypto-bot-eu}
ZONE=${BOT_ZONE:-europe-west3-a}
LOCAL_DB=./data/crypto_data.local.db

mkdir -p ./data

if [[ "${SKIP_DB_COPY:-0}" != "1" ]]; then
  echo "→ Copying production DB from $VM ($ZONE)..."
  gcloud compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --command \
    "sudo docker cp crypto-bot:/app/data/crypto_data.db /tmp/cbot.db && sudo chmod 644 /tmp/cbot.db"
  gcloud compute scp --zone "$ZONE" --tunnel-through-iap \
    "$VM:/tmp/cbot.db" "$LOCAL_DB"
  echo "→ DB copied: $(du -h "$LOCAL_DB" | cut -f1)"
else
  echo "→ SKIP_DB_COPY=1 — reusing $LOCAL_DB"
fi

# Generate or reuse a dev key for this session
DEV_KEY=${MINIAPP_DEV_KEY:-$(uuidgen | tr -d '-')}
export MINIAPP_DEV_MODE=true
export MINIAPP_DEV_KEY="$DEV_KEY"
export MINIAPP_READONLY_SERVER=true
# BOT_DB_PATH points the SQLite fallback at our prod-DB copy directly,
# avoiding the symlink hack that polluted tests reading data/crypto_data.db.
export BOT_DB_PATH="$PWD/${LOCAL_DB#./}"
unset DATABASE_URL

# Belt + suspenders: unset anything that would let auth think it's prod
unset K_SERVICE GCP_PROJECT_ID SERVICE_URL

URL="http://localhost:8000/miniapp/?dev_key=$DEV_KEY"
echo
echo "=============================================================="
echo "  Mini App local preview"
echo "  URL:  $URL"
echo "  DB:   $LOCAL_DB"
echo "  (editing static/miniapp/* → refresh browser)"
echo "  (editing src/api/* → uvicorn auto-reloads)"
echo "=============================================================="
echo

exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --reload
