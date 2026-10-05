# What day 5 reads, and what it writes

## Exact input binding

Day 4 now saves `inputIdentity` with version 1, `specSha256` and `barsSha256`.
Day 5 requires these identities before testing. Changed settings, revised prices,
or new bars stop the run and request Day 4 again. Legacy results without identity
also need a Day 4 rerun. Column and JSON-key ordering do not change identity.
This is a consistency check, not proof of data accuracy or future performance.

Workspace is `~/quant`. Day 1 creates it, the member starts Claude there, every
day reads and writes inside it. Override with `--input <folder>` or
`PTQ_QUANT_HOME`.

```
~/quant/
  strategy.json            day 3   <- day 5 reads (builds the grid from it)
  backtest-result.json     day 4   <- day 5 reads
  verdict.json             day 5   -> day 7 reads
  verdict-card.txt         day 5      the thing the member posts
```

Day 4 currently writes `backtest.json`. Day 5 looks for `backtest-result.json`
first and falls back to `backtest.json`, so it works either way.

---

## Reads: `~/quant/backtest-result.json`

Written by `day-4/skill/scripts/run_backtest.py`. Day 5 uses these fields and
stops honestly if any are missing.

| Field | Used for |
|---|---|
| `strategy.name` | the card |
| `strategy.direction`, `strategy.entryPlain` | carried into the verdict |
| `data.symbol`, `data.bar` | reloading the same price data |
| `data.assetClass`, `data.session` | periods-per-year, via day 4's own function |
| `data.ppy` | used directly when present |
| `data.bars` | compared against the current bar count; a difference is reported, not hidden |
| `data.costBps` | **must be non-zero.** Day 5 refuses to judge a frictionless curve. |
| `metrics.trades` | carried into the verdict for day 7 |

`data.provider` and `data.adjustment` are also written by day 4. Day 5 does not
read them, and does not need to: it reloads the bars by importing day 4's
`fetch_bars`, which goes through the same shared data layer and the same cache,
so it gets the same bars from the same source without spending another request.

## Reads: `~/quant/strategy.json`

Day 3's spec, validated against `day-3/skill/schema.json`. Day 5 needs it
because the trial grid is built by varying the member's own rule. The
workspace's own copy wins over the `specPath` recorded in the backtest result,
so a moved or copied folder is judged on the spec sitting in it.

---

## The bit that matters: day 5 builds the trial grid

Day 4 runs one configuration and saves one equity curve. Two of the three
checks cannot be computed from one curve — they measure the **search**, not the
curve:

- **PBO** ranks the in-sample winner against the alternatives out-of-sample.
  With one column there is nothing to rank.
- **The deflated Sharpe** deflates against the spread of trial Sharpes. With one
  column there is no spread, so it degrades to the probabilistic Sharpe against
  zero — a weaker and different claim. The card relabels the gate when this
  happens so nobody is misled.

A day-3 spec is an arbitrary condition tree, so there is no fixed parameter grid
to sweep. The engine's answer for exactly this case (`composed.py`,
`composed_variants`) is **length-scaled variants of the member's own tree**: the
same rule with every lookback stretched and squeezed.

```
LENGTH_SCALES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
```

Every operand's `params.period` in the entry and exit conditions is scaled and
clamped to the range day 3's validator allows for that series: [2, 400], or
[1, 400] for `pct_change`. Variants are deduped.

The member's own spec is never scaled or clamped. It goes in exactly as
written, labelled `your settings`, and a scaled variant that lands on the same
rule is dropped in its favour. That is the column judged, so PBO and DSR judge
the configuration they actually run.

Short lookbacks give a small family. `pct_change(period=1)` on its own gives
two versions (1 and 2). A rule with no lookback at all gives one, and PBO then
reports "could not run" because there is nothing to compare. The other two
checks still run. Day 5 never adds a made-up variant to fill the family out.

Each variant is simulated by **day 4's own `run_backtest.py`**, imported at run
time. One simulator, so the two days can never disagree and day 5 never
re-derives the trade logic. Same 6 bps per side on every column.

