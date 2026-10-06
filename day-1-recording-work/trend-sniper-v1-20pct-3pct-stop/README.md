# Trend Sniper version 1 (record, superseded)

Rules: SPY daily from 2000, buy after 2 closes above the 50-day average, sell after a close below it,
3% fixed stop, 20% of a $100 imaginary account per trade, 6 bps per side.

Results in this folder belong to version 1 only:
- Day 4 backtest: backtest-result.json / backtest-card.txt / day-4-card.png
- Day 5 verdict: verdict.json / verdict-card.txt / day-5-card.png (FAIL: deflated Sharpe)
- Day 6 paper drill: run-state.json / run-log.jsonl / day6-run-log.txt / day-6-card.png
  (loop ended by automatic expiry at 21:23:58; kill switch engaged afterwards at 21:25:00 and refused the next cycle)

On 2026-10-06, after seeing these results, the member changed sizing to 80% per trade (stop kept at 3%).
That is version 2, in the parent folder. Do not combine version 1 and version 2 results.
The Day 6 drill files left in the parent folder were produced with version 1 and are stale for version 2.
