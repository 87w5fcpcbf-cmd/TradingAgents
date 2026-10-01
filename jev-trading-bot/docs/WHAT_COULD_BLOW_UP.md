# Final check before anyone suggests live money

Answer these honestly with evidence from the dashboard and logs. If any answer is "no" or "not sure", stay on paper.

1. **Does paper match the backtest?** Compare weeks of paper results (win rate, profit factor, max drawdown, trade count) with the out-of-sample backtest. Paper fills use real quotes but no real slippage, so paper should look *better* than real life, not worse.
2. **Did the kill switch fire in testing?** Trip it with `/kill`, confirm no order goes out, restart the bot and confirm it stays tripped, then reset on the machine. Also test the approval timeout and a Jev outage (unset the key for one tick).
3. **Did Jev add value?** The logs hold every Jev probability next to the outcome. Compare trades Jev allowed against the rules alone. The backtest cannot test Jev (a model that has seen the past can look smarter on old headlines), so this paper evidence is the only evidence.
4. **What market regime would break this?** See below.

## WHAT COULD BLOW UP THIS ACCOUNT?

- **A regime the backtest never saw.** Five to seven years includes one bear market and one crash. A fast gap down (stops fill far below the stop price), a long sideways chop (trend and breakout rules get whipsawed), or a rate shock that breaks the stock-bond relationship (dual momentum's safe asset falls with stocks) are all plausible.
- **Overfitting disguised as discipline.** Picking the best of six candidates on the same out-of-sample window selects luck. The 40% hold-out is not a fresh test; only forward paper trading is.
- **The leveraged candidate.** TQQQ-style funds have historically fallen 40-70% peak to trough. Its position cap is 3% for that reason; it is never auto-selected by the backtest.
- **Jev being wrong with high confidence.** Probabilities are not guarantees, and calibration on this task is unmeasured. Thresholds of 0.80 are a starting guess. A feed that is stale, poisoned or misread is passed as data, but a wrong "bullish" still counts as one vote.
- **Silent failure of the machine.** Power or internet loss on the Pi: stops and targets live at Alpaca so open positions stay protected, but rule exits and new entries stop. The external heartbeat is what tells you.
- **A kill switch you cannot reach.** `/kill` needs Telegram and internet; `jevbot kill` needs the machine. Know your fastest path to flatten positions in the Alpaca dashboard.
- **Leaked credentials.** A Jev or Alpaca key pasted into chat or committed to git. Keys live only in `.env` (mode 600, gitignored); rotate anything that has been shared.
- **Human override at the worst moment.** Approving a large trade because the headline felt exciting is the most common way a gated bot loses money.
- **Software bugs.** 117 tests do not make a bug-free system. Nothing here has run against live services yet.
