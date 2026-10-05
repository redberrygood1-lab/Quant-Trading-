# PTQ Academy strategy spec — format and contract

**This file is the contract between day 3, day 4, day 5 and day 6.**
Day 3 writes it. Days 4, 5 and 6 read it. Nothing else in the academy defines strategy shape.

If you are building the day-4, day-5 or day-6 skill: read this file and `schema.json`, and use
`scripts/validate_spec.py` before you use the spec for anything. Do not re-interrogate the
member. Do not silently fill in missing fields.

---

## 1. Where the files live

Canonical member workspace: `~/quant/`

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `rule.md` | day 2 | day 3 | the plain-English rule, member's own words |
| `strategy.json` | day 3 | days 4, 5, 6 | **the machine contract.** Single source of truth |
| `STRATEGY.md` | day 3 | the member | the printed card, for reading and posting |

`strategy.json` is JSON, not YAML. Reason: every member's Claude can parse it with the Python
standard library on Mac and Windows with nothing installed. `STRATEGY.md` is display only.
Nothing parses it. If the two ever disagree, `strategy.json` wins and the member re-runs day 3.

Fallback search order if `~/quant/` is not there (some members set up day 1
elsewhere): `./strategy.json`, `~/quant/strategy.json`, `~/ptq/strategy.json`,
`~/Documents/part-time-quant/strategy.json`. If none exist, say so and stop. Never invent a spec.

---

## 2. Top-level shape

```json
{
  "spec_version": 1,
  "name": "Pullback Fifty",
  "created": "2026-09-03",
  "source_rule": "verbatim day-2 text, unedited",
  "instrument": { ... },
  "timeframe": { ... },
  "direction": "long",
  "entry": { ... },
  "exit": { ... },
  "stop": { ... },
  "sizing": { ... },
  "costs": { "per_side_bps": 6.0 },
  "vague_terms_resolved": [ ... ],
  "unresolved": [ ... ]
}
```

Every top-level key is required. `spec_version` is `1`.

### `unresolved` is a hard gate

`unresolved` is a list of strings. Each is a question the member could not or would not answer.

**Day 4 must refuse to run a backtest while `unresolved` is non-empty.** Print the unresolved
items and send them back to day 3. A spec with a hole in it produces a number that means
nothing, and the whole point of day 4 is that the number means something.

`vague_terms_resolved` is a list of `{"term": "...", "became": "..."}` — the audit trail of what
got killed. Days 4-6 may display it. Nothing depends on it.

---

## 3. `instrument`

```json
{
  "symbol": "AAPL",
  "asset_class": "equity",
  "data_source": "auto",
  "currency": "USD"
}
```

- `symbol` — exactly as the data source spells it. `EURUSD=X` not `EUR/USD`. `BTC-USD` not `BTC`.
  Day 3 must verify the symbol returns data before writing it.
- `asset_class` — one of `equity`, `etf`, `fx`, `crypto`, `futures`, `index`.
- `data_source` — normally `auto`, which means "use whichever price source this machine has a
  key for", in the order the shared data layer prefers them. Pin one on purpose with `tiingo`,
  `twelvedata`, `alpaca`, `polygon` or `yahoo`. Use `csv` to read a file instead, in which case
  an extra key `csv_path` is required and must point at a readable file with
  `date,open,high,low,close,volume` columns.
  `yfinance` and `stooq` are still accepted, because specs written before the shared data layer
  existed use them. Both now behave exactly as `auto`. Stooq's open endpoint stopped serving a
  price table in 2026 and Yahoo rate-limits hard, so neither is a source to depend on.
  Whichever source actually served the bars is printed on day 4's card, along with what the
  prices are adjusted for.
- `currency` — 3-letter code. Display only, no conversion is done anywhere.
- `second_symbol` — optional, and only with an `smt_divergence` condition (each needs the other).
  The market the divergence compares against, like `GBPUSD` for `EURUSD`. It comes from the same
  `data_source` and `asset_class` as `symbol`. With `data_source: "csv"` it also needs
  `second_csv_path`, its own file with the same bar size, dates and clock as `csv_path`. Day 4
  lines the two up on identical timestamps, never shifts or fills a bar, and refuses in plain
  words when the second market has no data, when under 90% of the bar times match, or when the
  two barely move together (usually two files on different clocks).

## 4. `timeframe`

