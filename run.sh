#!/usr/bin/env bash
# Start Helpdesk API + bundled frontend.  Usage: PORT=8091 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8091}"

exec ./.venv/bin/uvicorn backend.app.main:app --host "$HOST" --port "$PORT" "$@"
