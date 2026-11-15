#!/bin/bash
# Cron launcher for overnight-trader.py. Run every 5 minutes; the script itself
# decides what to do from the current New York time.
#
# Keys live in ~/.be-trader.env (chmod 600), NOT in this repository:
#   export APCA_API_KEY_ID=...
#   export APCA_API_SECRET_KEY=...
#   export APCA_API_BASE_URL=https://api.alpaca.markets
#   export BE_TRADER_ARGS="--live --max-usd 1000"
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BE_TRADER_ENV:-$HOME/.be-trader.env}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
cd "$DIR"
# shellcheck disable=SC2086
exec python3 overnight-trader.py "${1:-auto}" ${BE_TRADER_ARGS:-} >> "$DIR/overnight-trader-cron.log" 2>&1
