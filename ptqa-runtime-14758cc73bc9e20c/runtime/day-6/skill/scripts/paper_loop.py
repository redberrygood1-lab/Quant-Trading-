#!/usr/bin/env python3
"""
paper_loop.py — Part-Time Quant Academy, Day 6.

Runs the member's day-3 strategy spec on a real schedule, on PAPER ONLY, and
gives them a kill switch they can trip themselves and watch work.

PAPER ONLY, BY CONSTRUCTION
---------------------------
There is no broker code in this file. No API key is read. No order is sent
anywhere. Every fill is arithmetic on a price bar, and is labelled SIMULATED in
the log, in the journal, and on screen. It cannot place a real order because
the code to do that does not exist here.

WHAT IT DOES
------------
  setup     show which price source this machine will use. Installs nothing.
  arm       deliberate, expiring, fingerprinted consent for ONE strategy
  start     run cycles in the background on a timer
  cycle     run exactly one cycle, in front of you
  status    what is armed, what is running, what is halted
  kill      THE KILL SWITCH. Halts everything and stops the background loop.
  release   deliberately un-halt (a separate command, on purpose)
  log       print the run log, for screenshotting
  disarm    withdraw standing consent
  schedule-install / schedule-remove   optional OS-level timer (cron / schtasks)

SAFETY RAILS (the same shape as the real PTQ autopilot, in miniature)
---------------------------------------------------------------------
  - the kill switch is re-read from disk at the TOP of every cycle and AGAIN
    immediately before any fill is written, so tripping it mid-cycle stops it
  - consent EXPIRES. Past the expiry the loop halts itself
  - the spec is FINGERPRINTED at arm time. Edit the spec while armed and the
    loop stale-halts rather than run on consent you did not give
  - every intent goes through one risk choke. Nothing writes a fill around it
  - missing data, a stale price, or a spec it cannot read is a logged cycle
    that did nothing. It never invents a price and never invents a fill

SCOPE, DELIBERATELY
-------------------
One strategy. One position. One machine. No broker, no venue reconciliation,
no portfolio-level risk across positions, no hosted running.

Python 3.8+, standard library only. macOS, Linux and Windows.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

SPEC_VERSION_SUPPORTED = 1
STATE_SCHEMA = "ptq-academy/day6-run-state/1"

# --------------------------------------------------------------------------
# The workspace. Day 1 makes ~/quant and the member works there.
# --------------------------------------------------------------------------

QUANT_HOME = os.environ.get("PTQ_QUANT_HOME") or os.path.join(
    os.path.expanduser("~"), "quant"
)

SPEC_SEARCH = [
    os.path.join(QUANT_HOME, "strategy.json"),
    os.path.join(os.getcwd(), "strategy.json"),
    os.path.join(os.path.expanduser("~"), "part-time-quant", "strategy.json"),
    os.path.join(os.path.expanduser("~"), "ptq", "strategy.json"),
    os.path.join(
        os.path.expanduser("~"), "Documents", "part-time-quant", "strategy.json"
    ),
]

VERDICT_SEARCH = [
    os.path.join(QUANT_HOME, "verdict.json"),
    os.path.join(QUANT_HOME, "backtest", "verdict.json"),
    os.path.join(
        os.path.expanduser("~"), "ptq-academy", "work", "backtest", "verdict.json"
    ),
    os.path.join(os.getcwd(), "verdict.json"),
]

# Everything the member needs sits in the open, in ~/quant. Nothing that is
# evidence lives in a hidden folder. Day 7 reads run-state.json and run-log.jsonl.
LOG_PATH = os.path.join(QUANT_HOME, "day6-run-log.txt")   # the human log, for screenshots
STATE_PATH = os.path.join(QUANT_HOME, "run-state.json")   # loop + kill switch + arm
JOURNAL_PATH = os.path.join(QUANT_HOME, "run-log.jsonl")  # one JSON object per event

SCHEDULE_TAG = "# ptq-academy-day-6"
WIN_TASK_NAME = "PTQAcademyDay6PaperLoop"

MIN_INTERVAL_SECONDS = 5
# How old a cached bar may be before the loop asks the provider again. Short,
# because a loop wants a fresh bar. The shared data module holds the cache; it
# is derived data, safe to delete, and holds no evidence.
BARS_CACHE_SECONDS = 240
MAX_BAR_AGE_DAYS = 10
MAX_EVIDENCE_CYCLES = 200


def ensure_dirs() -> None:
    os.makedirs(QUANT_HOME, exist_ok=True)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def now_ts() -> float:
    return time.time()


def iso(ts):
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def stamp(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def ts_of(value):
    """Epoch seconds from an ISO string (or a number, for older files)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def human(value) -> str:
    """A readable clock time from whatever form the field is in."""
    t = ts_of(value)
    return stamp(t) if t is not None else "unknown"


def whoami() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown-user"


RISK_CAP_PCT = 10.0  # same cap as day 3's validate_spec.py


def planned_risk_pct(spec: dict, dist_pct=None):
    """% of the account a stopped-out trade loses: position size x distance to the stop.
    None when the stop distance is not known yet (ATR or swing stop before sizing)."""
    sizing, stop = spec["sizing"], spec["stop"]
    if sizing["method"] == "risk_percent":
        return float(sizing["risk_per_trade_pct"])
    f = float(sizing["fraction_pct"])
    if stop["type"] == "none":
        return f
    if stop["type"] == "percent":
        return f * float(stop["value"]) / 100.0
    return None if dist_pct is None else f * dist_pct / 100.0


def cap_message(stake: float) -> str:
    return (
        "a stopped-out trade would lose about %.1f%% of the account. The academy cap is %g%%. Five "
        "losing trades in a row at that size leave %.0f%% of the account. Lower it on day 3 (1 to 2%% "
        "is what most traders use), or confirm it there in your own words so day 3 records "
        "sizing.confirm_over_cap. Day 6 will not paper-trade it until then."
        % (stake, RISK_CAP_PCT, max(0.0, 1 - stake / 100.0) ** 5 * 100)
    )


GAP_NOTE = (" Note: this one trade holds %s%% of the account. A gap or a fast candle can jump past the "
            "stop and lose far more than the stop suggests.")


def die(msg: str, code: int = 2):
    print("\nSTOPPED: " + msg + "\n", file=sys.stderr)
    sys.exit(code)


def atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o644)  # so the member can just open and read it
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fmt_qty(q) -> str:
    return ("%.8f" % float(q)).rstrip("0").rstrip(".")


def units(x: float) -> float:
    # Fractional units, like the day-4 backtest: 1% risk on BTC is often 0.02 of a coin.
    return math.floor(max(x, 0.0) * 1e8) / 1e8


def fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

DEFAULT_STATE = {
    "version": 1,
    "paper_only": True,
    "kill_switch": {
        "engaged": False,
        "engaged_at": None,
        "engaged_by": None,
        "reason": None,
    },
    "loop": {
        "running": False,
        "pid": None,
        "kind": None,
        "every_seconds": 60,
        "started_at": None,
        "stopped_at": None,
        "last_cycle_at": None,
        "cycle_count": 0,
        "cycles_after_kill": 0,
    },
    "arm": None,
    "position": None,
    "last_exit_bar": None,
    "cycles": [],
    "fills": [],
}


def load_state() -> dict:
    ensure_dirs()
    if not os.path.exists(STATE_PATH):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            parsed = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        die(
            "the state file at %s could not be read (%s).\n"
            "Nothing has been changed. Move that file aside if you want a fresh "
            "start, then arm again." % (STATE_PATH, exc)
        )
    merged = json.loads(json.dumps(DEFAULT_STATE))
    for k, v in parsed.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    merged["paper_only"] = True
    return merged


def save_state(state: dict) -> None:
    """run-state.json is the published record. The member can open it, and day 7
    reads it. Timestamps are readable ISO strings, not epoch numbers."""
    state["schema"] = STATE_SCHEMA
    state["day"] = 6
    state["paper_only"] = True
    state["live_trading"] = False
    state["broker_connected"] = False
    state["updated_at"] = iso(now_ts())
    state["cycles"] = state.get("cycles", [])[-MAX_EVIDENCE_CYCLES:]

    loop, ks, cycles = state["loop"], state["kill_switch"], state.get("cycles", [])
    state["proved"] = {
        "ran_on_a_schedule": bool(loop.get("started_at")) and loop.get("cycle_count", 0) >= 1,
        "cycles_before_kill": len([c for c in cycles if c["outcome"] not in ("skipped", "idle")]),
        "stopped_by_member": bool(ks.get("engaged_at")),
        "refused_after_kill": loop.get("cycles_after_kill", 0) >= 1,
    }
    state["paths"] = {"human_log": LOG_PATH, "journal": JOURNAL_PATH, "state": STATE_PATH}
    state["honesty"] = (
        "Every fill recorded here is simulated arithmetic on a historical price bar. "
        "No broker was connected, no order was sent, no money moved. Nothing here is "
        "a performance claim."
    )
    atomic_write(STATE_PATH, json.dumps(state, indent=2, sort_keys=True))


def kill_engaged() -> bool:
    """Always re-read from disk. Never trust an in-memory copy."""
    return bool(load_state()["kill_switch"]["engaged"])


