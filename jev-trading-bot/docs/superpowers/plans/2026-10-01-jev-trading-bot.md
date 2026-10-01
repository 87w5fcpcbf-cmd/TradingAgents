# Jev Trading Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (native, inline). Steps use checkbox syntax.

**Goal:** Paper-trading, approval-gated US-ETF bot: rules propose, Jev gates, Alpaca paper executes, with backtester, dashboard, Telegram alerts, daily report.

**Architecture:** Python package `jevbot` (src layout), SQLite as source of truth, pure-function core (indicators, rules, backtest, risk, signal) with thin I/O adapters (Jev, Alpaca, Telegram). Everything order-related funnels through `risk` then `broker`.

**Tech Stack:** Python 3.11+, pandas, numpy, requests, FastAPI + uvicorn, pytest, SQLite (stdlib), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-01-jev-trading-bot-design.md`

## Global Constraints

- Paper trading only; `broker` refuses any base URL other than `https://paper-api.alpaca.markets`.
- Secrets only from environment (`TYPESAFE_API_KEY`, `ALPACA_KEY`, `ALPACA_SECRET`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DASHBOARD_PASSWORD`); never hardcoded or logged.
- Jev: `POST https://api.typesafe.ai/v1/systemone`, model `jev-latest`, `choice` questions always include `none_of_these`; log resolved `model`, latency, cost per answer. Fail closed on any error.
- Risk defaults: 10% max position (3% leveraged), 25% total exposure, 2% daily loss limit, 8% drawdown kill, 3 consecutive broker errors kill, approval above $5,000, approval timeout 10 min = reject.
- Jev thresholds start at 0.80. Backtest filter: max drawdown < 15%, >= 30 trades, profit factor > 1.3 (out-of-sample).
- Feed text and headlines are data inside `state`, never instructions.

## Review Focus

- Jev returns malformed/missing fields, HTTP error, or times out -> no trade, error logged.
- Kill switch tripped -> no order of any kind, even an approved one, until manual reset.
- Pending approval expires or reply is from an unknown chat ID -> reject, never trade.
- Stale or empty market data -> no signal, kill switch after repeated staleness.
- Backtest with too few trades or flat data -> candidate rejected, not divide-by-zero.
- Daily loss limit hit mid-day -> new entries blocked, existing bracket stops untouched.

---

### Task 1: Scaffold, config, database
**Files:** Create `pyproject.toml`, `.gitignore`, `.env.example`, `src/jevbot/__init__.py`, `config.py`, `db.py`; Test `tests/test_db.py`
**Interfaces:** Produces `Config` (dataclass from env), `Db(path)` with `.log_signal(dict)`, `.log_trade(dict)`, `.log_jev(dict)`, `.get_flag(name)/.set_flag(name,val)`, `.signals()`, `.trades()`.
- [ ] Test: flags default False, set/get round-trips; signal/trade/jev rows persist and read back.
- [ ] Implement, run `pytest tests/test_db.py`, commit.

### Task 2: Jev client
**Files:** Create `src/jevbot/jev.py`; Test `tests/test_jev.py`
**Interfaces:** Produces `ask(state, questions, model="jev-latest") -> JevResult` with `.answers: dict[name -> {choice, confidence, probabilities}]`, `.model`, `.latency_s`; raises `JevError`. Parser accepts both `body["answers"][q]` and top-level shapes.
- [ ] Tests (mocked `requests.post`): both response shapes parse; HTTP 500, timeout, missing `choice`, missing `none_of_these`-less probabilities -> `JevError`; key read from env, absent key -> `JevError`; `Authorization` header set.
- [ ] Implement, run, commit.

### Task 3: Indicators and candidate rules
**Files:** Create `indicators.py`, `rules.py`; Test `tests/test_rules.py`
**Interfaces:** Produces `sma, rsi, atr`; `RULES: dict[str, Callable[[DataFrame], Series[int]]]` mapping candidate name to target-position series (1 long, 0 flat) for candidates trend_ma, rsi2, breakout20, sector_rotation, dual_momentum, leveraged_trend.
- [ ] Tests on synthetic series: uptrend -> trend_ma long, downtrend -> flat; RSI(2) bounds 0-100; no lookahead (signal at t uses data <= t, applied at t+1).
- [ ] Implement, run, commit.

### Task 4: Backtest engine and filter
**Files:** Create `backtest.py`; Test `tests/test_backtest.py`
**Interfaces:** Produces `run(prices, positions, cost_bps) -> Result(equity, trades, max_drawdown, profit_factor, win_rate, n_trades)`, `evaluate(result, caps) -> (bool, reasons)`, `split(df, frac=0.6)`.
- [ ] Tests: buy-and-hold on a known series gives exact return; flat data -> n_trades 0, rejected, no ZeroDivision; costs reduce return; filter rejects <30 trades / drawdown over cap.
- [ ] Implement, run, commit.

### Task 5: strategy.md load/write
**Files:** Create `strategy.py`; Test `tests/test_strategy.py`
**Interfaces:** Produces `Strategy` dataclass, `load(path)`, `render(strategy, stats) -> str`; validation rejects thresholds outside (0,1), missing sections.
- [ ] Tests: round trip; invalid threshold raises; leveraged flag lowers position cap.
- [ ] Implement, run, commit.

### Task 6: Risk, kill switch, approval
**Files:** Create `risk.py`; Test `tests/test_risk.py`
**Interfaces:** Consumes `Db`, `Config`. Produces `Risk.check(order, account) -> Decision(allow|needs_approval|block, reason)`, `Risk.trip(reason)`, `Risk.reset()`, `Risk.record_broker_error()`, `Approvals.request/resolve/expire`.
- [ ] Tests: each kill trigger trips (drawdown 8%, 2 daily-limit days, 3 broker errors, stale data); tripped blocks everything incl. approved; over-size -> needs_approval; exposure and position caps; expiry = reject; unknown chat ID ignored.
- [ ] Implement, run, commit.

### Task 7: Signal gate
**Files:** Create `signal.py`; Test `tests/test_signal.py`
**Interfaces:** Consumes Jev client, Strategy. Produces `gate(rule_direction, state, strategy) -> SignalResult(fire, probabilities, model, reason)`.
- [ ] Tests (Jev mocked): all clear -> fire; one below threshold -> no; regime/headline disagree with direction -> no; JevError -> no fire.
- [ ] Implement, run, commit.

### Task 8: Broker and data adapters
**Files:** Create `broker.py`, `data.py`; Tests `tests/test_broker.py`, `tests/test_data.py`
**Interfaces:** Produces `Broker.account()`, `.positions()`, `.bracket_order(symbol, qty, stop, take_profit)`; `data.bars(symbols, start, end) -> DataFrame` (Alpaca, with CSV/yfinance-style fallback loader for backtests).
- [ ] Tests (mocked HTTP): non-paper URL refused; order JSON has bracket legs; broker errors recorded; stale bars detected.
- [ ] Implement, run, commit.

### Task 9: Alerts and Telegram commands
**Files:** Create `alerts.py`; Test `tests/test_alerts.py`
**Interfaces:** Produces `Alerts.send(text)`, `Alerts.poll(handler)` handling `/status /kill /approve /reject` only from allow-listed chat.
- [ ] Tests (mocked HTTP): foreign chat ignored; `/kill` trips switch; `/approve` resolves pending.
- [ ] Implement, run, commit.

### Task 10: Runner
**Files:** Create `runner.py`, `cli.py`; Test `tests/test_runner.py`
**Interfaces:** Produces `tick(deps)` orchestrating data -> rules -> gate -> risk -> approval -> broker -> alerts -> db; CLI `jevbot backtest|run|report|kill|reset`.
- [ ] Tests with fakes: end-to-end fire; blocked when kill tripped; approval-required path; Jev failure path; all events logged.
- [ ] Implement, run, commit.

### Task 11: Dashboard and daily report
**Files:** Create `dashboard.py`, `report.py`; Tests `tests/test_dashboard.py`, `tests/test_report.py`
**Interfaces:** FastAPI app with login (password env), pages for signals/probabilities/results, equity, kill state; `report.daily(db) -> str` (trades, P&L, win rate, largest loss, Jev avg latency and cost).
- [ ] Tests: unauthenticated -> 401/redirect; report math on fixture data.
- [ ] Implement, run, commit.

### Task 12: Backtest run, strategy.md, deployment, docs
**Files:** Create `Dockerfile`, `docker-compose.yml`, `README.md`, `strategy.md`, `docs/WHAT_COULD_BLOW_UP.md`
- [ ] Fetch >= 5 years of daily data, run all candidates through the filter, write the winner to `strategy.md` (or document honestly if none pass).
- [ ] Write Pi deployment walkthrough (Docker, restart policy, Telegram BotFather, healthcheck ping) and the final-check doc ending "WHAT COULD BLOW UP THIS ACCOUNT?".
- [ ] Run full `pytest`, commit, push.

## Self-review

Spec coverage: sections 3-9 map to Tasks 1-12. Types: `Db`, `Config`, `Strategy`, `Risk`, `JevResult` names are used identically across tasks. No placeholders: test cases are enumerated per task; code is written test-first during execution.
