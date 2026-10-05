# What day 6 writes, and what day 7 reads

Day 6 writes three files, all in `~/quant`, all plain and readable. Nothing that
counts as the member's evidence lives in a hidden folder.

| File | For |
|---|---|
| `day6-run-log.txt` | the member. Human lines, one per event. This is the screenshot. |
| `run-log.jsonl` | machines. One JSON object per line, append only. |
| `run-state.json` | machines and the curious member. Current state plus a `proved` block. |

There is also `~/quant/.cache/day6-bars/`, which holds downloaded price bars.
It is derived data, holds no evidence, and is safe to delete.

Everything in both machine files carries `"paper": true`, `"simulated": true`
and `"live": false`. Nothing in day 6 ever touches a broker.

---

## `run-state.json`

```json
{
  "schema": "ptq-academy/day6-run-state/1",
  "day": 6,
  "paper_only": true,
  "live_trading": false,
  "broker_connected": false,
  "updated_at": "2026-09-03T13:13:28+01:00",

  "kill_switch": {
    "engaged": true,
    "engaged_at": "2026-09-03T13:13:22+01:00",
    "engaged_by": "lewis",
    "reason": "proving I can stop it"
  },

  "loop": {
    "running": false,
    "pid": null,
    "kind": "background-process",
    "every_seconds": 15,
    "started_at": "2026-09-03T13:13:10+01:00",
    "stopped_at": "2026-09-03T13:13:22+01:00",
    "last_cycle_at": "2026-09-03T13:13:28+01:00",
    "cycle_count": 4,
    "cycles_after_kill": 1
  },

  "arm": {
    "name": "Pullback Fifty",
    "symbol": "SPY",
    "spec_path": "/Users/lewis/quant/strategy.json",
    "spec_fingerprint": "240905e00627...",
    "armed_at": "2026-09-03T13:13:04+01:00",
    "armed_by": "lewis",
    "expires_at": "2026-09-03T14:13:04+01:00",
    "mode": "paper",
    "day5_headline": "FAIL",
    "scope": { "symbols": ["SPY"], "max_notional": 100000.0, "max_open_positions": 1 }
  },

  "position": null,
  "last_exit_bar": "2026-09-02",
  "cycles": [ { "n": 1, "at": "...", "outcome": "opened", "detail": "BUY 79 SPY at 762.9075 (simulated)" } ],
  "fills":  [ { "...": "the same rows as the open/close events in run-log.jsonl" } ],

  "proved": {
    "ran_on_a_schedule": true,
    "cycles_before_kill": 3,
    "stopped_by_member": true,
    "refused_after_kill": true
  },

  "paths": { "human_log": "...", "journal": "...", "state": "..." },
  "honesty": "Every fill recorded here is simulated arithmetic on a historical price bar..."
}
```

### The four fields day 7 should read

- `loop.cycle_count` — how many cycles ran in total
- `kill_switch.engaged` — whether it is halted right now
- `kill_switch.engaged_at` — when the member stopped it
- `proved` — the whole point of the day, pre-computed

`proved.ran_on_a_schedule` is true once the loop was started and at least one
cycle ran. `proved.stopped_by_member` is true once the kill switch was engaged.
`proved.refused_after_kill` is true once at least one cycle ran AFTER the kill
and refused to act, which is the part that actually proves the switch holds.

`arm` is `null` when nothing is armed. `position` is `null` when flat. A
non-null `position` is a SIMULATED paper position and nothing more.

### Note on timestamps

All timestamps in `run-state.json` and `run-log.jsonl` are ISO 8601 strings with
an offset, not epoch numbers, so the member can read the file themselves.
`kill_switch.engaged_at` is therefore a string. Parse with
`datetime.fromisoformat`.

### `cycles[].outcome`

One of: `opened`, `closed`, `held-noop`, `flat-noop`, `cooldown`, `refused`,
`aborted`, `skipped`, `no-data`, `insufficient`, `expired`, `stale-halt`, `idle`.

`skipped` means the kill switch was engaged. `aborted` means it tripped
mid-cycle, after the risk check and before the fill was written. `refused` means
the risk choke said no. None of these are errors. They are the loop working.

---

## `run-log.jsonl`

One JSON object per line, appended, never rewritten. Every line carries `at`,
`event`, `paper`, `simulated`, `live`.

Events: `armed`, `disarmed`, `loop-start`, `open`, `close`, `refused`,
`no-data`, `skipped`, `stale-halt`, `expired`, `aborted-by-kill-switch`,
`kill-switch-engaged`, `kill-switch-released`.

An `open` fill:

```json
{"at": "2026-09-03T13:13:10+01:00", "cycle": 1, "event": "open",
 "side": "long", "symbol": "SPY", "qty": 79,
 "fill_price": 762.9074822143555, "price_before_costs": 762.4500122070312,
 "cost_bps_per_side": 6.0, "signal_bar": "2026-09-01", "fill_bar": "2026-09-02",
 "fill_rule": "next_bar_open", "stop_price": 750.3040157251367,
 "note": "SIMULATED paper fill. No order was sent to any venue.",
 "paper": true, "simulated": true, "live": false}
```

A `close` fill:

```json
{"at": "...", "cycle": 6, "event": "close", "symbol": "SPY", "qty": 79,
 "entry_price": 762.9074822143555, "exit_price": 749.8538,
 "exit_before_costs": 750.3040157251367, "cost_bps_per_side": 6.0,
 "reason": "stop hit (intrabar low 399 against stop 750.304)",
 "bars_held": 0, "simulated_pnl": -1031.24,
 "note": "SIMULATED paper close. No order was sent to any venue.",
 "paper": true, "simulated": true, "live": false}
```

The kill event, which day 7 looks for by name:

```json
{"at": "2026-09-03T13:13:22+01:00", "event": "kill-switch-engaged",
 "by": "lewis", "reason": "proving I can stop it",
 "paper": true, "simulated": true, "live": false}
```

`simulated_pnl` is arithmetic on historical bars with the spec's cost in
basis points charged on both sides. It is not a return, not a performance
figure, and must never be presented as one.

---

## What day 6 reads

| File | Purpose | If missing |
|---|---|---|
| `~/quant/strategy.json` | the strategy. Day 3's `spec_version: 1` schema. | refuses to arm, names the file, sends them to day 3 |
| `~/quant/verdict.json` | day 5's judgement, for context only | ignored silently |

From `verdict.json` day 6 reads only `strategyName` and `headline`. If the name
matches the spec and the headline is `FAIL`, arming prints a line saying the rule
failed its checks and that automating it on paper is still the drill, not a green
light. Nothing is blocked either way.
