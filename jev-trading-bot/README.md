# jevbot: approval-gated paper-trading bot

**Not financial advice. Paper trading only.** There is no live-money mode: the broker module refuses any
endpoint except `https://paper-api.alpaca.markets`.

Opus designs and backtests the strategy. [Jev](https://docs.typesafe.ai) (TypeSafe) is the live decision layer: it
scores fixed-outcome questions on live data and returns probabilities. Alpaca only executes.

```
backtest -> strategy.md -> daily after-close tick:
  rules propose entry -> Jev scores 3 questions -> every probability must clear its threshold
  -> risk limits -> (manual approval if above size) -> Alpaca paper bracket order -> Telegram + dashboard
```

Rules for what Jev can and cannot do:
- Jev only gates **entries**. Exits are rule-based or stop/take-profit orders that live at Alpaca, so they work even if Jev or the Pi is down.
- Any Jev error, timeout or malformed answer means **no trade**. Five in a row trips the kill switch.
- Headlines and data are passed to Jev as `state` (data), never inside question text.
- Every Jev answer is logged with the resolved model version, latency and cost.

## Status: what is and is not verified

- 120 automated tests cover the engine, risk limits, kill switch, approvals, Jev client, broker, alerts, dashboard and CLI.
- **No real-market backtest has been run yet.** The build environment could not reach a market-data source. So `strategy.md` is intentionally absent and the bot **refuses to run** until `jevbot backtest` writes a validated one (or you pass `--allow-unvalidated`).
- Not yet exercised against the live Jev, Alpaca or Telegram services (no keys in the build environment). The Jev client accepts both response shapes seen in your notes (`answers.<q>.*` and top-level); confirm with one real call.
- The Docker files are written but were not built here.

## Setup on a Raspberry Pi 5

1. Flash **64-bit Raspberry Pi OS**, boot from an SSD or a high-endurance SD card, enable SSH, and set the timezone. Plug it into a UPS or surge protector.
2. Install Docker: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER` (log out and back in).
3. `git clone` this project, then `cp .env.example .env && chmod 600 .env`.
4. Fill `.env` yourself (never paste keys into chat or commit them):
   - `ALPACA_KEY`, `ALPACA_SECRET`: Alpaca **paper** keys (trade permission; paper keys cannot withdraw).
   - `TYPESAFE_API_KEY`: your Jev key. Rotate it if it was ever shared in a chat or log.
   - `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`: message **@BotFather**, send `/newbot`, copy the token. Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` to read your chat id. Only that chat id is ever obeyed.
   - `DASHBOARD_PASSWORD`: a long random password (the dashboard serves nothing without it).
   - `APPROVAL_USD`: trades above this wait for your `/approve` (default 5000).
   - Optional `HEALTHCHECK_URL`: a free [healthchecks.io](https://healthchecks.io) check; you get alerted if the bot goes silent.
5. Or skip steps 3-4: run `./scripts/pi_setup.sh` (see `docs/PI_SETUP_FOR_COWORK.md`); it creates `.env`, builds the image and runs `jevbot check`.
6. Get data and validate a strategy:
   ```
   docker compose run --rm bot check
   docker compose run --rm bot fetch-data --years 7 --dir /data/data_cache
   docker compose run --rm bot backtest --dir /data/data_cache --out /data/strategy.md
   ```
   If nothing survives, **do not trade**; the command says so and writes nothing. The filter is strict by design (max drawdown < 15%, at least 30 trades, profit factor > 1.3). Low-frequency strategies often fail the 30-trade rule; relax with `--min-trades` knowingly, not casually.
7. Start it (only if a strategy was written): `docker compose up -d --build`. Check `docker compose logs -f bot`.
8. Dashboard: `http://localhost:8000` (localhost only). For your phone use [Tailscale](https://tailscale.com) (`tailscale serve 8000`); do not open the port to the internet.

## Safety rails (all enforced in code, in `risk.py`)

| Rail | Default |
|---|---|
| Normal order size / max position | 5% / 10% of equity (3% for the leveraged candidate) |
| Total exposure | 25% of equity |
| Daily loss limit | 2%: no new entries; two consecutive days trips the kill switch |
| Drawdown kill | 8% below peak equity |
| Other kill triggers | 3 broker errors in a row, 3 stale-data ticks, 5 Jev failures, `/kill` |
| Manual approval | any order above `APPROVAL_USD`; 10-minute timeout = reject |
| Kill switch | persisted; halts all entries and rule exits; reset only on the machine with `jevbot reset` |

Telegram commands (your chat only): `/status`, `/kill`, `/approve [id]`, `/reject [id]`.
CLI: `jevbot check | fetch-data | backtest | run | status | report | kill | reset | dashboard`.

## Before going anywhere near live money

Read [`docs/WHAT_COULD_BLOW_UP.md`](docs/WHAT_COULD_BLOW_UP.md). There is deliberately no switch for live trading.