```json
{
  "bar": "1d",
  "history_start": "2010-01-01",
  "session": "regular"
}
```

- `bar` — one of `1d`, `1wk`, `1h`, `30m`, `15m`, `5m`, `1m`. Intraday bars have short free history.
  `1m` only runs from the member's own CSV (`data_source: csv`); no free feed keeps enough of it.
  CSV times are read as UTC unless the file carries its own offset.
  Day 3 warns the member; day 4 enforces whatever the data source actually returns.
- `history_start` — `YYYY-MM-DD`. Day 4 splits this range; day 5 needs at least 12 months of
  out-of-sample left over, so day 3 pushes for the longest honest history available.
- `session` — `regular` or `24h`.

## 5. `direction`

One of `long` or `short`. Exactly two values in v1. Entry conditions are written from the side
being traded: if `short`, the conditions firing means open a short, and the stop sits above
entry rather than below.

There is no `both` in v1. A member who trades both sides picks one for week one; day 3 says so
and notes the other side as a later test. Two-sided specs are a v2 change and would bump
`spec_version`.

## 6. `entry` and `exit`

```json
"entry": {
  "plain_english": "close above the 50-day average for two days, after a 5% pullback",
  "combine": "all",
  "conditions": [ Condition, ... ],
  "fill": "next_bar_open",
  "max_open_positions": 1,
  "cooldown_bars": 0
}
```

```json
"exit": {
  "plain_english": "close back below the 50-day average, or 20 bars, whichever first",
  "combine": "any",
  "conditions": [ Condition, ... ],
  "time_stop_bars": 20,
  "target": null,
  "fill": "next_bar_open"
}
```

- `combine` — `all` (AND) or `any` (OR). Entry defaults to `all`, exit to `any`.
- `fill` — `next_bar_open` or `same_bar_close`. **Default and strong preference is
  `next_bar_open`.** A condition evaluated on a bar's close cannot be filled at that same close
  in real life; `same_bar_close` is only honest for an end-of-day process that trades the close
  and day 3 must say so out loud before allowing it.
- `cooldown_bars` — bars to wait after a flat before re-entering. 0 is fine.
- `time_stop_bars` — integer or `null`. Bars in the trade before a forced exit.
- `target` — `null`, or `{"type": "atr_multiple", "value": 3.0}`, or
  `{"type": "percent", "value": 8.0}`, or `{"type": "r_multiple", "value": 2.0}` (2 times the
  distance from entry to the stop, so "2R"; needs a stop).
- `exit.conditions` may be an empty list if `time_stop_bars` or `target` is set. `entry.conditions`
  may never be empty.

### Condition

```json
{
  "id": "e1",
  "plain_english": "close is above the 50-day simple moving average",
  "left":  { "series": "close",  "params": {},             "offset": 0 },
  "op": ">",
  "right": { "series": "sma",    "params": {"period": 50}, "offset": 0 },
  "persist_bars": 2
}
```

- `id` — unique within the spec. `e1, e2...` for entry, `x1, x2...` for exit.
- `plain_english` — one line, the member's own reading of the condition. Required, never blank.
- `left` / `right` — **Operand** (below).
- `op` — one of `>`, `>=`, `<`, `<=`, `==`, `crosses_above`, `crosses_below`.
  `==` is only valid when one side is `day_of_week` or `constant`.
- `persist_bars` — integer ≥ 1. The condition must be true for this many consecutive bars.
  Not valid with `crosses_above` / `crosses_below` (a cross is a single-bar event) — must be 1.

### Operand

```json
{ "series": "sma", "params": {"period": 50}, "offset": 0 }
```

- `offset` — bars back. `0` is the current bar, `1` is the previous bar. Integer ≥ 0.
- `series` — one of the allow-list below. `params` must match exactly: no missing keys, no extras.