A variant that never triggers a trade is dropped rather than counted as a flat
trial — a flat column would give PBO a fake thing to rank. The card says how
many were dropped. If the member's *own* settings never trigger, day 5 stops.

If day 4's `run_backtest.py` cannot be found, day 5 stops. It will not simulate
with different code and call the result theirs. Override with `--day4 <path>`.

---

## Writes: `~/quant/verdict.json`

`schema: "ptq-academy/verdict/1"`. Day 7 reads this to build the week-one card.

```json
{
  "schema": "ptq-academy/verdict/1",
  "createdAt": "2026-09-03T11:04:22.913Z",
  "day": 5,

  "strategyName": "Pullback Fifty",
  "asset": "AAPL",
  "bar": "1d",
  "assetClass": "equity",
  "periodsPerYear": 252.0,
  "direction": "long",
  "entryPlain": "Close above the 50-day average for two days in a row...",
  "bars": 4192,
  "trades": 96,
  "configurationsTried": 6,
  "yourConfiguration": "your settings",
  "trialLabels": ["lookbacks x0.5", "lookbacks x0.75", "your settings", "..."],
  "costBps": 6.0,

  "headline": "FAIL",
  "engineVerdict": "reduce",
  "failedGates": ["dsr"],

  "gates": [
    {
      "key": "pbo",
      "label": "Overfitting (PBO, CSCV)",
      "value": "11%",
      "detail": "needs to be under 50%",
      "pass": true,
      "ran": true
    }
  ],

  "pboPct": 11.4,
  "deflatedSharpe": 0.52,
  "expectedMaxSharpe": 0.0188,
  "walkForward": {
    "scheme": "anchored", "folds": 5, "run": true,
    "oosSharpe": 0.72, "efficiency": 0.99, "perFold": [], "note": "..."
  },

  "methodology": { "cscvSplits": 8, "cscvCombinations": 70, "purging": false, "thresholds": {}, "papers": [] }
}
```

### For day 7

Stable fields, safe to bind to:

- `headline` — `"PASS"` or `"FAIL"`. Binary. Use this one.
- `failedGates` — `[]`, or some of `"pbo"`, `"dsr"`, `"wf"`.
- `engineVerdict` — `approve` / `reduce` / `block`, the engine's own three-state
  word on its own rule: 0 failures approve, 1 reduce, 2 or more block.
- `gates[]` — render these rows directly. Gate keys are stable: `pbo`, `dsr`,
  `wf`. Each row carries `label`, `value`, `detail`, `pass`, `ran`.
- `configurationsTried`, `pboPct`, `deflatedSharpe`, `walkForward`.

**"Could not run" is distinct from "failed", and the distinction is explicit:**

| Situation | `gates[].ran` | `gates[].pass` | number field |
|---|---|---|---|
| computed, held | `true` | `true` | the value |
| computed, did not hold | `true` | `false` | the value |
| could not be computed | `false` | `false` | `null` |

- PBO uncomputable: `pboPct` is `null`, the gate's `value` is `"could not run"`
  and its `detail` carries the reason.
- Sharpe uncomputable: `deflatedSharpe` is `null`, same pattern.
- Walk-forward uncomputable: `walkForward.run` is `false` and
  `walkForward.note` carries the reason.

An uncomputable check is never a pass, and `headline` is `"FAIL"` whenever any
gate did not hold **or** did not run. When *nothing* could be computed, every
gate has `"ran": false` — day 7 should say the data was too short to test
anything, not that the rule failed.

**Do not present a PASS as a result to act on.** Day 7's card must carry the
same line day 5 does: one clean result, one asset, one history, paper only.

---

## Writes: `~/quant/verdict-card.txt`

The rendered card plus the plain-English "what broke" section. This is what the
member screenshots and posts. Not machine-read by anything.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The checks ran. Card printed, files written. PASS or FAIL. |
| 2 | Stopped honestly. Nothing computed, nothing written, the reason printed. |

A non-zero exit is never a crash message. Every stop names what is missing and
what to do about it.