# --------------------------------------------------------------------------
# The run log. This is the artifact the member screenshots.
# --------------------------------------------------------------------------


def log(event: str, message: str, cycle=None, echo: bool = True) -> None:
    ensure_dirs()
    tag = ("CYCLE %-4s" % cycle) if cycle is not None else "         -"
    line = "%s  %s %-11s %s" % (stamp(now_ts()), tag, event, message)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if echo:
        print(line, flush=True)


def journal(row: dict) -> None:
    ensure_dirs()
    row = dict(row)
    row.setdefault("at", iso(now_ts()))
    row["paper"] = True
    row["simulated"] = True
    row["live"] = False
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# The spec — day 3's published contract, spec_version 1.
# See ~/ptq-academy/day-3/skill/schema.json and SCHEMA.md.
# --------------------------------------------------------------------------

SERIES_PARAMS = {
    "open": [],
    "high": [],
    "low": [],
    "close": [],
    "volume": [],
    "constant": ["value"],
    "sma": ["period"],
    "ema": ["period"],
    "rsi": ["period"],
    "atr": ["period"],
    "stdev": ["period"],
    "volume_sma": ["period"],
    "adx": ["period"],
    "vwap": ["period"],
    "pct_change": ["period"],
    "highest": ["period", "field"],
    "lowest": ["period", "field"],
    "bb_upper": ["period", "mult"],
    "bb_lower": ["period", "mult"],
    "pct_below_highest": ["period", "field"],
    "day_of_week": [],
    "day_level": ["tz", "day_start", "days_back", "level"],
    "day_position": ["tz", "day_start", "days_back"],
    "hour_of_day": ["tz"],
    "smt_divergence": ["side", "period", "within", "taken_by"],
    "custom": ["formula"],
}

OPS = (">", ">=", "<", "<=", "==", "crosses_above", "crosses_below")


def find_spec(explicit=None) -> str:
    if explicit:
        p = os.path.abspath(os.path.expanduser(explicit))
        if not os.path.exists(p):
            die("no spec file at %s." % p)
        return p
    for p in SPEC_SEARCH:
        if os.path.exists(p):
            return p
    die(
        "I could not find your strategy spec.\n"
        "It should be at %s, written by day 3.\n"
        "Run the day-3 skill first, or point me at the file with --spec <path>.\n"
        "I will not invent a spec." % os.path.join(QUANT_HOME, "strategy.json")
    )


def read_spec(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except json.JSONDecodeError as exc:
        die("%s is not valid JSON (%s)." % (path, exc))
    if not isinstance(spec, dict):
        die("%s should contain one strategy object." % path)

    v = spec.get("spec_version")
    if v != SPEC_VERSION_SUPPORTED:
        die(
            "this spec says spec_version %r and I only know version %d.\n"
            "I will not guess at a format I do not know. Re-run day 3."
            % (v, SPEC_VERSION_SUPPORTED)
        )

    missing = [
        k
        for k in (
            "name",
            "instrument",
            "timeframe",
            "direction",
            "entry",
            "exit",
            "stop",
            "sizing",
            "costs",
            "unresolved",
        )
        if k not in spec
    ]
    if missing:
        die(
            "your spec is missing: %s.\n"
            "That is day 3's job. Re-run the day-3 skill rather than filling it "
            "in by hand." % ", ".join(missing)
        )

    unresolved = spec.get("unresolved") or []
    if unresolved:
        die(
            "your spec still has %d unresolved question(s):\n  - %s\n"
            "Day 6 runs your rule unattended. Running a rule with a hole in it "
            "unattended is how you end up not knowing what it did. Go back to "
            "day 3, answer these, then come back."
            % (len(unresolved), "\n  - ".join(str(u) for u in unresolved))
        )

    if spec["direction"] not in ("long", "short"):
        die(
            "direction is %r. Day 6 runs one side. Pick long or short in day 3."
            % spec["direction"]
        )

    for block in ("entry", "exit"):
        b = spec[block]
        if b.get("combine") not in ("all", "any"):
            die(
                '%s.combine must be "all" or "any", got %r.' % (block, b.get("combine"))
            )
        if b.get("fill") not in ("next_bar_open", "same_bar_close"):
            die(
                "%s.fill must be next_bar_open or same_bar_close, got %r."
                % (block, b.get("fill"))
            )
        for cond in b.get("conditions") or []:
            check_condition(cond, block)

    if not (spec["entry"].get("conditions") or []):
        die("entry.conditions is empty. There is nothing to trigger on.")
    smt = any(
        (c.get(side) or {}).get("series") == "smt_divergence"
        for block in ("entry", "exit")
        for c in spec[block].get("conditions") or []
        for side in ("left", "right")
    )
    if smt and not (spec.get("instrument") or {}).get("second_symbol"):
        die(
            "your rule uses smt_divergence, which compares two markets, but "
            "instrument.second_symbol is missing. Add the second market in day 3."
        )

    stop = spec["stop"]
    if stop.get("type") not in ("atr_multiple", "percent", "swing", "none"):
        die(
            "stop.type must be atr_multiple, percent, swing or none, got %r."
            % stop.get("type")
        )
    if stop["type"] == "atr_multiple" and not stop.get("atr_period"):
        die("stop.type is atr_multiple but atr_period is missing.")
    if stop["type"] != "none" and not stop.get("value"):
        die("stop.type is %r but stop.value is missing." % stop["type"])

    sizing = spec["sizing"]
    if sizing.get("method") not in ("risk_percent", "fixed_fraction"):
        die(
            "sizing.method must be risk_percent or fixed_fraction, got %r."
            % sizing.get("method")
        )
    if sizing["method"] == "risk_percent":
        if not sizing.get("risk_per_trade_pct"):
            die("sizing.method is risk_percent but risk_per_trade_pct is missing.")
        if stop["type"] == "none":
            die(
                "sizing.method is risk_percent but there is no stop. There is nothing "
                "to size against. Day 3 should have caught this."
            )
    elif not sizing.get("fraction_pct"):
        die("sizing.method is fixed_fraction but fraction_pct is missing.")
    if not sizing.get("starting_equity"):
        die("sizing.starting_equity is missing.")
    # Same cap as day 3, measured by stop distance: a stopped-out trade losing more than 10% of the
    # account is refused unless the member confirmed it on day 3 (sizing.confirm_over_cap). ATR and
    # swing stops are measured when the order is sized (size_position).
    stake = planned_risk_pct(spec)
    if stake is not None and stake > RISK_CAP_PCT and sizing.get("confirm_over_cap") is not True:
        die(cap_message(stake))

    bps = (spec.get("costs") or {}).get("per_side_bps")
    # Same floors as day 3: 6 bps a side, 1 on FX (about 1.1 pips on EURUSD).
    floor = 1.0 if (spec.get("instrument") or {}).get("asset_class") == "fx" else 6.0
    if bps is None or float(bps) < floor:
        die(
            "costs.per_side_bps is %r. The academy floor for this market is %g per side. "
            "A paper run that pays less than real trading pays is a paper run that lies "
            "to you." % (bps, floor)
        )
    return spec


def check_condition(cond: dict, block: str) -> None:
    for k in ("left", "op", "right"):
        if k not in cond:
            die("a %s condition is missing %r: %r" % (block, k, cond))
    if cond["op"] not in OPS:
        die(
            "in %s, %r is not an operator I know. Allowed: %s"
            % (block, cond["op"], ", ".join(OPS))
        )
    pb = cond.get("persist_bars", 1)
    if not isinstance(pb, int) or pb < 1:
        die(
            "in %s, persist_bars must be a whole number of 1 or more, got %r."
            % (block, pb)
        )
    if cond["op"] in ("crosses_above", "crosses_below") and pb != 1:
        die(
            "in %s, condition %s uses %s with persist_bars %d. A cross happens on one "
            "bar. Fix it in day 3." % (block, cond.get("id", "?"), cond["op"], pb)
        )
    for slot in ("left", "right"):
        check_operand(cond[slot], block, cond.get("id", "?"), slot)


def check_operand(op: dict, block: str, cid: str, slot: str) -> None:
    if not isinstance(op, dict) or "series" not in op:
        die(
            "in %s condition %s, %s is not an operand object: %r"
            % (block, cid, slot, op)
        )
    series = op["series"]
    if series not in SERIES_PARAMS:
        die(
            "in %s condition %s, I do not know the series %r.\nDay 3's allow-list is: %s"
            % (block, cid, series, ", ".join(sorted(SERIES_PARAMS)))
        )
    if series == "custom":
        die(
            "condition %s in %s uses a 'custom' formula: %r\n"
            "I will not guess at what that means and then run it unattended. Day 3 is "
            "meant to put a custom formula into 'unresolved'. Go back and express it "
            "with the allow-list, or leave this strategy off the schedule."
            % (cid, block, (op.get("params") or {}).get("formula"))
        )
    params = op.get("params") or {}
    for need in SERIES_PARAMS[series]:
        if need not in params:
            die(
                "in %s condition %s, series %r needs a %r parameter."
                % (block, cid, series, need)
            )
    offset = op.get("offset", 0)
    if not isinstance(offset, int) or offset < 0:
        die(
            "in %s condition %s, offset must be 0 or more, got %r."
            % (block, cid, offset)
        )


# --------------------------------------------------------------------------
# Series maths. Wilder where day 3 says Wilder.
# --------------------------------------------------------------------------


def _sma(vals, n):
    out, total = [], 0.0
    for i, v in enumerate(vals):
        total += v
        if i >= n:
            total -= vals[i - n]
        out.append(total / n if i >= n - 1 else None)
    return out


def _ema(vals, n):
    out, k, prev = [], 2.0 / (n + 1.0), None
    for i, v in enumerate(vals):
        if i < n - 1:
            out.append(None)
            continue
        prev = sum(vals[: i + 1]) / (i + 1) if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def _stdev(vals, n):
    out = []
    for i in range(len(vals)):
        if i < n - 1 or n < 2:
            out.append(None)
            continue
        w = vals[i - n + 1 : i + 1]
        m = sum(w) / n
        out.append(math.sqrt(sum((x - m) ** 2 for x in w) / (n - 1)))
    return out


def _wilder(vals, n):
    """Wilder's smoothing over a list whose first entry may be None."""
    out = [None] * len(vals)
    if len(vals) <= n:
        return out
    seed = [v for v in vals[1 : n + 1] if v is not None]
    if len(seed) < n:
        return out
    prev = sum(seed) / n
    out[n] = prev
    for i in range(n + 1, len(vals)):
        v = vals[i] if vals[i] is not None else 0.0
        prev = (prev * (n - 1) + v) / n
        out[i] = prev
    return out


def _rsi(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [
        max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))
    ]
    ag = sum(gains[1 : n + 1]) / n
    al = sum(losses[1 : n + 1]) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def _true_range(bars):
    tr = [None]
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def _atr(bars, n):
    if not bars:
        return []
    values = _true_range(bars)
    if bars:
        values[0] = bars[0]["high"] - bars[0]["low"]
    return _sma(values, n)