| `series` | required `params` | notes |
|---|---|---|
| `open` `high` `low` `close` `volume` | none | raw bar fields |
| `constant` | `value` (number) | a plain number |
| `sma` `ema` | `period` (int ≥ 2) | on close |
| `rsi` | `period` (int ≥ 2) | Wilder |
| `atr` | `period` (int ≥ 2) | Simple rolling mean of true range; shared by the historical and paper exercises |
| `stdev` | `period` (int ≥ 2) | of close |
| `highest` `lowest` | `period` (int ≥ 2), `field` (`open`/`high`/`low`/`close`) | rolling extreme |
| `pct_change` | `period` (int ≥ 1) | close vs close N bars ago, in percent |
| `volume_sma` | `period` (int ≥ 2) | |
| `bb_upper` `bb_lower` | `period` (int ≥ 2), `mult` (number > 0) | close, stdev bands |
| `adx` | `period` (int ≥ 2) | |
| `vwap` | `period` (int ≥ 2) | rolling, not session |
| `day_of_week` | none | 0 = Monday, 4 = Friday |
| `pct_below_highest` | `period` (int ≥ 2), `field` | percent below the rolling high, positive number |
| `day_level` | `tz`, `day_start` ("HH:MM"), `days_back` (int ≥ 0), `level` (number) | price at `level`% of a daily candle's range built from the bars: 0 = low, 50 = midpoint, 100 = high, 61.8 = a fib level |
| `day_position` | `tz`, `day_start`, `days_back` | where close sits inside that daily range, 0-100. Above 50 is premium, below 50 is discount |
| `hour_of_day` | `tz` | the bar's open time in that time zone, in hours: 9.5 = 09:30. For session windows |
| `smt_divergence` | `side` (`bullish`/`bearish`), `period` (int ≥ 2), `within` (int ≥ 1), `taken_by` (`main`/`second`/`either`) | 1 or 0. SMT divergence against `instrument.second_symbol`, see below |
| `custom` | `formula` (string), `needs_review` (true) | see below |

**Daily candles from intraday bars.** `day_level` and `day_position` build a daily candle on the
member's own clock: `tz` is a time zone name (`America/New_York`, `Europe/London`, `UTC`) and
`day_start` is when that day opens (`00:00` for midnight, `17:00` for the New York FX close).
`days_back: 1` is yesterday's full candle, `2` the day before. `days_back: 0` is today so far:
the high and low up to and including the current bar, never the rest of the day, so there is
no lookahead. Examples:

- premium of yesterday: `day_position(days_back 1) > 50`
- price back in discount: `close < day_level(days_back 1, level 50)`
- sweep of yesterday's high: `high > day_level(days_back 1, level 100)`
- a fib retracement: `close <= day_level(days_back 1, level 38.2)`

**Session window:** two entry conditions, `hour_of_day >= 8` and `hour_of_day < 11`, with the
right side a `constant`.

**SMT divergence between two markets.** `smt_divergence` is 1 on a bar where one market takes out
its lowest low (`bullish`) or highest high (`bearish`) of the `period` bars before it and the
other market does not, and 0 otherwise. `taken_by: "main"` means `instrument.symbol` takes it out
and `second_symbol` holds; `"second"` is the reverse; `"either"` counts both. `within: 1` is this
bar only; `within: 10` keeps it at 1 for this bar and the 9 after the divergence, so it can sit
beside a fair value gap that forms a few bars later. Only bars up to the current one are read.
A bar with no second-market price is no divergence. Write it as `smt_divergence == 1`:

```json
{ "id": "e1", "plain_english": "EURUSD sweeps its 20-bar low and GBPUSD does not, in the last 10 bars",
  "left": { "series": "smt_divergence",
            "params": {"side": "bullish", "period": 20, "within": 10, "taken_by": "main"}, "offset": 0 },
  "op": "==", "right": { "series": "constant", "params": {"value": 1}, "offset": 0 },
  "persist_bars": 1 }
```

**Three-bar patterns use `offset`.** A bullish fair value gap is `low` (offset 0) `>` `high`
(offset 2): the current bar's low sits above the high two bars back. Bearish is `high` (offset 0)
`<` `low` (offset 2). An engulfing close is `close > high` (offset 1).

**`custom` is the escape hatch and it is not free.** If a member's idea does not fit the
allow-list, day 3 writes `custom` with a plain-language `formula` string and **also adds an entry
to `unresolved`**, which blocks day 4. That is deliberate: better a blocked spec than a backtest
of something the member did not mean. Day 3 should try hard to express the idea with the
allow-list first.

## 7. `stop`

```json
{
  "type": "atr_multiple",
  "value": 2.0,
  "atr_period": 14,
  "trailing": false,
  "intrabar": true
}
```

- `type` — `atr_multiple`, `percent`, `swing`, or `none`.
- `value` — number > 0. Multiple of ATR, or percent from entry. For `swing` it is a whole number
  of bars N ≥ 2: the stop goes at the lowest low (long) or highest high (short) of the last N bars
  up to the signal bar. If price is already through that point there is no trade. Ignored when
  `type` is `none`.
