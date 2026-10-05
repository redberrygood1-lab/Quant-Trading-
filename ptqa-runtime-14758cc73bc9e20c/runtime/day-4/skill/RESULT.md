# Day 4 result file — format and contract

**This file is the contract between day 4 and day 5.** Day 4 writes it. Day 5
reads it. It sits alongside day 3's `SCHEMA.md`, which defines the strategy
spec; nothing here redefines strategy shape.

The additive `inputIdentity` object binds the actual inputs: `version: 1`,
`specSha256` (canonical JSON) and `barsSha256` (all fetched simulator columns
and timestamps). Day 5 rejects a missing identity or changed input and asks
for Day 4 again. The result version remains 1; older readers ignore this field.

---

## 1. Where it lives

Canonical member workspace: `~/quant/`

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `strategy.json` | day 3 | days 4, 5, 6 | the strategy spec, schema v1 |
| `backtest-result.json` | day 4 | day 5 | **this file.** The base run |

Written next to whichever `strategy.json` was used, so an override of the spec
path moves the result with it. `--out` overrides.

Day 4 reads the spec at `~/quant/strategy.json`, falling back through
`~/part-time-quant/`, the current folder, `~/ptq/`, and
`~/Documents/part-time-quant/` — SCHEMA.md's fallback order with `~/quant`
promoted to first, which is where day 1 puts the member.

## 2. What day 4 refuses to write

`backtest-result.json` is written **only** when a real backtest ran on real bars. It
is never written on a refusal. If the file is not there, no test happened, and
day 5 should say so rather than reaching for anything else.

Day 4 refuses when: `unresolved` is non-empty, day 3's `validate_spec.py`
fails, `spec_version` is not 1, `max_open_positions` is above 1, a `custom`
condition survived into the spec, the feed has no data, fewer than 250 bars
came back, the rule's warm-up exceeds a fifth of the sample, the rule never
triggered, or sizing is by risk per trade with no stop to size against.

## 3. Shape

```json
{
  "result_version": 1,
  "createdAt": "2026-09-03T12:00:00+00:00",
  "specPath": "/Users/x/quant/strategy.json",
  "specName": "Pullback Fifty",
  "specCreated": "2026-09-03",
  "strategy": { ... },
  "data": { ... },
  "metrics": { ... },
  "context": { ... },
  "methodology": { ... },
  "warnings": [ ... ],
  "provenance": "ported-from-trading-os-engine",
  "equityCurve": [ ... ],
  "realizedEquityCurve": [ ... ],
  "tradePnl": [ ... ],
  "trades": [ ... ]
}
```

`result_version` is `1`. Day 5 should refuse a version it does not know rather
than guess.

### `strategy`

Display copies of what actually ran. `name`, `direction`, `entryPlain`,
`exitPlain` (the spec's `plain_english` lines), `stopHuman`, `targetHuman`,
`sizingHuman`, `fillHuman`. **Day 5 should re-read `strategy.json` for the
machine-readable rule** — these are for printing, not parsing.

### `data`

| Key | Meaning |
|---|---|
| `symbol` | what was actually tested, after any override |
| `assetClass` `currency` `session` | from the spec's instrument and timeframe |
| `bar` | the timeframe actually tested |
| `bars` | number of bars |
| `warmup` | bars consumed before the rule's first honest signal |
| `start` `end` | human dates |
| `startISO` `endISO` | `YYYY-MM-DD`, for day 5's holdout split |
| `source` | the line printed on the card, e.g. `Tiingo 1d bars`, `Twelve Data 1d bars (cached 2h ago)`, `your CSV at /path/to/file.csv` |
| `provider` | which source served the bars: `tiingo`, `twelvedata`, `alpaca`, `polygon`, `yahoo`, `csv` |
| `adjustment` | what the prices are adjusted for: `splits and dividends`, `splits`, `however you saved it`, `unknown` |
| `costBps` | per side, from the spec |
| `startEquity` | from the spec's sizing |
| `ppy` | bars per year used to annualise |
| `calendarNote` | the plain-English reason for that `ppy` |
| `historyStartClipped` | true when the data source could not go back as far as the spec asked (more than five days short) |

`provider` and `adjustment` were added when days 4-6 moved onto the shared data
layer. They are additive: a reader that ignores them still works, and day 5
does exactly that. **No API key is ever written to this file.**

**Day 5 needs `startISO`, `endISO`, `bars` and `warmup`** to cut an
out-of-sample slice that leaves the rule enough warm-up on both sides.

### `metrics`

Exactly the production engine's `compute_metrics` output: `sharpe`, `sortino`,
`maxDrawdownPct`, `realizedMaxDrawdownPct`, `winRatePct`, `profitFactor`,
`trades`, `netReturnPct`, `costDragPct`.

`sharpe` is the number day 5's deflated Sharpe deflates.

### `context`

`avgWin`, `avgLoss`, `bestTrade`, `worstTrade`, `longestLosingStreak`,
`timeInMarketPct`, `buyAndHoldPct`. Display only. Nothing depends on it.

### `methodology`

`markToMarket` (always true), `markPrice` (`close`), `costBooking`
(`trade-close`), `atr`, and a `note`. Day 5 should carry these through to its
own output so the two runs cannot be read as having used different rules.

### `warnings`

Strings, already written for the member. Thin data, short span, few trades, a
position open at the end, a clipped history start, and any ticker or timeframe
override typed on the day. **Day 5 should repeat these**, not swallow them — a
thin-data warning matters more after the validation than before it.

### `equityCurve` / `realizedEquityCurve`

`[{ "t": "YYYY-MM-DD HH:MM", "equity": number }]`, one entry per bar, not
downsampled. `equityCurve` is mark-to-market — open positions valued at each
bar's close. `realizedEquityCurve` moves only when a trade closes. Headline
metrics come from the mark-to-market one.

### `tradePnl`

`[number]`, one per **closed** trade, in order. A position still open at the
end contributes nothing here.

### `trades`

One object per trade: `entryIndex`, `entryTime`, `entryPrice`, `side`, `qty`,
`stop`, `target`, `exitIndex`, `exitTime`, `exitPrice`, `exitReason`, `pnl`.

`exitReason` is one of `stop`, `target`, `time`, `signal`. A trade still open
at the end of the data has `exitIndex`, `exitTime`, `exitPrice`, `exitReason`
and `pnl` all `null`. **It is never given an invented exit.** Day 5 must skip
it rather than fill it in.

## 4. Versioning

`result_version` is `1`. Removing a field or changing a meaning bumps it to
`2`, and day 5 must refuse a version it does not know. Adding a field with a
documented default does not bump it.
