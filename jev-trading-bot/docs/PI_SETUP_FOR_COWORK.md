# Pi setup runbook (for Claude Cowork)

## Paste this to Cowork

> Connect to my Raspberry Pi and set up the `jev-trading-bot` project by following `docs/PI_SETUP_FOR_COWORK.md`
> in that repo, step by step. Paper trading only. Never print, log, commit or echo any API key, secret or token.
> When a secret is needed, tell me to type it into the terminal prompt myself. Stop and tell me at every
> failure; don't edit code or limits to make a check pass. Report back using the format at the end of the runbook.

## Rules for the agent running this

1. **Paper trading only.** The code refuses any non-paper Alpaca endpoint. Never try to change that.
2. **Secrets:** `ALPACA_KEY`, `ALPACA_SECRET`, `TYPESAFE_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DASHBOARD_PASSWORD`, `HEALTHCHECK_URL` live only in `.env` (mode 600) on the Pi. Never put them in a command line, chat message, log, file in the repo, or git commit. Never `cat .env`. If you need to confirm a value exists, check the *name* only (`cut -d= -f1 .env`).
3. **Don't loosen safety.** Don't edit `risk.py`, `config.py` limits, thresholds, or pass `--allow-unvalidated` unless the owner explicitly asks.
4. **No live trading.** There is no switch for it and you must not add one.
5. **Stop on failure.** If a step fails, report the exact error text (with secrets removed) and wait.

## What you need from the owner (they type these; you never see them)

| Value | Where it comes from |
|---|---|
| Alpaca **paper** key id + secret | Alpaca dashboard > Paper Trading > API keys. Do **not** set any base URL; the code uses `https://paper-api.alpaca.markets` itself (no `/v2` suffix to configure). |
| Jev API key (`TYPESAFE_API_KEY`) | TypeSafe dashboard. |
| Telegram bot token + chat id | Message **@BotFather**, `/newbot`, copy the token. Send the new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read `message.chat.id`. Only that chat id is ever obeyed. |
| Optional healthchecks.io URL | Free check at healthchecks.io; set period 10 min, grace 10 min. |
| Dashboard password | Auto-generated into `.env`; the owner reads it with `grep DASHBOARD_PASSWORD .env`. |

## Steps

### 1. Check the Pi
```bash
uname -m              # expect aarch64 (64-bit Raspberry Pi OS). If armv7l, stop: reflash 64-bit.
free -h; df -h /      # 4 GB RAM is fine; need >2 GB free disk
timedatectl           # "System clock synchronized: yes". If not: sudo timedatectl set-ntp true
```
Boot from an SSD or a high-endurance SD card; the bot writes logs constantly.

### 2. Get the code
```bash
git clone --branch claude/new-session-yqrwr7 https://github.com/87w5fcpcbf-cmd/tradingagents.git ~/tradingagents
cd ~/tradingagents/jev-trading-bot
```
If the repo is private the clone asks for credentials: have the owner authenticate (a read-only fine-grained token they enter themselves), or copy the `jev-trading-bot/` folder over with `scp`. Do not store the token in the repo or in shell history.

### 3. Run the setup script
```bash
./scripts/pi_setup.sh
```
It: checks the machine; prompts silently for each secret (the owner types them; blank is allowed only for Telegram/healthcheck); writes `.env` (mode 600); installs Docker if missing (if it just added the user to the `docker` group, it falls back to `sudo docker`); builds the image; runs `jevbot check`. It does **not** start the bot.

Expected `check` output:
```
PASS  alpaca account: paper equity $100,000.00
PASS  alpaca data: 7 recent SPY bars
PASS  jev: model jev-1.x.x, answered 'no' (0.9x), 0.xs
PASS  telegram: test message sent
```
The owner should see the Telegram test message. Troubleshooting:

| FAIL | Likely cause |
|---|---|
| alpaca account (401/403) | Keys are live keys or mistyped. Must be **paper** keys. |
| alpaca data | Paper keys have free IEX data only; check the error text. |
| jev | Wrong/expired `TYPESAFE_API_KEY`, or response shape differs from the docs. Report the error text; do not change the parser silently. |
| telegram | Wrong chat id, or the owner never messaged the bot first. |

Do not continue until all four pass (Telegram may be skipped only if the owner says so, but alerts and approvals won't work).

### 4. Fetch data and backtest
```bash
docker compose run --rm bot fetch-data --years 7 --dir /data/data_cache
docker compose run --rm bot backtest --dir /data/data_cache --out /data/strategy.md
```
(Prefix with `sudo` if docker needs it.) The backtest prints a table of six candidates out-of-sample and writes `data/strategy.md` only if one survives (max drawdown < 15%, >= 30 trades, profit factor > 1.3). **If nothing survives it writes nothing: stop here and report the table to the owner. Do not relax the filter on your own.** The leveraged candidate is never auto-selected.

### 5. Start the bot (only if `data/strategy.md` exists and is validated)
```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail 50 bot
```
Expected: the owner gets a Telegram message "jevbot started (PAPER)". The bot runs one decision pass per weekday after 16:15 New York time, polls Telegram continuously, and survives reboots (`restart: unless-stopped`; Docker is enabled at boot).

### 6. Dashboard
Bound to `127.0.0.1:8000` on the Pi. For phone/laptop access install Tailscale (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`, owner approves login) and run `sudo tailscale serve --bg 8000`. Never port-forward it to the internet.

### 7. Safety tests with the owner (paper only), before leaving it running
1. Send `/status` on Telegram: expect equity and kill-switch status.
2. Send `/kill`: expect "Kill switch TRIPPED". Run `docker compose run --rm bot status`: TRIPPED. Then `docker compose run --rm bot reset`.
3. Temporarily set `APPROVAL_USD=1` in `.env`, `docker compose up -d`, and confirm a signal waits for `/approve` (or times out as rejected after 10 min); then restore `5000`. (Needs a signal to occur; otherwise note it as untested.)
4. Unplug the network briefly: bot should alert on reconnect and place nothing meanwhile.

### 8. Ongoing
```bash
docker compose logs -f bot                 # live log
docker compose run --rm bot report         # daily report text
docker compose run --rm bot kill           # stop all trading now
docker compose run --rm bot reset          # re-arm after a kill (owner decision only)
docker compose pull && git pull && docker compose up -d --build   # updates
```

## Report back in this format

```
Pi: <model, aarch64?, RAM, disk free>
check: alpaca account PASS/FAIL, alpaca data PASS/FAIL, jev PASS/FAIL (model version), telegram PASS/FAIL
backtest: <the printed table> ; winner: <name or none>
bot started: yes/no ; Telegram "started" message received: yes/no
dashboard reachable via: <tailscale/localhost>
safety tests: 1..4 pass/fail/untested
problems: <exact error text with secrets removed>
```
