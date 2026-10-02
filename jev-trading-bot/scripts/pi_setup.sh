#!/usr/bin/env bash
# One-shot setup for a Raspberry Pi (or any Linux box). PAPER TRADING ONLY.
# Secrets are read from the environment if already set, otherwise prompted silently, and written only to
# ./.env (mode 600). Nothing secret is ever printed, logged or committed.
# Set JEVBOT_SETUP_SKIP_DOCKER=1 to only create .env (used for testing).
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n== %s\n' "$*"; }

say "Checking machine"
arch="$(uname -m)"; echo "architecture: $arch"
[ "$arch" = "aarch64" ] || echo "note: expected aarch64 (64-bit Raspberry Pi OS); continuing on $arch"
mkdir -p data

if [ -f .env ] && [ "${1:-}" != "--force" ]; then
  say ".env already exists; keeping it (pass --force to rewrite)"
else
  say "Creating .env (values are not echoed)"
  umask 077
  ask() { # name, prompt, required(1/0)
    local name="$1" prompt="$2" required="$3" val="${!1:-}"
    if [ -z "$val" ] && [ -t 0 ]; then   # only prompt when a person is at the terminal
      read -rsp "$prompt: " val; echo
    fi
    if [ -z "$val" ] && [ "$required" = 1 ]; then echo "$name is required" >&2; exit 1; fi
    printf '%s' "$val"
  }
  ALPACA_KEY="$(ask ALPACA_KEY 'Alpaca PAPER key id' 1)"
  ALPACA_SECRET="$(ask ALPACA_SECRET 'Alpaca PAPER secret' 1)"
  TYPESAFE_API_KEY="$(ask TYPESAFE_API_KEY 'Jev (TypeSafe) API key' 1)"
  TELEGRAM_TOKEN="$(ask TELEGRAM_TOKEN 'Telegram bot token (blank to skip)' 0)"
  TELEGRAM_CHAT_ID="$(ask TELEGRAM_CHAT_ID 'Telegram chat id (blank to skip)' 0)"
  HEALTHCHECK_URL="$(ask HEALTHCHECK_URL 'healthchecks.io ping URL (blank to skip)' 0)"
  DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-}"
  [ -n "$DASHBOARD_PASSWORD" ] || DASHBOARD_PASSWORD="$(openssl rand -base64 24 | tr -d '=+/')"
  {
    echo "ALPACA_KEY=$ALPACA_KEY"
    echo "ALPACA_SECRET=$ALPACA_SECRET"
    echo "TYPESAFE_API_KEY=$TYPESAFE_API_KEY"
    echo "TELEGRAM_TOKEN=$TELEGRAM_TOKEN"
    echo "TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID"
    echo "HEALTHCHECK_URL=$HEALTHCHECK_URL"
    echo "DASHBOARD_PASSWORD=$DASHBOARD_PASSWORD"
    echo "APPROVAL_USD=5000"
  } > .env
  chmod 600 .env
  echo ".env written (mode 600). The dashboard password is inside it; read it yourself with: grep DASHBOARD_PASSWORD .env"
fi

if [ "${JEVBOT_SETUP_SKIP_DOCKER:-}" = 1 ]; then echo "skipping docker steps"; exit 0; fi

say "Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
fi
DOCKER=docker
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
sudo systemctl enable docker >/dev/null 2>&1 || true

say "Building image"
$DOCKER compose build

say "Testing connections (Alpaca, Jev, Telegram)"
$DOCKER compose run --rm bot check || { echo "A check failed. Fix it before going further; do not start the bot." >&2; exit 1; }

cat <<'NEXT'

Setup done. The bot is NOT started yet. Next:
  docker compose run --rm bot fetch-data --years 7 --dir /data/data_cache
  docker compose run --rm bot backtest --dir /data/data_cache --out /data/strategy.md
  (if a strategy was written)  docker compose up -d --build
NEXT
