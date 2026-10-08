# Week one review (Day 7) - Alki

Strategy reviewed: **Trend Sniper version 2** (SPY daily from 2000; buy after 2 closes above the 50-day average,
sell after a close below it, 3% fixed stop, 80% of a $100 imaginary account per trade, 6 bps per side).
Evidence files (all version 2, written in this order on 2026-10-06): strategy.json -> backtest-result.json ->
verdict.json -> run-state.json / run-log.jsonl. Version 1 (20% per trade) is kept separately in
trend-sniper-v1-20pct-3pct-stop/ and is not part of this review.

## What happened
- Day 2 idea: "Not decided yet". Day 3: member chose the labelled example "follow the trend".
- Day 4 backtest: ran on Tiingo SPY daily bars, 03 Jan 2000 to 06 Oct 2026, 183 trades.
- Day 5 verdict: FAIL (engine: reduce). PBO 40% pass, deflated Sharpe 0.89 fail, walk-forward OOS 0.61 pass. 6 configurations tried.
- Day 6 paper drill (version 2): cycles 11-17, 1 simulated paper buy, stopped by the member at 22:03:12
  (before the 22:03:54 expiry); cycle 17 refused by the kill switch.
- Note: the week-one card's "ran 17 times - 2 paper fills" counts the shared Day 6 log, which also holds the
  version 1 drill (cycles 1-10, 1 fill, ended by automatic expiry). Version 2 alone: 7 cycles, 1 fill.

## Prediction comparison
My prediction before testing (Day 2, unchanged): "i dont know"
Comparison: there is not enough evidence to judge this prediction. The historical backtest was positive after
modelled costs, but Day 5 could not separate the result from luck, so whether the rule works remains unknown.
A historical verdict is not permission to trade real money.

## In my own words
- What I learned: that buying bigger doesn't improve it
- Next step: I want to investigate whether the 3% stop is hurting the strategy