- `atr_period` — int ≥ 2, required when `type` is `atr_multiple`, otherwise `null`.
- `trailing` — boolean. `false` means the stop sits where it was placed at entry.
- `intrabar` — boolean. `true` means the stop is hit when the bar's low (long) or high (short)
  breaches it, filled at the stop price. `false` means it only checks on the close.

`type: "none"` is allowed but day 3 must say plainly that without a stop, position sizing has
nothing to size against, and `sizing.method` then has to be `fixed_fraction`.

## 8. `sizing`

```json
{
  "method": "risk_percent",
  "risk_per_trade_pct": 1.0,
  "fraction_pct": null,
  "starting_equity": 100000,
  "max_risk_open_pct": 1.0
}
```

- `method` — `risk_percent` (size so that a stop hit loses `risk_per_trade_pct` of equity) or
  `fixed_fraction` (put `fraction_pct` of equity into the position, regardless of stop).
- `risk_per_trade_pct` — number > 0 and ≤ 100, required for `risk_percent`, else `null`.
- `fraction_pct` — number > 0 and ≤ 100, required for `fixed_fraction`, else `null`.
- `starting_equity` — number > 0. Default 100000, matching the PTQ engine.
- `max_risk_open_pct` — number > 0. Total risk allowed across open positions.
- `confirm_over_cap` — optional, `true` or `false`. Leave it out unless the member, in their own
  words, confirms a size above the cap below.

**Risk cap.** Risk per trade is what a stopped-out trade loses, measured by stop distance so the
same trade gives the same answer however it is written: `risk_per_trade_pct` for `risk_percent`,
and `fraction_pct` x the distance to the stop for `fixed_fraction` (the whole `fraction_pct` when
there is no stop; an ATR stop uses the typical bar move; a swing stop is measured on day 6 when the
order is sized). Above 2% the validator warns and says, in plain words, what a run of losses does
to the account. Above 10% it refuses the spec unless `confirm_over_cap` is `true`. Separately, when
one position holds 100% of the account or more, it warns (never blocks) that a gap or a fast candle
can jump past the stop and lose far more than the stop suggests. Day 6's paper loop applies the
same cap and the same note.

**Stop distance check.** For `percent` and `atr_multiple` stops the validator compares the stop with
how far one bar typically moves (average true range as a % of price). With `--data` it measures
that from the member's own bars; otherwise it uses a rough figure for the market and bar size. A
stop more than 10 typical bars away almost never fires, and one under half a typical bar is
stopped out by noise: both get a warning with a suggested range of 1 to 4 typical bars. Warnings
never change the exit code.

## 9. `costs`

```json
{ "per_side_bps": 6.0 }
```

6 bps per side is the academy default and matches the PTQ engine (`COST_BPS = 6.0`,
spread + commission + slippage). On FX (`asset_class: fx`) the floor and default are 1 bp per
side: 6 bps on EURUSD is about 7 pips a side, many times what a major pair costs, while 1 bp is
about 1.1 pips a side, 2.2 a round trip, still above a typical retail spread. A member may raise
either. Day 3 refuses to go below the floor and says why: a backtest that pays less than real
trading pays is a backtest that lies to you.

---

## 10. Mapping to the PTQ engine

For anyone wiring this into `trading_os_engine`, the fields line up like this. This is
documentation, not something the academy skills do.

| Spec | Engine `Manifest` |
|---|---|
| `name` | `name` |
| `instrument.symbol` | `instrument` |
| `instrument.asset_class` | `assetClass` |
| `direction` | `direction` |
| `timeframe.bar` | `signalTimeframe` |
| `entry.conditions` | `entryLogic: "composed"` + `composed` tree |
| `stop.value` | `risk.stopAtr` |
| `sizing.risk_per_trade_pct` | `risk.riskPerTradePct` |
| `costs.per_side_bps` | `cost_bps` |

The academy spec is deliberately wider than the engine in one place (arbitrary condition trees
via the allow-list) and narrower in others (no mechanics, no signal stacks, no ensembles).

---

## 11. Versioning

`spec_version` is `1`. Any later change that removes a field or changes a meaning bumps it to
`2`, and days 4-6 must refuse a version they do not know rather than guess. Adding an optional
field with a documented default does not bump the version.
