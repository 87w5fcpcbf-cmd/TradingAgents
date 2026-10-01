# Jev Trading Bot — Design Spec

Date: 2026-10-01
Status: Draft for review
Not financial advice. Paper trading only in v1. Approval-gated.

## 1. Goal

A standalone, approval-gated trading bot for US stocks/ETFs on Alpaca **paper** trading.

- Opus (via Claude Code) designs and backtests strategies and writes `strategy.md`.
- Jev (TypeSafe, `POST https://api.typesafe.ai/v1/systemone`) is the **live decision layer**:
  it scores fixed-outcome questions on live data and returns probabilities.
- The broker only executes.
- Success: a bot running on paper 24/7 on a Raspberry Pi 5, a strategy-tester dashboard,
  `strategy.md`, and a daily report (trades, P&L, win rate, largest loss, Jev average
  latency and cost per decision). Live money is out of scope for v1.

## 2. Non-goals

- No live-money mode in v1. No withdrawals, no password/2FA handling, ever.
- No Jev-originated trades. Rules propose, Jev gates.
- No options, futures, crypto, or intraday (sub-daily) trading.
- No public internet exposure of the dashboard.

## 3. Architecture

One Python 3.12 project, one Docker Compose stack on a Raspberry Pi 5 (4 GB, 64-bit
Raspberry Pi OS, SSD or high-endurance SD). SQLite is the single source of truth.
Backtests are a separate CLI command runnable on any machine.

| Module | Responsibility |
|---|---|
| `data` | Historical and live bars from Alpaca into a local cache. |
| `backtest` | Replays >= 5 years across regimes; models commission and slippage; in-sample/out-of-sample split; filters candidates. |
| `strategy` | Loads and validates `strategy.md` (entry, exit, stop loss, take profit, timeframe, Jev thresholds). |
| `jev` | Jev API client. Data in, probabilities out. Logs every answer with resolved `model`, latency, cost. |
| `signal` | Fires a trade only when every Jev probability clears its `strategy.md` threshold and answers agree with rule direction. |
| `risk` | Hard limits Jev cannot override; kill switch; approval gate. |
| `broker` | Only module that can place orders. Alpaca paper, trade-only key. |
| `alerts` | Telegram (BotFather bot), allow-listed chat ID only. |
| `dashboard` | FastAPI + HTML strategy tester behind a login, LAN/Tailscale only. |
| `report` | Daily report. |

Data flow: scheduler tick per bar -> `data` -> rules propose candidate -> `jev` scores ->
`signal` decides -> `risk` checks -> (approval if above size) -> `broker` bracket order ->
`alerts` + database record.

## 4. Strategy and backtest

- Universe: SPY, QQQ, IWM; candidates 4-6 use sector ETFs, an international ETF and bonds.
- Timeframe: daily bars.
- Candidates:
  1. 50/200-day MA trend follower
  2. RSI(2) mean reversion
  3. 20-day breakout with volatility-based stop
  4. Sector-rotation momentum (top 3 of 11 sector ETFs by 6-month return, monthly; cash when SPY < 200-day MA)
  5. Dual momentum (SPY / international / bonds by trailing 12-month return)
  6. Leveraged trend follower (TQQQ/UPRO above unleveraged 200-day MA, else cash) —
     high risk; evaluated under its own looser drawdown cap and 3% position size, clearly labelled.
- Data: >= 5 years (covers 2020 crash, 2022 bear, 2023-24 rally). Commission and slippage modelled.
- Overfitting guard: tune on in-sample, judge only on out-of-sample.
- Survival filter (out-of-sample, starting values, configurable): max drawdown < 15%
  (candidate 6 uses its own cap), >= 30 trades, profit factor > 1.3, acceptable win rate.
  High return alone never qualifies a candidate.
- The winner is written to `strategy.md`.
- The backtest tests rules only, not Jev (look-ahead/contamination risk on historical text).
  Jev's value is measured from paper-trading logs: results with the gate vs. rules alone.

## 5. Jev integration

- Questions are `choice` type, each with a `none_of_these` option. Market data and headlines
  go only in `state`, as data, never as instructions.
  - Market regime: trending_up / trending_down / range_bound / none_of_these
  - Headline read: bullish / bearish / neutral / none_of_these
  - Buying pressure building: yes / no / none_of_these
- Trade fires only if every probability >= its threshold (start 0.80) and regime and headline
  agree with the rule signal direction.
- The response shape is verified against the docs and a real call before the client is
  written (the notes show both `answers.q.*` and top-level fields).
- Jev errors, timeouts or malformed output mean no trade (fail closed); repeated failures trip the kill switch.
- `TYPESAFE_API_KEY` comes from the environment only.

## 6. Risk rules (`strategy.md`, enforced by `risk`)

- Max position 10% of equity per trade (3% for candidate 6); 25% total exposure.
- Daily loss limit 2% of equity: stop opening new trades until next session.
- Kill switch (halts everything until manually reset) on any of: `/kill`; daily limit hit twice
  in a row; 8% drawdown from peak; 3 consecutive broker errors; stale market data; repeated Jev failures.
- Every entry is a bracket order so stop and take-profit live at Alpaca.
- Paper trade until results match the backtest. Live money is a separate, later decision.

## 7. Approval gate

Trades above `$5,000` (configurable) are held. Telegram message shows symbol, size, Jev
probabilities and the firing rule. `/approve` or `/reject`; no reply in 10 minutes = reject.

## 8. Alerts, dashboard, secrets, deployment

- Telegram alerts: every fill, error and kill-switch event. Commands: `/status /kill /approve /reject`.
- Dashboard: every signal, its Jev probabilities, threshold check, result; equity curve;
  kill-switch state; daily report. Login required; LAN or Tailscale only.
- Secrets: `.env` (gitignored, mode 600) holding `TYPESAFE_API_KEY`, Alpaca key/secret,
  Telegram token. Alpaca key trade-only. Nothing hardcoded or committed.
  The Jev key was pasted into chat during design and should be rotated.
- Deployment: Docker Compose, `restart: unless-stopped`, time sync on, daily report job, heartbeat ping to an external uptime monitor.
- Fail safe: on lost connectivity the bot opens no new positions and alerts on reconnect.

## 9. Testing

- Unit tests for `risk` (every kill-switch trigger, limits, approval timeout), `signal`
  thresholds, `strategy.md` validation, and the Jev client (mocked, including malformed responses).
- Backtest engine tested on synthetic data with known results.
- Paper-run checklist before any live discussion: paper matches backtest, kill switch fired
  in testing, and the "WHAT COULD BLOW UP THIS ACCOUNT?" review is written.

## 10. Open items

- Pi model confirmed as Pi 5 (4 GB assumed; verify).
- Telegram bot and Alpaca paper keys to be created by the user.
- Approval size, thresholds and drawdown caps are starting values to adjust after backtests.