def _adx(bars, n):
    if len(bars) < 2 * n + 2:
        return [None] * len(bars)
    plus_dm, minus_dm = [None], [None]
    for i in range(1, len(bars)):
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
    atr_s = _wilder(_true_range(bars), n)
    pdm_s = _wilder(plus_dm, n)
    mdm_s = _wilder(minus_dm, n)
    dx = [None] * len(bars)
    for i in range(len(bars)):
        if not atr_s[i] or pdm_s[i] is None or mdm_s[i] is None:
            continue
        pdi = 100.0 * pdm_s[i] / atr_s[i]
        mdi = 100.0 * mdm_s[i] / atr_s[i]
        dx[i] = 0.0 if (pdi + mdi) == 0 else 100.0 * abs(pdi - mdi) / (pdi + mdi)
    return _wilder([0.0 if v is None else v for v in dx], n)


def _rolling(vals, n, fn):
    return [
        fn(vals[i - n + 1 : i + 1]) if i >= n - 1 else None for i in range(len(vals))
    ]


def _bar_local(b: dict, tz: str):
    """The bar's open time on the member's clock. Bar dates are UTC."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # Python 3.8
        die("day_level, day_position and hour_of_day need Python 3.9 or newer.")
    raw = b["date"]
    fmt_ = "%Y-%m-%d %H:%M" if len(raw) > 10 else "%Y-%m-%d"
    try:
        zone = ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001
        die("the time zone %r in your spec is not one I know (%s)." % (tz, exc))
    return datetime.strptime(raw, fmt_).replace(tzinfo=timezone.utc).astimezone(zone)


def _day_range(bars: list, p: dict):
    """[(high, low) or None] per bar: the day `days_back` before the bar's own
    day. days_back 0 is today so far, up to and including the bar."""
    from datetime import timedelta

    hh, mm = (int(x) for x in str(p["day_start"]).split(":"))
    shift = timedelta(hours=hh, minutes=mm)
    keys = [(_bar_local(b, str(p["tz"])) - shift).date() for b in bars]
    back = int(p["days_back"])
    out = [None] * len(bars)
    if back == 0:
        hi = lo = None
        for i, b in enumerate(bars):
            if i == 0 or keys[i] != keys[i - 1]:
                hi, lo = b["high"], b["low"]
            else:
                hi, lo = max(hi, b["high"]), min(lo, b["low"])
            out[i] = (hi, lo)
        return out
    order, full = [], {}
    for k, b in zip(keys, bars):
        if k not in full:
            order.append(k)
            full[k] = (b["high"], b["low"])
        else:
            h, l = full[k]
            full[k] = (max(h, b["high"]), min(l, b["low"]))
    pos = {k: n for n, k in enumerate(order)}
    for i, k in enumerate(keys):
        j = pos[k] - back
        out[i] = full[order[j]] if j >= 0 else None
    return out


def _smt(bars: list, p: dict):
    """Day 4's smt_divergence, bar by bar: 1.0 where one market takes out its
    lowest low (bullish) or highest high (bearish) of the `period` bars before
    it and the other market does not, else 0.0; `within` widens it to this bar
    or the within-1 before. The second market's prices are on each bar as
    second_high / second_low (get_bars). A bar with no second-market price is no
    divergence; a missing one inside the lookback is skipped."""
    n, within = int(p["period"]), int(p["within"])
    bull = p["side"] == "bullish"
    f_main, f_second = ("low", "second_low") if bull else ("high", "second_high")
    cols = {f: [b.get(f) for b in bars] for f in (f_main, f_second)}

    def took(field, i):  # True took it out, False held, None no price
        col = cols[field]
        v, w = col[i], col[i - n : i]
        if None in w:
            w = [x for x in w if x is not None]
        if v is None or not w:
            return None
        return bool(v < min(w)) if bull else bool(v > max(w))

    hits = [None] * len(bars)
    for i in range(n, len(bars)):
        a, b = took(f_main, i), took(f_second, i)
        main, second = (a is True and b is False), (b is True and a is False)
        hit = {"main": main, "second": second, "either": main or second}[p["taken_by"]]
        hits[i] = 1.0 if hit else 0.0
    out = []
    for i in range(len(bars)):
        w = [h for h in hits[max(0, i - within + 1) : i + 1] if h is not None]
        out.append(max(w) if w else None)
    return out


def series_values(operand: dict, bars: list):
    """Full aligned series for one operand, plus a short label."""
    s = operand["series"]
    p = operand.get("params") or {}
    closes = [b["close"] for b in bars]

    if s in ("open", "high", "low", "close", "volume"):
        return [b[s] for b in bars], s
    if s == "constant":
        return [float(p["value"])] * len(bars), fmt(float(p["value"]))
    if s == "sma":
        return _sma(closes, int(p["period"])), "sma(%s)" % p["period"]
    if s == "ema":
        return _ema(closes, int(p["period"])), "ema(%s)" % p["period"]
    if s == "rsi":
        return _rsi(closes, int(p["period"])), "rsi(%s)" % p["period"]
    if s == "atr":
        return _atr(bars, int(p["period"])), "atr(%s)" % p["period"]
    if s == "stdev":
        return _stdev(closes, int(p["period"])), "stdev(%s)" % p["period"]
    if s == "volume_sma":
        return (
            _sma([b["volume"] for b in bars], int(p["period"])),
            "volume_sma(%s)" % p["period"],
        )
    if s == "adx":
        return _adx(bars, int(p["period"])), "adx(%s)" % p["period"]
    if s == "pct_change":
        n = int(p["period"])
        out = [None] * len(bars)
        for i in range(n, len(bars)):
            prev = closes[i - n]
            out[i] = None if prev == 0 else (closes[i] - prev) / prev * 100.0
        return out, "pct_change(%s)" % n
    if s in ("highest", "lowest"):
        n, field = int(p["period"]), str(p["field"])
        vals = [b[field] for b in bars]
        return _rolling(vals, n, max if s == "highest" else min), "%s(%s,%s)" % (
            s,
            n,
            field,
        )
    if s in ("bb_upper", "bb_lower"):
        n, mult = int(p["period"]), float(p["mult"])
        mid, sd = _sma(closes, n), _stdev(closes, n)
        sign = 1.0 if s == "bb_upper" else -1.0
        out = [
            None if (mid[i] is None or sd[i] is None) else mid[i] + sign * mult * sd[i]
            for i in range(len(bars))
        ]
        return out, "%s(%s,%s)" % (s, n, fmt(mult))
    if s == "vwap":
        n = int(p["period"])
        out = [None] * len(bars)
        for i in range(n - 1, len(bars)):
            w = bars[i - n + 1 : i + 1]
            vol = sum(b["volume"] for b in w)
            if vol <= 0:
                continue  # no volume means no honest vwap
            out[i] = (
                sum(
                    ((b["high"] + b["low"] + b["close"]) / 3.0) * b["volume"] for b in w
                )
                / vol
            )
        return out, "vwap(%s)" % n
    if s == "pct_below_highest":
        n, field = int(p["period"]), str(p["field"])
        hi = _rolling([b[field] for b in bars], n, max)
        out = [
            None if not hi[i] else (hi[i] - closes[i]) / hi[i] * 100.0
            for i in range(len(bars))
        ]
        return out, "pct_below_highest(%s,%s)" % (n, field)
    if s == "day_of_week":
        out = []
        for b in bars:
            try:
                out.append(
                    float(datetime.strptime(b["date"][:10], "%Y-%m-%d").weekday())
                )
            except ValueError:
                out.append(None)
        return out, "day_of_week"
    if s in ("day_level", "day_position"):
        out = []
        for i, r in enumerate(_day_range(bars, p)):
            if r is None:
                out.append(None)
                continue
            hi, lo = r
            if s == "day_level":
                out.append(lo + (hi - lo) * float(p["level"]) / 100.0)
            else:
                out.append(None if hi == lo else (closes[i] - lo) / (hi - lo) * 100.0)
        label = "%s(%s,%s,%s%s)" % (
            s, p["tz"], p["day_start"], p["days_back"],
            "," + fmt(float(p["level"])) if s == "day_level" else "",
        )
        return out, label
    if s == "hour_of_day":
        out = []
        for b in bars:
            lt = _bar_local(b, str(p["tz"]))
            out.append(lt.hour + lt.minute / 60.0)
        return out, "hour_of_day(%s)" % p["tz"]
    if s == "smt_divergence":
        return _smt(bars, p), "smt_divergence(%s,%s,%s,%s)" % (
            p["side"], p["period"], p["within"], p["taken_by"],
        )
    die(
        "series %r has no implementation. That is a bug in this script, not your spec."
        % s
    )


def operand_at(operand: dict, bars: list, i: int):
    vals, label = series_values(operand, bars)
    off = int(operand.get("offset", 0))
    j = i - off
    if j < 0 or j >= len(vals):
        return None, label + ("[%d]" % off if off else "")
    return vals[j], label + ("[%d]" % off if off else "")


def compare(op: str, a, b):
    if a is None or b is None:
        return None
    return {
        ">": a > b,
        ">=": a >= b,
        "<": a < b,
        "<=": a <= b,
        "==": abs(a - b) < 1e-12,
    }[op]


def eval_condition(cond: dict, bars: list, i: int):
    """(True / False / None, one readable line)."""
    op = cond["op"]
    lv, ll = operand_at(cond["left"], bars, i)
    rv, rl = operand_at(cond["right"], bars, i)
    head = "%s %s %s %s %s" % (ll, fmt(lv), op, rl, fmt(rv))

    if op in ("crosses_above", "crosses_below"):
        if i < 1:
            return None, head + "  (needs a previous bar)"
        pl, _ = operand_at(cond["left"], bars, i - 1)
        pr, _ = operand_at(cond["right"], bars, i - 1)
        if None in (lv, rv, pl, pr):
            return None, head + "  (not enough history)"
        if op == "crosses_above":
            return (lv > rv and pl <= pr), head
        return (lv < rv and pl >= pr), head

    persist = int(cond.get("persist_bars", 1))
    results = []
    for k in range(persist):
        a, _ = operand_at(cond["left"], bars, i - k)
        b, _ = operand_at(cond["right"], bars, i - k)
        results.append(compare(op, a, b))
    if any(r is None for r in results):
        return None, head + "  (not enough history)"
    return all(results), head + ("  (for %d bars)" % persist if persist > 1 else "")


def eval_block(block: dict, bars: list, i: int):
    conds = block.get("conditions") or []
    if not conds:
        return False, []
    results, lines = [], []
    for c in conds:
        r, line = eval_condition(c, bars, i)
        results.append(r)
        lines.append(
            "%-3s %-3s %s"
            % (c.get("id", "?"), "?" if r is None else ("yes" if r else "no"), line)
        )
    if any(r is None for r in results):
        return None, lines
    return (all(results) if block["combine"] == "all" else any(results)), lines


# --------------------------------------------------------------------------
# Price bars. Real data or an honest refusal. Never an invented number.
# --------------------------------------------------------------------------


def _load_ptq_data():
    """Find the one shared price-data module.

    Days 4, 5 and 6 all read bars through `ptq_data.py`, so the three days can
    never disagree about what a bar is or where it came from. A skill installs
    as its own folder, so day 4 and day 6 each ship a copy generated from
    `shared/ptq_data.py`; if more than one copy is on this machine the newest
    version wins, so a member who updated one day's skill and not another still
    gets one behaviour."""
    import importlib.util

    here = os.path.abspath(__file__)
    scripts = os.path.dirname(here)
    academy = os.path.dirname(os.path.dirname(os.path.dirname(scripts)))
    candidates = [
        os.path.join(scripts, "ptq_data.py"),
        os.path.join(academy, "shared", "ptq_data.py"),
        os.path.join(os.path.expanduser("~"), ".ptq-academy", "lib", "ptq_data.py"),
    ]
    for root in (
        os.path.join(os.path.expanduser("~"), ".claude", "skills"),
        os.path.join(os.path.expanduser("~"), "ptq-academy"),
    ):
        if os.path.isdir(root):
            for base, _dirs, files in os.walk(root):
                if "ptq_data.py" in files:
                    candidates.append(os.path.join(base, "ptq_data.py"))
                if len(candidates) > 12:
                    break

    best = None
    for c in candidates:
        if not os.path.isfile(c):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "ptq_data_%d" % abs(hash(c)), c
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:  # noqa: BLE001 - a broken copy is not the only copy
            continue
        if not hasattr(mod, "get_bars"):
            continue
        ver = tuple(
            int(x)
            for x in str(getattr(mod, "DATA_MODULE_VERSION", "0")).split(".")
            if x.isdigit()
        )
        if best is None or ver > best[0]:
            best = (ver, mod)
    return best[1] if best else None


_DATA = None


def data_layer():
    global _DATA
    if _DATA is None:
        _DATA = _load_ptq_data()
    return _DATA


def get_bars(spec: dict, explicit_csv=None):
    """(bars, source) or (None, honest reason). Never fabricates.

    Everything about providers, keys, caching and rate limits lives in the
    shared module. This turns its bars into the shape the loop already speaks:
    a date string, four prices and a volume, oldest first.
    """
    d = data_layer()
    if d is None:
        return None, (
            "I cannot find ptq_data.py, the shared price-data module. It should "
            "sit next to this script. Reinstall the day-6 skill folder."
        )

    inst = spec["instrument"]
    bar = spec["timeframe"]["bar"]
    source = inst.get("data_source", "auto")
    csv_path = explicit_csv or (inst.get("csv_path") if source == "csv" else None)

    try:
        bars, meta = d.get_bars(
            str(inst["symbol"]),
            bar,
            spec["timeframe"].get("history_start", "2010-01-01"),
            data_source="csv" if csv_path else source,
            csv_path=csv_path,
            asset_class=inst.get("asset_class", "equity"),
            # A loop wants a fresh bar, so the cache window is short. Stale is
            # allowed here and ONLY here, because a cycle that runs on an old
            # bar and says so out loud is better than a cycle that guesses --
            # and the staleness check downstream refuses to trade on it anyway.
            max_age=BARS_CACHE_SECONDS,
            allow_stale=True,
        )
    except Exception as exc:  # noqa: BLE001 - report anything, honestly
        return None, str(exc)

    out = []
    for b in bars:
        when = b["time"]
        out.append(
            {
                # Daily and weekly bars stay YYYY-MM-DD, which is what the log,
                # the journal and every existing state file expect. Intraday
                # bars keep their time, so two bars on the same day are two
                # bars rather than one overwriting the other.
                "date": when[:10] if bar in ("1d", "1wk") else when[:16].replace("T", " "),
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
            }
        )
    if len(out) < 5:
        return None, "only %d usable bars came back for %s" % (
            len(out),
            inst["symbol"],
        )
    if inst.get("second_symbol"):
        second_source, why = _attach_second(out, spec, source)
        if why:
            return None, why
        return out, "%s; %s: %s" % (meta["source"], inst["second_symbol"], second_source)
    return out, meta["source"]


SECOND_MATCH_MIN = 0.90  # same share as day 4


def _attach_second(bars: list, spec: dict, source: str):
    """(source, None) or (None, honest reason). Puts instrument.second_symbol's
    high and low on each bar with the same time, for smt_divergence. Same price
    source and asset class as the main symbol; its own CSV when the spec reads
    CSVs. Nothing is shifted or filled forward."""
    inst = spec["instrument"]
    bar = spec["timeframe"]["bar"]
    second = str(inst["second_symbol"])
    csv_path = inst.get("second_csv_path") if source == "csv" else None
    if source == "csv" and not csv_path:
        return None, (
            "your rule compares %s with %s (SMT divergence), but the spec has no "
            "second_csv_path for %s" % (inst["symbol"], second, second)
        )
    try:
        raw, meta = data_layer().get_bars(
            second,
            bar,
            spec["timeframe"].get("history_start", "2010-01-01"),
            data_source="csv" if csv_path else source,
            csv_path=csv_path,
            asset_class=inst.get("asset_class", "equity"),
            max_age=BARS_CACHE_SECONDS,
            allow_stale=True,
        )
    except Exception as exc:  # noqa: BLE001 - report anything, honestly
        return None, "no prices for %s, the second market your SMT rule compares with: %s" % (
            second,
            exc,
        )
    when = (lambda t: t[:10]) if bar in ("1d", "1wk") else (lambda t: t[:16].replace("T", " "))
    by_date = {when(b["time"]): b for b in raw}
    for b in bars:
        m = by_date.get(b["date"])
        b["second_high"] = m["high"] if m else None
        b["second_low"] = m["low"] if m else None
    recent = bars[-500:]
    share = sum(b["second_low"] is not None for b in recent) / float(len(recent))
    if share < SECOND_MATCH_MIN:
        return None, (
            "%s's bars line up with only %d%% of %s's last %d bar times. SMT "
            "divergence compares the two bar by bar, so both need the same bar size, "
            "the same dates and the same clock (time zone)"
            % (second, int(share * 100), inst["symbol"], len(recent))
        )
    return meta["source"], None


# --------------------------------------------------------------------------
# Sizing and the risk choke. Every intent goes through here.
# --------------------------------------------------------------------------


def stop_distance(spec: dict, bars: list, i: int, price: float):
    stop = spec["stop"]
    if stop["type"] == "none":
        return None, "no stop in your spec"
    if stop["type"] == "percent":
        d = price * float(stop["value"]) / 100.0
        return d, "%s%% of %s" % (fmt(float(stop["value"])), fmt(price))
    if stop["type"] == "swing":
        n = int(stop["value"])
        if i < n - 1:
            return None, "not %d bars of history yet for the swing stop" % n
        window = bars[i - n + 1 : i + 1]
        if spec["direction"] == "long":
            level = min(b["low"] for b in window)
            d = price - level
        else:
            level = max(b["high"] for b in window)
            d = level - price
        if d <= 0:
            return None, "price is already through the %d-bar swing point %s" % (n, fmt(level))
        return d, "swing point of last %d bars at %s" % (n, fmt(level))
    atr_vals = _atr(bars, int(stop["atr_period"]))
    a = atr_vals[i] if 0 <= i < len(atr_vals) else None
    if not a or a <= 0:
        return None, "ATR(%s) is not available at this bar yet" % stop["atr_period"]
    return float(stop["value"]) * a, "%s x ATR(%s) = %s" % (
        fmt(float(stop["value"])),
        stop["atr_period"],
        fmt(a),
    )


def size_position(spec: dict, bars: list, i: int, price: float):
    """(qty, explanation). qty 0 with an honest reason is a valid answer."""
    qty, why = _size_position(spec, bars, i, price)
    if qty <= 0:
        return qty, why
    sizing = spec["sizing"]
    equity = float(sizing["starting_equity"])
    dist, _ = stop_distance(spec, bars, i, price) if spec["stop"]["type"] != "none" else (None, "")
    stake = (qty * dist / equity * 100.0) if dist else planned_risk_pct(spec)
    if stake is not None and stake > RISK_CAP_PCT and sizing.get("confirm_over_cap") is not True:
        return 0, "refused: " + cap_message(stake)
    pos = float(sizing["fraction_pct"]) if sizing["method"] == "fixed_fraction" else qty * price / equity * 100.0
    if pos >= 100.0:
        why += GAP_NOTE % fmt(pos)
    return qty, why


def _size_position(spec: dict, bars: list, i: int, price: float):
    sizing = spec["sizing"]
    equity = float(sizing["starting_equity"])
    if sizing["method"] == "fixed_fraction":
        budget = equity * float(sizing["fraction_pct"]) / 100.0
        return units(budget / price), "%s%% of %s paper equity = %s budget at %s" % (
            fmt(float(sizing["fraction_pct"])),
            fmt(equity),
            fmt(budget),
            fmt(price),
        )
    dist, how = stop_distance(spec, bars, i, price)
    if not dist or dist <= 0:
        return 0, "cannot size: " + how
    risk_cash = equity * float(sizing["risk_per_trade_pct"]) / 100.0
    return units(risk_cash / dist), "risk %s%% of %s = %s, stop distance %s (%s)" % (
        fmt(float(sizing["risk_per_trade_pct"])),
        fmt(equity),
        fmt(risk_cash),
        fmt(dist),
        how,
    )


def bar_age_days(bar_date: str):
    try:
        return (datetime.now() - datetime.strptime(bar_date[:10], "%Y-%m-%d")).days
    except ValueError:
        return None


def risk_choke(
    state: dict, arm: dict, symbol: str, price: float, bar_date: str, qty: float
):
    """(ok, reason). One choke. Nothing writes a fill around it."""
    scope = arm["scope"]
    if symbol not in scope["symbols"]:
        return False, "%s is not in the armed scope %s" % (symbol, scope["symbols"])
    if not (price and price > 0 and math.isfinite(price)):
        return False, "the price handed to me (%r) is not a usable number" % price
    age = bar_age_days(bar_date)
    if age is None:
        return False, "cannot read the bar date %r" % bar_date
    if age > MAX_BAR_AGE_DAYS:
        return (
            False,
            "the newest bar is %d days old. Refusing to act on a stale price" % age,
        )
    if qty <= 0:
        return False, "size came out at 0 units. Nothing to do"
    notional = qty * price
    if notional > scope["max_notional"]:
        return False, "notional %s is over your armed cap of %s" % (
            fmt(notional),
            fmt(scope["max_notional"]),
        )
    if state.get("position"):
        return False, "already holding a position, and day 6 runs one at a time"
    return True, "notional %s within cap %s, newest bar %s is %d day(s) old" % (
        fmt(notional),
        fmt(scope["max_notional"]),
        bar_date,
        age,
    )


def cost_adjust(price: float, bps: float, buying: bool) -> float:
    return price * (1 + bps / 10000.0) if buying else price * (1 - bps / 10000.0)


# --------------------------------------------------------------------------
# One cycle
# --------------------------------------------------------------------------


def record_cycle(state: dict, n: int, outcome: str, detail: str) -> None:
    state["cycles"] = state.get("cycles", [])
    state["cycles"].append(
        {"n": n, "at": iso(now_ts()), "outcome": outcome, "detail": detail}
    )
    state["loop"]["cycle_count"] = n
    state["loop"]["last_cycle_at"] = iso(now_ts())
    if state["kill_switch"]["engaged"]:
        state["loop"]["cycles_after_kill"] = (
            state["loop"].get("cycles_after_kill", 0) + 1
        )
    save_state(state)


def run_cycle(explicit_csv=None) -> str:
    state = load_state()
    n = state["loop"]["cycle_count"] + 1

    # RAIL 1 — the kill switch, re-read from disk at the top of every cycle.
    if state["kill_switch"]["engaged"]:
        why = state["kill_switch"]["reason"] or "engaged by you"
        log(
            "SKIPPED",
            "kill switch engaged (%s). Nothing evaluated, nothing filled." % why,
            cycle=n,
        )
        journal({"cycle": n, "event": "skipped", "reason": "kill switch engaged"})
        record_cycle(state, n, "skipped", "kill switch engaged")
        return "skipped"

    arm = state.get("arm")
    if not arm:
        log("IDLE", "nothing is armed. Did nothing.", cycle=n)
        record_cycle(state, n, "idle", "nothing armed")
        return "idle"

    # RAIL 2 — consent expires.
    if now_ts() > (ts_of(arm["expires_at"]) or 0):
        log(
            "EXPIRED",
            "consent expired at %s. Disarmed. Nothing filled."
            % human(arm["expires_at"]),
            cycle=n,
        )
        journal({"cycle": n, "event": "expired", "strategy": arm["name"]})
        state["arm"] = None
        record_cycle(state, n, "expired", "standing consent expired")
        return "expired"

    # RAIL 3 — the spec cannot change underneath a standing consent.
    if not os.path.exists(arm["spec_path"]):
        log(
            "STALE-HALT",
            "the spec file has gone: %s. Disarmed." % arm["spec_path"],
            cycle=n,
        )
        state["arm"] = None
        record_cycle(state, n, "stale-halt", "spec file missing")
        return "stale"
    if sha256_file(arm["spec_path"]) != arm["spec_fingerprint"]:
        log(
            "STALE-HALT",
            "your spec changed since you armed it. The consent you gave was for the "
            "old one, so it no longer applies. Disarmed. Read it, then arm again.",
            cycle=n,
        )
        journal({"cycle": n, "event": "stale-halt", "strategy": arm["name"]})
        state["arm"] = None
        record_cycle(state, n, "stale-halt", "spec changed under a standing arm")
        return "stale"

    spec = read_spec(arm["spec_path"])
    symbol = str(spec["instrument"]["symbol"])
    bps = float(spec["costs"]["per_side_bps"])

    # RAIL 4 — data, or an honest empty cycle.
    bars, note = get_bars(spec, explicit_csv=explicit_csv)
    if bars is None:
        log("NO-DATA", note + ". Did nothing. No price was invented.", cycle=n)
        journal({"cycle": n, "event": "no-data", "reason": note})
        record_cycle(state, n, "no-data", note)
        return "no-data"

    latest = bars[-1]
    log(
        "DATA",
        "%d %s %s bars, newest %s close %s  [%s]"
        % (
            len(bars),
            symbol,
            spec["timeframe"]["bar"],
            latest["date"],
            fmt(latest["close"]),
            note,
        ),
        cycle=n,
    )

    if state.get("position"):
        return cycle_manage_position(state, spec, arm, bars, n, bps)
    return cycle_look_for_entry(state, spec, arm, bars, n, bps)


def cycle_look_for_entry(state, spec, arm, bars, n, bps) -> str:
    entry = spec["entry"]
    symbol = str(spec["instrument"]["symbol"])

    # next_bar_open means the signal is judged on the last CLOSED bar and paid at
    # the next bar's open. Both bars already exist, so there is no peeking ahead.
    if entry["fill"] == "next_bar_open":
        if len(bars) < 2:
            log("NO-SIGNAL", "only one bar. Nothing to judge yet.", cycle=n)
            record_cycle(state, n, "insufficient", "one bar")
            return "insufficient"
        sig_i, fill_price, fill_note = (
            len(bars) - 2,
            bars[-1]["open"],
            "next bar's open",
        )
    else:
        sig_i, fill_price, fill_note = (
            len(bars) - 1,
            bars[-1]["close"],
            "same bar's close",
        )

    cooldown = int(entry.get("cooldown_bars", 0) or 0)
    if cooldown and state.get("last_exit_bar"):
        idx = {b["date"]: i for i, b in enumerate(bars)}
        prev = idx.get(state["last_exit_bar"])
        if prev is not None and (sig_i - prev) < cooldown:
            log(
                "COOLDOWN",
                "%d of %d cooldown bars since the last exit. Not entering."
                % (sig_i - prev, cooldown),
                cycle=n,
            )
            record_cycle(state, n, "cooldown", "waiting out cooldown_bars")
            return "cooldown"

    fired, lines = eval_block(entry, bars, sig_i)
    for line in lines:
        log("SIGNAL", "entry " + line, cycle=n)

    if fired is None:
        log(
            "NO-SIGNAL",
            "not enough history to judge the entry rule yet. Did nothing.",
            cycle=n,
        )
        record_cycle(state, n, "insufficient", "not enough bars for the entry rule")
        return "insufficient"

    if not fired:
        log(
            "FLAT-NOOP",
            "flat, entry rule did not fire (combine=%s). Nothing to do."
            % entry["combine"],
            cycle=n,
        )
        record_cycle(state, n, "flat-noop", "entry did not fire")
        return "flat-noop"

    qty, how = size_position(spec, bars, sig_i, fill_price)
    log("SIZE", how + "  ->  %s unit(s)" % fmt_qty(qty), cycle=n)

    ok, reason = risk_choke(state, arm, symbol, fill_price, bars[-1]["date"], qty)
    if not ok:
        log(
            "REFUSED",
            "the risk choke refused it: %s. Nothing filled." % reason,
            cycle=n,
        )
        journal({"cycle": n, "event": "refused", "reason": reason, "symbol": symbol})
        record_cycle(state, n, "refused", reason)
        return "refused"
    log("RISK", "ok. " + reason, cycle=n)

    # RAIL 5 — re-read the kill switch immediately before writing a fill.
    if kill_engaged():
        log(
            "ABORTED",
            "kill switch tripped mid-cycle. The order was NOT filled.",
            cycle=n,
        )
        journal({"cycle": n, "event": "aborted-by-kill-switch"})
        record_cycle(state, n, "aborted", "kill switch tripped mid-cycle")
        return "aborted"

    buying = spec["direction"] == "long"
    paid = cost_adjust(fill_price, bps, buying)
    dist, _ = stop_distance(spec, bars, sig_i, paid)
    stop_price = (paid - dist if buying else paid + dist) if dist else None

    state["position"] = {
        "direction": spec["direction"],
        "symbol": symbol,
        "qty": qty,
        "entry_price": paid,
        "entry_price_before_costs": fill_price,
        "entry_bar": bars[-1]["date"],
        "signal_bar": bars[sig_i]["date"],
        "stop_price": stop_price,
        "bars_held": 0,
        "opened_at": iso(now_ts()),
        "simulated": True,
    }
    side = "BUY" if buying else "SELL SHORT"
    log(
        "FILL",
        "%s %s %s at %s (%s, %s bps per side applied)   *** SIMULATED PAPER FILL. "
        "No broker, no money, nothing was sent anywhere. ***"
        % (side, fmt_qty(qty), symbol, fmt(paid), fill_note, fmt(bps)),
        cycle=n,
    )
    if stop_price:
        log(
            "STOP",
            "stop sits at %s (%s)"
            % (
                fmt(stop_price),
                "trailing" if spec["stop"].get("trailing") else "fixed",
            ),
            cycle=n,
        )

    fill_row = {
        "cycle": n,
        "event": "open",
        "side": spec["direction"],
        "symbol": symbol,
        "qty": qty,
        "fill_price": paid,
        "price_before_costs": fill_price,
        "cost_bps_per_side": bps,
        "signal_bar": bars[sig_i]["date"],
        "fill_bar": bars[-1]["date"],
        "fill_rule": entry["fill"],
        "stop_price": stop_price,
        "note": "SIMULATED paper fill. No order was sent to any venue.",
    }
    journal(fill_row)
    state["fills"] = state.get("fills", [])[-100:] + [dict(fill_row, at=iso(now_ts()))]
    record_cycle(
        state,
        n,
        "opened",
        "%s %s %s at %s (simulated)" % (side, fmt_qty(qty), symbol, fmt(paid)),
    )
    return "opened"


def cycle_manage_position(state, spec, arm, bars, n, bps) -> str:
    pos = state["position"]
    ex = spec["exit"]
    symbol = pos["symbol"]
    latest = bars[-1]
    long_side = pos["direction"] == "long"

    idx = {b["date"]: i for i, b in enumerate(bars)}
    entry_i = idx.get(pos["entry_bar"])
    bars_held = (
        (len(bars) - 1 - entry_i) if entry_i is not None else pos.get("bars_held", 0)
    )

    reason = None
    raw_exit = None

    # 1. Stop.
    sp = pos.get("stop_price")
    if sp:
        intrabar = bool(spec["stop"].get("intrabar"))
        probe = (
            (latest["low"] if long_side else latest["high"])
            if intrabar
            else latest["close"]
        )
        if (probe <= sp) if long_side else (probe >= sp):
            reason = "stop hit (%s %s against stop %s)" % (
                (
                    ("intrabar low" if long_side else "intrabar high")
                    if intrabar
                    else "close"
                ),
                fmt(probe),
                fmt(sp),
            )
            raw_exit = sp if intrabar else latest["close"]

    # 2. Target.
    if reason is None and ex.get("target"):
        t = ex["target"]
        if t["type"] == "percent":
            tp = pos["entry_price"] * (
                1 + (1 if long_side else -1) * float(t["value"]) / 100.0
            )
        elif t["type"] == "r_multiple":
            sp_ = pos.get("stop_price")
            tp = (
                None
                if not sp_
                else pos["entry_price"]
                + (1 if long_side else -1)
                * float(t["value"])
                * abs(pos["entry_price"] - sp_)
            )
        else:
            a = _atr(bars, int(spec["stop"].get("atr_period") or 14))[len(bars) - 1]
            tp = (
                None
                if not a
                else pos["entry_price"]
                + (1 if long_side else -1) * float(t["value"]) * a
            )
        if tp is not None and (
            (latest["high"] >= tp) if long_side else (latest["low"] <= tp)
        ):
            reason = "target reached (%s)" % fmt(tp)
            raw_exit = tp

    # 3. Time stop.
    if reason is None and ex.get("time_stop_bars"):
        if bars_held >= int(ex["time_stop_bars"]):
            reason = "time stop: %d bars held, limit %d" % (
                bars_held,
                int(ex["time_stop_bars"]),
            )
            raw_exit = latest["close"]

    # 4. Exit conditions.
    if reason is None and (ex.get("conditions") or []):
        if ex["fill"] == "next_bar_open" and len(bars) >= 2:
            sig_i, raw = len(bars) - 2, latest["open"]
        else:
            sig_i, raw = len(bars) - 1, latest["close"]
        fired, lines = eval_block(ex, bars, sig_i)
        for line in lines:
            log("SIGNAL", "exit  " + line, cycle=n)
        if fired is None:
            log(
                "NO-SIGNAL",
                "not enough history to judge the exit rule. Holding, did nothing.",
                cycle=n,
            )
            record_cycle(state, n, "insufficient", "not enough bars for the exit rule")
            return "insufficient"
        if fired:
            reason = "exit rule fired (combine=%s)" % ex["combine"]
            raw_exit = raw

    if reason is None:
        log(
            "HELD-NOOP",
            "holding %s %s %s, %d bar(s) in. Nothing triggered. Nothing to do."
            % (pos["direction"], fmt_qty(pos["qty"]), symbol, bars_held),
            cycle=n,
        )
        pos["bars_held"] = bars_held
        record_cycle(state, n, "held-noop", "nothing triggered")
        return "held-noop"

    age = bar_age_days(latest["date"])
    if age is None or age > MAX_BAR_AGE_DAYS:
        log(
            "REFUSED",
            "will not close on a price that is %s days old. Still holding." % fmt(age),
            cycle=n,
        )
        record_cycle(state, n, "refused", "stale price on the close")
        return "refused"

    # RAIL 5 again — the kill switch immediately before writing the fill.
    if kill_engaged():
        log(
            "ABORTED",
            "kill switch tripped mid-cycle. The close was NOT filled.",
            cycle=n,
        )
        record_cycle(state, n, "aborted", "kill switch tripped mid-cycle")
        return "aborted"

    got = cost_adjust(raw_exit, bps, buying=not long_side)
    pnl = (
        (got - pos["entry_price"]) * pos["qty"]
        if long_side
        else (pos["entry_price"] - got) * pos["qty"]
    )
    log("EXIT", reason, cycle=n)
    log(
        "FILL",
        "CLOSE %s %s at %s, simulated P&L %s after %s bps each side   *** SIMULATED "
        "PAPER FILL. No broker, no money. ***"
        % (fmt_qty(pos["qty"]), symbol, fmt(got), fmt(round(pnl, 2)), fmt(bps)),
        cycle=n,
    )

    fill_row = {
        "cycle": n,
        "event": "close",
        "symbol": symbol,
        "qty": pos["qty"],
        "entry_price": pos["entry_price"],
        "exit_price": got,
        "exit_before_costs": raw_exit,
        "cost_bps_per_side": bps,
        "reason": reason,
        "bars_held": bars_held,
        "simulated_pnl": round(pnl, 4),
        "note": "SIMULATED paper close. No order was sent to any venue.",
    }
    journal(fill_row)
    state["fills"] = state.get("fills", [])[-100:] + [dict(fill_row, at=iso(now_ts()))]
    state["position"] = None
    state["last_exit_bar"] = latest["date"]
    record_cycle(
        state, n, "closed", "%s, simulated P&L %s" % (reason, fmt(round(pnl, 2)))
    )
    return "closed"


# --------------------------------------------------------------------------
# The evidence file day 7 reads
# --------------------------------------------------------------------------
# Day 5's verdict, if it is there. Context, never a gate.
# --------------------------------------------------------------------------


def read_day5():
    for p in VERDICT_SEARCH:
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    v = json.load(f)
                if isinstance(v, dict) and v.get("headline"):
                    return v, p
            except (json.JSONDecodeError, OSError):
                continue
    return None, None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_setup(args) -> None:
    """There is nothing to pip install any more. Prices come from the shared
    data module, which is standard library only. What setup does now is tell
    the member which price source this machine is actually going to use."""
    ensure_dirs()
    d = data_layer()
    if d is None:
        print(
            "I cannot find ptq_data.py, the shared price-data module.\n"
            "It should sit next to this script. Reinstall the day-6 skill folder."
        )
        return
    print(d.render_status())
    print("")
    print("Workspace: %s" % QUANT_HOME)


def cmd_arm(args) -> None:
    spec_path = find_spec(args.spec)
    spec = read_spec(spec_path)
    state = load_state()

    if state["kill_switch"]["engaged"]:
        die(
            "the kill switch is engaged, so I will not arm anything.\n"
            "That is exactly what it is for. Run 'release' first, deliberately."
        )

    # Preflight: never arm a strategy whose prices we cannot actually get.
    bars, note = get_bars(spec)
    if bars is None:
        die(
            "I could not get price data for %s, so I have armed nothing.\n\n%s"
            % (spec["instrument"]["symbol"], note)
        )

    verdict, _vpath = read_day5()
    headline = None
    if (
        verdict
        and str(verdict.get("strategyName", "")).strip() == str(spec["name"]).strip()
    ):
        headline = verdict.get("headline")

    equity = float(spec["sizing"]["starting_equity"])
    cap = float(args.max_notional) if args.max_notional else equity
    expires = now_ts() + args.ttl * 60

    state["arm"] = {
        "name": str(spec["name"]),
        "symbol": str(spec["instrument"]["symbol"]),
        "spec_path": spec_path,
        "spec_fingerprint": sha256_file(spec_path),
        "armed_at": iso(now_ts()),
        "armed_by": whoami(),
        "expires_at": iso(expires),
        "mode": "paper",
        "day5_headline": headline,
        "scope": {
            "symbols": [str(spec["instrument"]["symbol"])],
            "max_notional": cap,
            "max_open_positions": 1,
        },
    }
    state["loop"]["every_seconds"] = max(MIN_INTERVAL_SECONDS, int(args.every))
    state["position"] = None
    state["last_exit_bar"] = None
    save_state(state)

    log(
        "ARMED",
        "%s on %s, %s bars"
        % (spec["name"], spec["instrument"]["symbol"], spec["timeframe"]["bar"]),
    )
    log("ARMED", "mode PAPER. This script has no broker code and reads no keys.")
    log("ARMED", "armed by %s. Consent expires %s." % (whoami(), stamp(expires)))
    log(
        "ARMED",
        "cap %s notional, one position at a time, costs %s bps per side"
        % (fmt(cap), fmt(float(spec["costs"]["per_side_bps"]))),
    )
    log(
        "ARMED",
        "spec fingerprint %s. Change the spec and the loop halts itself."
        % state["arm"]["spec_fingerprint"][:12],
    )
    log("ARMED", "everything it does is written to %s" % LOG_PATH)
    log(
        "PREFLIGHT",
        "%d bars, newest %s close %s  [%s]"
        % (len(bars), bars[-1]["date"], fmt(bars[-1]["close"]), note),
    )
    if headline == "FAIL":
        log(
            "NOTE",
            "day 5 said this rule FAILED its checks. Running it on paper is "
            "still the drill. It is not a green light.",
        )
    journal(
        {
            "event": "armed",
            "strategy": spec["name"],
            "mode": "paper",
            "expires_at": iso(expires),
        }
    )
    print("\nArmed. Now start it:\n  python3 paper_loop.py start\n")


def cmd_disarm(args) -> None:
    state = load_state()
    if not state.get("arm"):
        print("Nothing was armed.")
        return
    name = state["arm"]["name"]
    state["arm"] = None
    save_state(state)
    log("DISARMED", "%s disarmed. Standing consent withdrawn." % name)
    journal({"event": "disarmed", "strategy": name})


def _spawn_daemon(every: int) -> int:
    cmd = [sys.executable, os.path.abspath(__file__), "_daemon", "--every", str(every)]
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen(
            cmd,
            creationflags=flags,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        p = subprocess.Popen(
            cmd,
            start_new_session=True,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return p.pid


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid], capture_output=True, text=True
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_start(args) -> None:
    state = load_state()
    if state["kill_switch"]["engaged"]:
        die("the kill switch is engaged. Nothing starts until you run 'release'.")
    if not state.get("arm"):
        die("nothing is armed. Run 'arm' first.")
    if _pid_alive(state["loop"].get("pid")):
        print("The loop is already running, pid %s." % state["loop"]["pid"])
        return

    every = max(MIN_INTERVAL_SECONDS, int(args.every or state["loop"]["every_seconds"]))
    pid = _spawn_daemon(every)
    state["loop"].update(
        {
            "running": True,
            "pid": pid,
            "kind": "background-process",
            "every_seconds": every,
            "started_at": iso(now_ts()),
            "stopped_at": None,
        }
    )
    save_state(state)

    log(
        "START",
        "background loop started, pid %d, one cycle every %ds. PAPER ONLY."
        % (pid, every),
    )
    journal({"event": "loop-start", "pid": pid, "every_seconds": every})
    print("\nIt is running without you now.")
    print("  watch it:  python3 paper_loop.py log")
    print("  stop it:   python3 paper_loop.py kill\n")


def cmd_daemon(args) -> None:
    every = max(MIN_INTERVAL_SECONDS, int(args.every))
    log("LOOP", "background loop awake, pid %d." % os.getpid(), echo=False)
    while True:
        if kill_engaged():
            log("LOOP", "kill switch engaged. Background loop exiting.", echo=False)
            st = load_state()
            st["loop"]["running"] = False
            st["loop"]["pid"] = None
            st["loop"]["stopped_at"] = iso(now_ts())
            save_state(st)
            return
        try:
            outcome = run_cycle()
            if outcome == "expired":
                st = load_state()
                st["loop"]["running"] = False
                st["loop"]["pid"] = None
                st["loop"]["stopped_at"] = iso(now_ts())
                save_state(st)
                journal({"event": "loop-expired", "reason": "standing consent expired"})
                log("LOOP", "consent expired. Background loop exiting.", echo=False)
                return
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            log("ERROR", "the cycle failed: %r. Nothing was filled." % exc, echo=False)
        waited = 0
        while waited < every:
            slice_s = min(2, every - waited)
            time.sleep(slice_s)
            waited += slice_s
            if kill_engaged():
                break


def cmd_cycle(args) -> None:
    run_cycle(explicit_csv=args.bars)


def cmd_kill(args) -> None:
    """THE KILL SWITCH. Fail-safe: it always succeeds at halting."""
    state = load_state()
    who, reason = whoami(), (args.reason or "stopped by hand")

    state["kill_switch"] = {
        "engaged": True,
        "engaged_at": iso(now_ts()),
        "engaged_by": who,
        "reason": reason,
    }
    state["loop"]["running"] = False
    state["loop"]["stopped_at"] = iso(now_ts())
    save_state(state)

    print("")
    log("KILL", "*** KILL SWITCH ENGAGED by %s. Reason: %s ***" % (who, reason))
    log(
        "KILL",
        "everything is halted. No cycle will evaluate anything and no fill can "
        "be written until you deliberately run 'release'.",
    )

    pid = state["loop"].get("pid")
    if _pid_alive(pid):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                os.kill(pid, signal.SIGTERM)
            log("KILL", "background loop pid %s stopped." % pid)
        except OSError as exc:
            log(
                "KILL",
                "could not signal pid %s (%s). The switch is engaged anyway, so "
                "every cycle it attempts will refuse to act." % (pid, exc),
            )
    else:
        log("KILL", "no background loop was running. The switch is engaged anyway.")

    if state.get("position"):
        p = state["position"]
        log(
            "KILL",
            "one open SIMULATED paper position is left on the books: %s %s %s from "
            "%s. It is not real, nothing is at risk, and no further cycle will "
            "touch it."
            % (p["direction"], fmt_qty(p["qty"]), p["symbol"], fmt(p["entry_price"])),
        )

    state = load_state()
    state["loop"]["pid"] = None
    save_state(state)
    journal({"event": "kill-switch-engaged", "by": who, "reason": reason})
    print("\nStopped. Now run one more cycle and watch it refuse:")
    print("  python3 paper_loop.py cycle\n")


def cmd_release(args) -> None:
    state = load_state()
    if not state["kill_switch"]["engaged"]:
        print("The kill switch is not engaged. Nothing to release.")
        return
    state["kill_switch"] = {
        "engaged": False,
        "engaged_at": None,
        "engaged_by": None,
        "reason": None,
    }
    state["loop"]["cycles_after_kill"] = 0
    save_state(state)
    log(
        "RELEASE",
        "kill switch released by %s. Nothing has restarted on its own. Run "
        "'start' if you want the loop back." % whoami(),
    )
    journal({"event": "kill-switch-released", "by": whoami()})


def cmd_status(args) -> None:
    state = load_state()
    ks, loop = state["kill_switch"], state["loop"]
    arm, pos = state.get("arm"), state.get("position")
    running = _pid_alive(loop.get("pid"))

    print("")
    print("  PART-TIME QUANT ACADEMY  DAY 6  PAPER LOOP")
    print("  " + "-" * 64)
    print("  Mode              PAPER ONLY. No broker code, no keys, no real orders.")
    print(
        "  Kill switch       %s"
        % ("ENGAGED (everything halted)" if ks["engaged"] else "released")
    )
    if ks["engaged"]:
        print(
            "                    by %s at %s"
            % (ks["engaged_by"], human(ks["engaged_at"]))
        )
        print("                    reason: %s" % (ks["reason"] or "-"))
        print(
            "                    cycles refused since: %d"
            % loop.get("cycles_after_kill", 0)
        )
    print(
        "  Background loop   %s"
        % (
            "RUNNING, pid %s, every %ss" % (loop["pid"], loop["every_seconds"])
            if running
            else "not running"
        )
    )
    print("  Cycles run        %d" % loop["cycle_count"])
    if loop.get("last_cycle_at"):
        print("  Last cycle        %s" % human(loop["last_cycle_at"]))
    if arm:
        print("  Armed strategy    %s on %s" % (arm["name"], arm["symbol"]))
        print(
            "                    cap %s notional, one position at a time"
            % fmt(arm["scope"]["max_notional"])
        )
        print(
            "                    armed by %s, expires %s"
            % (arm["armed_by"], human(arm["expires_at"]))
        )
        print("                    spec fingerprint %s" % arm["spec_fingerprint"][:12])
        if arm.get("day5_headline"):
            print("                    day 5 said: %s" % arm["day5_headline"])
    else:
        print("  Armed strategy    nothing armed")
    if pos:
        print(
            "  Open position     SIMULATED %s %s %s from %s (not real, nothing at risk)"
            % (pos["direction"], fmt_qty(pos["qty"]), pos["symbol"], fmt(pos["entry_price"]))
        )
    else:
        print("  Open position     none")
    print("  Run log           %s" % LOG_PATH)
    print("  Event log         %s" % JOURNAL_PATH)
    print("  State             %s" % STATE_PATH)
    print("")


def cmd_log(args) -> None:
    if not os.path.exists(LOG_PATH):
        print("No log yet. Run 'arm', then 'start'.")
        return
    lines = open(LOG_PATH, encoding="utf-8").read().rstrip("\n").split("\n")
    for line in lines[-int(args.n) :]:
        print(line)


def _cron_line(minutes: int) -> str:
    when = "* * * * *" if minutes <= 1 else "*/%d * * * *" % minutes
    return '%s "%s" "%s" cycle >> "%s" 2>&1 %s' % (
        when,
        sys.executable,
        os.path.abspath(__file__),
        os.path.join(QUANT_HOME, "cron.out"),
        SCHEDULE_TAG,
    )


def cmd_schedule_install(args) -> None:
    mins = max(1, int(args.minutes))
    state = load_state()
    if os.name == "nt":
        cmd = [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            WIN_TASK_NAME,
            "/SC",
            "MINUTE",
            "/MO",
            str(mins),
            "/TR",
            '"%s" "%s" cycle' % (sys.executable, os.path.abspath(__file__)),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            die(
                "Windows Task Scheduler refused: %s"
                % (r.stderr.strip() or r.stdout.strip())
            )
        state["loop"]["kind"] = "schtasks"
        log(
            "SCHEDULE",
            "Windows task %s installed, one cycle every %d minute(s)."
            % (WIN_TASK_NAME, mins),
        )
    else:
        if not shutil.which("crontab"):
            die(
                "crontab is not on this machine. Use 'start' instead, it does the same job."
            )
        current = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        ).stdout
        kept = [l for l in current.split("\n") if SCHEDULE_TAG not in l and l.strip()]
        kept.append(_cron_line(mins))
        subprocess.run(
            ["crontab", "-"], input="\n".join(kept) + "\n", text=True, check=True
        )
        state["loop"]["kind"] = "cron"
        log("SCHEDULE", "cron entry installed, one cycle every %d minute(s)." % mins)
    state["loop"]["started_at"] = state["loop"].get("started_at") or iso(now_ts())
    state["loop"]["every_seconds"] = mins * 60
    save_state(state)
    print("\nThe kill switch still wins. Even with the OS timer firing on its own,")
    print(
        "every cycle reads the switch first and refuses to act while it is engaged.\n"
    )


def cmd_schedule_remove(args) -> None:
    if os.name == "nt":
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", WIN_TASK_NAME], capture_output=True
        )
        log("SCHEDULE", "Windows task %s removed." % WIN_TASK_NAME)
    else:
        current = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        ).stdout
        kept = [l for l in current.split("\n") if SCHEDULE_TAG not in l and l.strip()]
        subprocess.run(
            ["crontab", "-"],
            input=("\n".join(kept) + "\n") if kept else "\n",
            text=True,
        )
        log("SCHEDULE", "cron entry removed.")


# --------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        prog="paper_loop.py",
        description="Day 6: run your strategy on a schedule, on paper, and stop it yourself.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="show which price source this machine uses").set_defaults(
        func=cmd_setup
    )

    a = sub.add_parser("arm", help="deliberate, expiring consent for one strategy")
    a.add_argument(
        "--spec",
        default=None,
        help="path to strategy.json (default: ~/quant/strategy.json)",
    )
    a.add_argument(
        "--ttl",
        type=int,
        default=120,
        help="minutes until consent expires (default 120)",
    )
    a.add_argument(
        "--max-notional",
        type=float,
        default=None,
        help="paper cap (default: your starting equity)",
    )
    a.add_argument(
        "--every", type=int, default=60, help="seconds between cycles (default 60)"
    )
    a.set_defaults(func=cmd_arm)

    s = sub.add_parser("start", help="run cycles in the background on a timer")
    s.add_argument("--every", type=int, default=None)
    s.set_defaults(func=cmd_start)

    c = sub.add_parser("cycle", help="run exactly one cycle, in front of you")
    c.add_argument(
        "--bars", default=None, help="optional CSV of bars instead of fetching"
    )
    c.set_defaults(func=cmd_cycle)

    k = sub.add_parser("kill", help="THE KILL SWITCH. Halt everything now.")
    k.add_argument("--reason", default=None)
    k.set_defaults(func=cmd_kill)

    sub.add_parser(
        "release", help="deliberately un-halt (starts nothing)"
    ).set_defaults(func=cmd_release)
    sub.add_parser("status", help="what is armed, running and halted").set_defaults(
        func=cmd_status
    )
    sub.add_parser("disarm", help="withdraw standing consent").set_defaults(
        func=cmd_disarm
    )

    lg = sub.add_parser("log", help="print the run log for screenshotting")
    lg.add_argument("-n", default=40, help="how many lines (default 40)")
    lg.set_defaults(func=cmd_log)

    si = sub.add_parser(
        "schedule-install", help="optional OS-level timer (cron / Task Scheduler)"
    )
    si.add_argument("--minutes", type=int, default=1)
    si.set_defaults(func=cmd_schedule_install)
    sub.add_parser(
        "schedule-remove", help="remove the OS-level timer. Always do this after."
    ).set_defaults(func=cmd_schedule_remove)

    d = sub.add_parser("_daemon", help=argparse.SUPPRESS)
    d.add_argument("--every", type=int, default=60)
    d.set_defaults(func=cmd_daemon)

    args = p.parse_args()
    ensure_dirs()
    args.func(args)


if __name__ == "__main__":
    main()
