# Strategy: sector_rotation

> Not financial advice. Paper trading only. Written by the backtester; edit the JSON block to change rules.

| Rule | Value |
|---|---|
| Symbols | XLK, XLF |
| Timeframe | 1D |
| Entry | top 3 of 11 sector ETFs by 6-month return (monthly), only while SPY > 200-day SMA |
| Exit | dropped from top 3 at month end, or SPY < 200-day SMA |
| Stop loss | 8.0% |
| Take profit | 30.0% |
| Leveraged | no |
| Backtest-validated | NO - unvalidated, run `jevbot backtest` |

Jev must clear every threshold before a trade fires:

- regime: >= 0.8
- headline: >= 0.8
- buying_pressure: >= 0.8

## Backtest (out-of-sample)

- note: EXAMPLE FORMAT ONLY. Not a backtest result. Run jevbot backtest on real data.

## Machine-readable rules

```json
{
  "name": "sector_rotation",
  "symbols": [
    "XLK",
    "XLF"
  ],
  "timeframe": "1D",
  "entry": "top 3 of 11 sector ETFs by 6-month return (monthly), only while SPY > 200-day SMA",
  "exit": "dropped from top 3 at month end, or SPY < 200-day SMA",
  "stop_loss_pct": 0.08,
  "take_profit_pct": 0.3,
  "jev_thresholds": {
    "regime": 0.8,
    "headline": 0.8,
    "buying_pressure": 0.8
  },
  "validated": false
}
```
