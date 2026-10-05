#!/usr/bin/env python3
"""Validate a PTQ Academy strategy spec and print its card.

Standard library only. Runs on Mac and Windows, Python 3.8+, nothing to install.

    python3 validate_spec.py ~/quant/strategy.json
    python3 validate_spec.py ~/quant/strategy.json --card
    python3 validate_spec.py ~/quant/strategy.json --card --write-card ~/quant/STRATEGY.md --data

Exit codes: 0 valid, 1 invalid, 2 could not read the file.
A valid spec can still carry warnings (risk above 2% a trade, a stop far wider than normal moves
or inside noise). They are printed after the card and do not change the exit code.

Errors are written for a member who has never coded. Every message says what is wrong and
what to do about it. The rules here mirror schema.json and SCHEMA.md; if you change one,
change all three.
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys

SPEC_VERSION = 1

ASSET_CLASSES = ["equity", "etf", "fx", "crypto", "futures", "index"]
# "auto" is what day 3 should write: let the data layer pick whichever
# source this machine has a key for. The named providers pin one on
# purpose. "yfinance" and "stooq" are accepted for specs written before
# the shared data layer existed; both now behave as "auto".
DATA_SOURCES = ["auto", "csv", "tiingo", "twelvedata", "alpaca", "polygon",
                "yahoo", "yfinance", "stooq"]
# "1m" only runs from the member's own CSV: no free feed serves 1-minute history.
BARS = ["1d", "1wk", "1h", "30m", "15m", "5m", "1m"]
OPS = [">", ">=", "<", "<=", "==", "crosses_above", "crosses_below"]
CROSS_OPS = ["crosses_above", "crosses_below"]
FIELDS = ["open", "high", "low", "close"]

# series name -> {param name: (type, validator, description)}
INT2 = ("int", lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 2, "a whole number of 2 or more")
INT1 = ("int", lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1, "a whole number of 1 or more")
NUMPOS = ("number", lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0, "a number above 0")
ANYNUM = ("number", lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "a number")
FIELDV = ("str", lambda v: v in FIELDS, "one of open, high, low, close")
STRV = ("str", lambda v: isinstance(v, str) and v.strip() != "", "some text")
TRUEV = ("bool", lambda v: v is True, "true")
INT0 = ("int", lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0, "a whole number of 0 or more")


def _tz_ok(v):
    if not isinstance(v, str) or not v.strip():
        return False
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # Python 3.8: check the shape, day 4 checks the name
        return v == "UTC" or bool(re.match(r"^[A-Za-z]+/[A-Za-z_]+(/[A-Za-z_]+)?$", v))
    try:
        ZoneInfo(v)
        return True
    except Exception:  # noqa: BLE001 - unknown zone
        return False


TZV = ("str", _tz_ok, "a time zone name like America/New_York, Europe/London or UTC")
HHMM = ("str", lambda v: isinstance(v, str) and bool(re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v)),
        "a clock time like 00:00 or 17:00")
SIDEV = ("str", lambda v: v in ("bullish", "bearish"), "bullish (the lows) or bearish (the highs)")
TAKENV = ("str", lambda v: v in ("main", "second", "either"),
          "main (instrument.symbol takes it out), second (instrument.second_symbol does) or either")

SERIES = {
    "open": {}, "high": {}, "low": {}, "close": {}, "volume": {},
    "constant": {"value": ANYNUM},
    "sma": {"period": INT2},
    "ema": {"period": INT2},
    "rsi": {"period": INT2},
    "atr": {"period": INT2},
    "stdev": {"period": INT2},
    "highest": {"period": INT2, "field": FIELDV},
    "lowest": {"period": INT2, "field": FIELDV},
    "pct_change": {"period": INT1},
    "volume_sma": {"period": INT2},
    "bb_upper": {"period": INT2, "mult": NUMPOS},
    "bb_lower": {"period": INT2, "mult": NUMPOS},
    "adx": {"period": INT2},
    "vwap": {"period": INT2},
    "day_of_week": {},
    "pct_below_highest": {"period": INT2, "field": FIELDV},
    # Daily candles built from intraday bars, on the member's clock. days_back 0 is
    # today so far (no peeking at the rest of the day), 1 is yesterday's full candle.
    # day_level: price at `level` percent of that day's range (0 low, 50 the midpoint,
    # 100 high, 61.8 a fib level). day_position: where close sits in that range, 0-100.
    "day_level": {"tz": TZV, "day_start": HHMM, "days_back": INT0, "level": ANYNUM},
    "day_position": {"tz": TZV, "day_start": HHMM, "days_back": INT0},
    # Clock time of the bar's open, in hours: 9.5 is 09:30. For session windows.
    "hour_of_day": {"tz": TZV},
    # SMT divergence against instrument.second_symbol, bar by bar on the same timestamps. 1 on a
    # bar where one market takes out its lowest low (bullish) or highest high (bearish) of the
    # `period` bars before it and the other market does not, else 0. `taken_by` says which one
    # has to take it out. `within` 1 means on this bar; 5 means on this bar or the 4 before it.
    "smt_divergence": {"side": SIDEV, "period": INT2, "within": INT1, "taken_by": TAKENV},
    "custom": {"formula": STRV, "needs_review": TRUEV},
}
SMT = "smt_divergence"

# Cost floor per side, in basis points. 6 covers spread, commission and slippage on
# stocks, ETFs, futures and crypto. A major FX pair costs far less: 1 bp on EURUSD is
# about 1.1 pips a side, 2.2 pips a round trip, still above a typical retail spread.
COST_FLOOR_BPS = {"fx": 1.0}
DEFAULT_COST_FLOOR_BPS = 6.0


def cost_floor(asset_class):
    return COST_FLOOR_BPS.get(asset_class, DEFAULT_COST_FLOOR_BPS)


# ---------------------------------------------------------------- risk and stop checks
# Share of the account a stopped-out trade loses (position size x distance to the stop). Above
# RISK_WARN_PCT the member is warned in plain words; above RISK_CAP_PCT the spec is refused unless
# sizing.confirm_over_cap is true. A position of 100% of the account or more gets a gap warning.
# Day 6's paper loop enforces the same two numbers.
RISK_WARN_PCT = 2.0
RISK_CAP_PCT = 10.0


def _num_ok(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _pct(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


def stop_pct(spec, bars=None):
    """Distance from entry to the stop as % of price, or None when it cannot be known before a trade
    (no stop, or a swing stop). An ATR stop uses the typical bar move."""
    st = spec.get("stop") if isinstance(spec.get("stop"), dict) else {}
    if st.get("type") == "percent" and _num_ok(st.get("value")):
        return float(st["value"])
    if st.get("type") == "atr_multiple" and _num_ok(st.get("value")):
        return float(st["value"]) * typical_bar_move(spec, bars)[0]
    return None


def risk_per_trade(spec, bars=None):
    """(percent of the account a stopped-out trade loses, field) or None.

    Measured by stop distance, so the same trade gives the same answer however it is written:
    risk_percent is that number; fixed_fraction is position size x distance to the stop. With no
    stop the whole fixed-fraction position is at risk. A swing stop's distance is unknown until
    the trade, so it is not measured here."""
    sz = spec.get("sizing") if isinstance(spec, dict) else None
    if not isinstance(sz, dict):
        return None
    if sz.get("method") == "risk_percent" and _num_ok(sz.get("risk_per_trade_pct")):
        return float(sz["risk_per_trade_pct"]), "sizing.risk_per_trade_pct"
    if sz.get("method") == "fixed_fraction" and _num_ok(sz.get("fraction_pct")):
        st = spec.get("stop") if isinstance(spec.get("stop"), dict) else {}
        if st.get("type") in (None, "none"):
            return float(sz["fraction_pct"]), "sizing.fraction_pct"
        d = stop_pct(spec, bars)
        if d is not None:
            return float(sz["fraction_pct"]) * d / 100, "sizing.fraction_pct"
    return None


def position_pct(spec, bars=None):
    """How big one position is, as % of the account, or None if it cannot be known yet."""
    sz = spec.get("sizing") if isinstance(spec, dict) else None
    if not isinstance(sz, dict):
        return None
    if sz.get("method") == "fixed_fraction" and _num_ok(sz.get("fraction_pct")):
        return float(sz["fraction_pct"])
    if sz.get("method") == "risk_percent" and _num_ok(sz.get("risk_per_trade_pct")):
        d = stop_pct(spec, bars)
        if d:
            return float(sz["risk_per_trade_pct"]) / d * 100
    return None


def risk_text(spec, bars=None):
    """Plain words: what this size does to the account after a few losses."""
    pct, _ = risk_per_trade(spec, bars)
    left5, left10 = (1 - min(pct, 100) / 100) ** 5 * 100, (1 - min(pct, 100) / 100) ** 10 * 100
    back = "a {:,.0f}%".format((100 / left10 - 1) * 100) if left10 > 0.01 else "an impossible"
    how = ""
    sz = spec["sizing"]
    if sz["method"] == "fixed_fraction":
        d = stop_pct(spec, bars)
        how = ("(%s%% of the account in the trade x a %s%% stop) " % (_pct(sz["fraction_pct"]), _pct(d))
               if d is not None else "(no stop, so the whole position) ")
    return ("Each losing trade costs about %s%% of the account %s. Losing trades come in runs: 5 in a row "
            "leaves %s%% of the account, 10 in a row leaves %s%%, and getting back from there needs %s "
            "gain. At 1%% a trade, 10 losses in a row cost about 10%%. Most traders keep this at 1 to 2%%."
            % (_pct(pct), how.strip(), _pct(left5), _pct(left10), back)).replace(" .", ".")


# Typical high-to-low range of one DAILY bar, as % of price, used only when no price data was
# checked. Rough, deliberately round figures; the member's own bars always win.
DAILY_RANGE_PCT = {"equity": 2.0, "etf": 1.3, "index": 1.2, "futures": 1.5, "fx": 0.6, "crypto": 3.5}
METAL_DAILY_RANGE_PCT = {"xau": 1.2, "xag": 2.0, "xpt": 1.8, "xpd": 2.5}
SESSION_MINUTES = {"fx": 1440, "crypto": 1440, "futures": 1380}   # everything else: 6.5 hours
BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
STOP_WIDE_X = 10.0     # stop more than 10 typical bars away: it would almost never fire
STOP_TIGHT_X = 0.5     # stop inside half a typical bar: noise stops it out
STOP_GOOD_X = (1.0, 4.0)


def typical_bar_move(spec, bars=None):
    """(typical high-to-low move of one bar as % of price, where the figure came from).

    From the member's bars when at least 30 are given: average true range over the last 500
    bars as a % of the close (ATR %). Otherwise a rough per-market figure scaled to the bar size."""
    inst = spec.get("instrument") or {}
    bar = (spec.get("timeframe") or {}).get("bar", "1d")
    if bars and len(bars) >= 30:
        recent = bars[-501:]
        trs = []
        for prev, b in zip(recent, recent[1:]):
            try:
                h, l, c, pc = float(b["high"]), float(b["low"]), float(b["close"]), float(prev["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if c > 0:
                trs.append(max(h - l, abs(h - pc), abs(l - pc)) / c * 100)
        if len(trs) >= 30:
            return sum(trs) / len(trs), "the last %d bars of your price data" % (len(trs) + 1)
    sym = str(inst.get("symbol", "")).lower().replace("/", "").replace("-", "")
    daily = METAL_DAILY_RANGE_PCT.get(sym[:3]) if len(sym) == 6 else None
    daily = daily or DAILY_RANGE_PCT.get(inst.get("asset_class"), 2.0)
    if bar == "1wk":
        return daily * 5 ** 0.5, "a typical figure for this market, no price data checked"
    if bar in BAR_MINUTES:
        session = SESSION_MINUTES.get(inst.get("asset_class"), 390)
        daily *= (BAR_MINUTES[bar] / float(session)) ** 0.5
    return daily, "a typical figure for this market, no price data checked"


BAR_WORDS = {"1d": "daily", "1wk": "weekly", "1h": "hourly", "30m": "30-minute",
             "15m": "15-minute", "5m": "5-minute", "1m": "1-minute"}


def stop_check(spec, bars=None):
    """(field, short, long) when the stop is far wider than normal moves or tighter than noise."""
    st = spec.get("stop") or {}
    stype, v = st.get("type"), st.get("value")
    if stype not in ("percent", "atr_multiple") or not _num_ok(v):
        return None
    move, source = typical_bar_move(spec, bars)
    sym = (spec.get("instrument") or {}).get("symbol", "this market")
    bw = BAR_WORDS.get((spec.get("timeframe") or {}).get("bar"), "")
    lo, hi = STOP_GOOD_X
    if stype == "percent":
        x = v / move
        dist = "%s%%" % _pct(v)
        better = ("A stop between %s%% and %s%% (1 to 4 typical bars) sits outside normal noise and "
                  "still gets you out when you're wrong. Or use an ATR stop, which follows the market's "
                  "own movement: stop.type atr_multiple, value 2, atr_period 14."
                  % ("%.2f" % (move * lo), "%.2f" % (move * hi)))
    else:
        x = float(v)
        dist = "%s x ATR" % _pct(v)
        better = "Something between %g and %g x ATR sits outside normal noise and still gets you out " \
                 "when you're wrong." % (lo, hi)
    typical = "On %s %s bars a typical bar moves about %.2f%% from high to low (ATR; source: %s)" % (
        sym, bw, move, source)
    if x > STOP_WIDE_X:
        return ("stop.value",
                "Stop %s is about %s typical %s bars away: it would almost never fire" % (dist, "{:,.0f}".format(x), bw),
                "is %s. %s, so this stop is about %s times a normal bar's move. Price almost never "
                "travels that far within a trade, so the stop would almost never fire and the trade "
                "has no real protection. %s" % (dist, typical, "{:,.0f}".format(x), better))
    if x < STOP_TIGHT_X:
        return ("stop.value",
                "Stop %s is inside normal %s noise: most trades would be stopped out by noise" % (dist, bw),
                "is %s. %s, so this stop is %.2f of a normal bar's move. Ordinary noise would stop out "
                "most trades before the idea has a chance to work, and every stop-out pays costs. %s"
                % (dist, typical, x, better))
    return None


def warnings(spec, bars=None):
    """Plain-word warnings on a valid spec: [(field, short line for the card, full message)].
    They do not block day 4; the member reads them and decides."""
    out = []
    stake = risk_per_trade(spec, bars)
    if stake and stake[0] > RISK_WARN_PCT:
        over = stake[0] > RISK_CAP_PCT
        short = ("Risk: about %s%% of the account lost per stopped-out trade%s" % (
            _pct(stake[0]), ", above the 10% cap (confirmed by the member)" if over else ", above 2%"))
        out.append((stake[1], short, "%s%s" % (
            risk_text(spec, bars),
            " It is above the academy cap of %g%%, and the member has confirmed it." % RISK_CAP_PCT if over else "")))
    pos = position_pct(spec, bars)
    if pos is not None and pos >= 100:
        out.append(("sizing", "Position: %s%% of the account in one trade; a gap can jump the stop" % _pct(pos),
                    "One trade holds %s%% of the account. The stop sets the planned loss, but it is not a "
                    "guarantee: a price gap, or a fast candle when the stop is only checked on the close, can "
                    "jump straight past it and fill much further away. With that much in one trade, "
                    "a 10%% gap through the stop costs %s%% of the account, far more than the stop suggests."
                    % (_pct(pos), _pct(pos / 10))))
    s = stop_check(spec, bars)
    if s:
        out.append(s)
    return out


def bars_for(spec):
    """The member's price bars for the typical-move figure, or None. Never raises."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import ptq_data  # noqa: E402 - ships beside this file
        bars, _ = ptq_data.bars_from_spec(spec, allow_stale=True)
        return bars
    except Exception:  # noqa: BLE001 - no key, no network, no file: fall back to the typical figure
        return None


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^[ex][0-9]+$")


def _at(where, key):
    """Join a block path and a field name. Top level has no prefix."""
    return (where + "." + key) if where else key


class Errors(object):
    def __init__(self):
        self.items = []

    def add(self, where, message):
        self.items.append((where, message))

    def __bool__(self):
        return bool(self.items)

    __nonzero__ = __bool__


# ---------------------------------------------------------------- helpers

def _obj(errs, parent, where, key, required_keys):
    """Fetch parent[key], check it is an object with exactly required_keys."""
    val = parent.get(key)
    if not isinstance(val, dict):
        errs.add(where, "missing, or not a block of settings")
        return None
    missing = [k for k in required_keys if k not in val]
    extra = [k for k in val.keys() if k not in required_keys]
    for m in missing:
        errs.add(where + "." + m, "missing. SCHEMA.md says what goes here")
    for e in extra:
        errs.add(where + "." + e, "not a field this format knows about. Remove it or fix the spelling")
    return val


def _enum(errs, block, where, key, allowed):
    if key not in block:
        return None          # the missing-key check already reported it
    v = block.get(key)
    if v not in allowed:
        errs.add(_at(where, key), "is %r. It has to be one of: %s" % (v, ", ".join(map(str, allowed))))
        return None
    return v


def _num(errs, block, where, key, lo=None, hi=None, allow_null=False, integer=False):
    if key not in block:
        return None
    v = block.get(key)
    if v is None:
        if not allow_null:
            errs.add(_at(where, key), "is empty. It needs a number")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        errs.add(_at(where, key), "is %r. It needs a number" % (v,))
        return None
    if integer and not isinstance(v, int):
        errs.add(_at(where, key), "is %r. It needs a whole number" % (v,))
        return None
    if lo is not None and v <= lo:
        errs.add(_at(where, key), "is %s. It has to be above %s" % (v, lo))
        return None
    if hi is not None and v > hi:
        errs.add(_at(where, key), "is %s. It cannot be above %s" % (v, hi))
        return None
    return v


def _text(errs, block, where, key):
    if key not in block:
        return None
    v = block.get(key)
    if not isinstance(v, str) or v.strip() == "":
        errs.add(_at(where, key), "is empty. Write it in plain English, one line")
        return None
    return v


def _date(errs, block, where, key):
    if key not in block:
        return None
    v = block.get(key)
    if not isinstance(v, str) or not DATE_RE.match(v):
        errs.add(_at(where, key), "is %r. It has to look like 2010-01-01" % (v,))
        return None
    try:
        _dt.date(int(v[0:4]), int(v[5:7]), int(v[8:10]))
    except ValueError:
        errs.add(_at(where, key), "is %r, which is not a real date" % (v,))
        return None
    return v


def _bool(errs, block, where, key):
    if key not in block:
        return None
    v = block.get(key)
    if not isinstance(v, bool):
        errs.add(_at(where, key), "is %r. It has to be true or false" % (v,))
        return None
    return v


# ---------------------------------------------------------------- operands

def check_operand(errs, op, where):
    if not isinstance(op, dict):
        errs.add(where, "is not a block of settings")
        return None
    for k in ("series", "params", "offset"):
        if k not in op:
            errs.add(where + "." + k, "missing")
    extra = [k for k in op if k not in ("series", "params", "offset")]
    for e in extra:
        errs.add(where + "." + e, "not a field this format knows about")
    name = op.get("series")
    if name not in SERIES:
        errs.add(where + ".series", "is %r, which this format does not know. Allowed: %s"
                 % (name, ", ".join(sorted(SERIES))))
        return None
    params = op.get("params")
    if not isinstance(params, dict):
        errs.add(where + ".params", "missing. Use {} when there are no settings")
        return name
    spec = SERIES[name]
    for pname, (_t, ok, desc) in spec.items():
        if pname not in params:
            errs.add(where + ".params." + pname, "missing. %s needs %s, %s" % (name, pname, desc))
        elif not ok(params[pname]):
            errs.add(where + ".params." + pname, "is %r. It has to be %s" % (params[pname], desc))
    for pname in params:
        if pname not in spec:
            errs.add(where + ".params." + pname, "is not a setting %s takes" % name)
    _num(errs, op, where, "offset", lo=-1, integer=True)
    return name


def check_condition(errs, cond, where, prefix):
    required = ["id", "plain_english", "left", "op", "right", "persist_bars"]
    if not isinstance(cond, dict):
        errs.add(where, "is not a condition block")
        return
    for k in required:
        if k not in cond:
            errs.add(where + "." + k, "missing")
    for k in cond:
        if k not in required:
            errs.add(where + "." + k, "not a field this format knows about")
    cid = cond.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        errs.add(where + ".id", "is %r. Entry conditions are e1, e2... exit conditions are x1, x2..." % (cid,))
    elif not cid.startswith(prefix):
        errs.add(where + ".id", "is %r but it lives in the %s block, so it has to start with %r"
                 % (cid, "entry" if prefix == "e" else "exit", prefix))
    _text(errs, cond, where, "plain_english")
    left = check_operand(errs, cond.get("left"), where + ".left")
    right = check_operand(errs, cond.get("right"), where + ".right")
    op = _enum(errs, cond, where, "op", OPS)
    persist = _num(errs, cond, where, "persist_bars", lo=0, integer=True)
    if op == "==" and not ("day_of_week" in (left, right) or "constant" in (left, right)):
        errs.add(where + ".op", "is '==' but neither side is day_of_week or a plain number. "
                                "Exact equality on prices never fires. Use > or <")
    if op in CROSS_OPS and persist not in (None, 1):
        errs.add(where + ".persist_bars", "is %s, but a cross happens on one bar only. Set it to 1" % persist)


def check_second_market(errs, inst):
    """instrument.second_symbol, plus second_csv_path on a CSV spec: the market smt_divergence
    compares with. It comes from the same price source and asset class as instrument.symbol."""
    if "second_symbol" not in inst:
        if "second_csv_path" in inst:
            errs.add("instrument.second_csv_path", "is set but there is no second_symbol. Name the "
                                                   "second market, or remove this")
        return
    sym, second = inst.get("symbol"), inst.get("second_symbol")
    if not isinstance(second, str) or not second.strip():
        errs.add("instrument.second_symbol", "is empty. It is the second market an smt_divergence "
                                             "condition compares with, spelled the way your data "
                                             "source spells it, like GBPUSD")
    elif isinstance(sym, str) and second.strip().upper() == sym.strip().upper():
        errs.add("instrument.second_symbol", "is %s, the same market as instrument.symbol. SMT "
                                             "divergence compares two different markets that usually "
                                             "move together, like EURUSD and GBPUSD" % second)
    p2 = inst.get("second_csv_path")
    if inst.get("data_source") != "csv":
        if "second_csv_path" in inst:
            errs.add("instrument.second_csv_path", "is only read when data_source is csv. The second "
                                                   "market comes from the same price source as the "
                                                   "first. Remove it, or set data_source to csv")
        return
    if not isinstance(p2, str) or not p2:
        errs.add("instrument.second_csv_path", "missing. Your prices come from a CSV, so the second "
                                               "market needs its own file too: same bar size, same "
                                               "dates and same clock as csv_path")
    elif not os.path.exists(os.path.expanduser(p2)):
        errs.add("instrument.second_csv_path", "points at %s, which is not there" % p2)
    elif isinstance(inst.get("csv_path"), str) and os.path.abspath(os.path.expanduser(p2)) == \
            os.path.abspath(os.path.expanduser(inst["csv_path"])):
        errs.add("instrument.second_csv_path", "is the same file as csv_path. The second market "
                                               "needs its own file")


def uses_smt(spec):
    return any(isinstance(c, dict) and isinstance(c.get(side), dict) and c[side].get("series") == SMT
               for block in ("entry", "exit") if isinstance(spec.get(block), dict)
               for c in (spec[block].get("conditions") if isinstance(spec[block].get("conditions"), list) else [])
               for side in ("left", "right"))


# ---------------------------------------------------------------- top level

def validate(spec):
    errs = Errors()
    if not isinstance(spec, dict):
        errs.add("file", "is not a strategy spec. Re-run /strategy-spec")
        return errs

    top = ["spec_version", "name", "created", "source_rule", "instrument", "timeframe",
           "direction", "entry", "exit", "stop", "sizing", "costs",
           "vague_terms_resolved", "unresolved"]
    for k in top:
        if k not in spec:
            errs.add(k, "missing. Re-run /strategy-spec to rebuild the file")
    for k in spec:
        if k not in top:
            errs.add(k, "not a field this format knows about")

    if spec.get("spec_version") != SPEC_VERSION:
        errs.add("spec_version", "is %r. This tool only reads version %d"
                 % (spec.get("spec_version"), SPEC_VERSION))

    name = spec.get("name")
    if not isinstance(name, str) or len(name.strip()) < 2:
        errs.add("name", "is empty. Your strategy needs a name")
    elif re.match(r"^(strategy|test|untitled|my strategy)\s*\d*$", name.strip(), re.I):
        errs.add("name", "is %r. That is a placeholder, not a name. Pick one you would defend" % name)
    _date(errs, spec, "", "created")
    _text(errs, spec, "", "source_rule")
    _enum(errs, spec, "", "direction", ["long", "short"])

    # instrument
    inst = spec.get("instrument")
    if isinstance(inst, dict):
        keys = ["symbol", "asset_class", "data_source", "currency"]
        if inst.get("data_source") == "csv":
            keys.append("csv_path")
        # Optional: the second market for smt_divergence. check_second_market says what is wrong.
        keys += [k for k in ("second_symbol", "second_csv_path") if k in inst]
        _obj(errs, spec, "instrument", "instrument", keys)
        sym = inst.get("symbol")
        if not isinstance(sym, str) or sym.strip() == "":
            errs.add("instrument.symbol", "is empty. It has to be the exact ticker your data source uses")
        _enum(errs, inst, "instrument", "asset_class", ASSET_CLASSES)
        _enum(errs, inst, "instrument", "data_source", DATA_SOURCES)
        cur = inst.get("currency")
        if not isinstance(cur, str) or not re.match(r"^[A-Z]{3}$", cur or ""):
            errs.add("instrument.currency", "is %r. Use a 3-letter code like USD or GBP" % (cur,))
        if inst.get("data_source") == "csv":
            p = inst.get("csv_path")
            if not isinstance(p, str) or not p:
                errs.add("instrument.csv_path", "missing. A csv source needs the path to the file")
            elif not os.path.exists(os.path.expanduser(p)):
                errs.add("instrument.csv_path", "points at %s, which is not there" % p)
        check_second_market(errs, inst)
    else:
        errs.add("instrument", "missing")

    # timeframe
    tf = _obj(errs, spec, "timeframe", "timeframe", ["bar", "history_start", "session"])
    if tf:
        _enum(errs, tf, "timeframe", "bar", BARS)
        _enum(errs, tf, "timeframe", "session", ["regular", "24h"])
        start = _date(errs, tf, "timeframe", "history_start")
        if start:
            try:
                d = _dt.date(int(start[0:4]), int(start[5:7]), int(start[8:10]))
                if (_dt.date.today() - d).days < 730:
                    errs.add("timeframe.history_start",
                             "is %s. Day 5 needs at least 12 months held back out of sample, "
                             "so start at least 2 years ago" % start)
            except ValueError:
                pass

    # entry
    en = _obj(errs, spec, "entry", "entry",
              ["plain_english", "combine", "conditions", "fill", "max_open_positions", "cooldown_bars"])
    if en:
        _text(errs, en, "entry", "plain_english")
        _enum(errs, en, "entry", "combine", ["all", "any"])
        _enum(errs, en, "entry", "fill", ["next_bar_open", "same_bar_close"])
        _num(errs, en, "entry", "max_open_positions", lo=0, integer=True)
        _num(errs, en, "entry", "cooldown_bars", lo=-1, integer=True)
        conds = en.get("conditions")
        if not isinstance(conds, list) or not conds:
            errs.add("entry.conditions", "is empty. An entry needs at least one condition")
        else:
            seen = set()
            for i, c in enumerate(conds):
                check_condition(errs, c, "entry.conditions[%d]" % i, "e")
                cid = c.get("id") if isinstance(c, dict) else None
                if cid in seen:
                    errs.add("entry.conditions[%d].id" % i, "is %r, already used. Every id has to be unique" % cid)
                seen.add(cid)

    # exit
    ex = _obj(errs, spec, "exit", "exit",
              ["plain_english", "combine", "conditions", "time_stop_bars", "target", "fill"])
    if ex:
        _text(errs, ex, "exit", "plain_english")
        _enum(errs, ex, "exit", "combine", ["all", "any"])
        _enum(errs, ex, "exit", "fill", ["next_bar_open", "same_bar_close"])
        _num(errs, ex, "exit", "time_stop_bars", lo=0, allow_null=True, integer=True)
        conds = ex.get("conditions")
        if not isinstance(conds, list):
            errs.add("exit.conditions", "missing. Use [] if the trade only ends on a stop, target or time")
            conds = []
        else:
            seen = set()
            for i, c in enumerate(conds):
                check_condition(errs, c, "exit.conditions[%d]" % i, "x")
                cid = c.get("id") if isinstance(c, dict) else None
                if cid in seen:
                    errs.add("exit.conditions[%d].id" % i, "is %r, already used" % cid)
                seen.add(cid)
        tgt = ex.get("target")
        if tgt is not None:
            if not isinstance(tgt, dict):
                errs.add("exit.target", "is not a target block. Use null if you have no profit target")
            else:
                ttype = _enum(errs, tgt, "exit.target", "type", ["atr_multiple", "percent", "r_multiple"])
                _num(errs, tgt, "exit.target", "value", lo=0)
                if ttype == "r_multiple" and isinstance(spec.get("stop"), dict) \
                        and spec["stop"].get("type") == "none":
                    errs.add("exit.target.type", "is r_multiple, which is measured in stop distances, "
                                                 "but there is no stop. Add a stop or use a percent target")
        stop_is_none = isinstance(spec.get("stop"), dict) and spec["stop"].get("type") == "none"
        if not conds and ex.get("time_stop_bars") is None and tgt is None and stop_is_none:
            errs.add("exit", "has no exit condition, no time stop, no target and no stop loss. "
                             "That trade never ends. Give it at least one way out")

    # smt_divergence and instrument.second_symbol only make sense together
    has_second = isinstance(inst, dict) and "second_symbol" in inst
    if uses_smt(spec) and not has_second:
        errs.add("instrument.second_symbol", "missing. smt_divergence compares this market with a "
                                             "second one. Add the second market here, like GBPUSD for "
                                             "EURUSD, plus second_csv_path when your prices come from a CSV")
    elif has_second and not uses_smt(spec):
        errs.add("instrument.second_symbol", "is set but no condition uses smt_divergence, the only "
                                             "thing that reads it. Remove it, or add the SMT condition")

    # stop
    st = _obj(errs, spec, "stop", "stop", ["type", "value", "atr_period", "trailing", "intrabar"])
    if st:
        stype = _enum(errs, st, "stop", "type", ["atr_multiple", "percent", "swing", "none"])
        _bool(errs, st, "stop", "trailing")
        _bool(errs, st, "stop", "intrabar")
        if stype == "none":
            if st.get("value") is not None:
                errs.add("stop.value", "is set but stop.type is 'none'. Use null")
            if st.get("atr_period") is not None:
                errs.add("stop.atr_period", "is set but stop.type is 'none'. Use null")
        else:
            _num(errs, st, "stop", "value", lo=0)
            if stype == "swing":
                v = st.get("value")
                if not isinstance(v, int) or isinstance(v, bool) or v < 2:
                    errs.add("stop.value", "is %r. For a swing stop it is how many bars back to look "
                                           "for the swing low (long) or high (short), a whole number of 2 or more" % (v,))
            if stype == "atr_multiple":
                _num(errs, st, "stop", "atr_period", lo=1, integer=True)
            elif st.get("atr_period") is not None:
                errs.add("stop.atr_period", "is set but the stop is not an ATR stop. Use null")

    # sizing. confirm_over_cap is the one optional field: the member's own yes to a size above the cap.
    raw_sz = spec.get("sizing")
    view = spec
    if isinstance(raw_sz, dict) and "confirm_over_cap" in raw_sz:
        view = dict(spec, sizing={k: v for k, v in raw_sz.items() if k != "confirm_over_cap"})
        if not isinstance(raw_sz["confirm_over_cap"], bool):
            errs.add("sizing.confirm_over_cap", "is %r. It is true or false, and only the member "
                                                "can set it to true" % (raw_sz["confirm_over_cap"],))
    sz = _obj(errs, view, "sizing", "sizing",
              ["method", "risk_per_trade_pct", "fraction_pct", "starting_equity", "max_risk_open_pct"])
    if sz:
        method = _enum(errs, sz, "sizing", "method", ["risk_percent", "fixed_fraction"])
        _num(errs, sz, "sizing", "starting_equity", lo=0)
        _num(errs, sz, "sizing", "max_risk_open_pct", lo=0)
        if method == "risk_percent":
            r = _num(errs, sz, "sizing", "risk_per_trade_pct", lo=0, hi=100)
            if sz.get("fraction_pct") is not None:
                errs.add("sizing.fraction_pct", "is set but the method is risk_percent. Use null")
            if isinstance(spec.get("stop"), dict) and spec["stop"].get("type") == "none":
                errs.add("sizing.method", "is risk_percent but there is no stop. Risk sizing measures "
                                          "the distance to the stop, so there is nothing to size against. "
                                          "Add a stop, or use fixed_fraction")
        elif method == "fixed_fraction":
            _num(errs, sz, "sizing", "fraction_pct", lo=0, hi=100)
            if sz.get("risk_per_trade_pct") is not None:
                errs.add("sizing.risk_per_trade_pct", "is set but the method is fixed_fraction. Use null")
        stake = risk_per_trade(spec)
        if stake and stake[0] > RISK_CAP_PCT and raw_sz.get("confirm_over_cap") is not True:
            errs.add(stake[1], risk_text(spec) + " That is above the academy cap of %g%% a trade, so "
                     "this spec is refused. Lower it to %g%% or less. Only if the member, in their own "
                     "words, says they understand and still want it, set sizing.confirm_over_cap to "
                     "true. Never set it for them." % (RISK_CAP_PCT, RISK_CAP_PCT))

    # costs
    co = _obj(errs, spec, "costs", "costs", ["per_side_bps"])
    if co:
        c = _num(errs, co, "costs", "per_side_bps")
        floor = cost_floor(inst.get("asset_class") if isinstance(inst, dict) else None)
        if c is not None and c < floor:
            errs.add("costs.per_side_bps",
                     "is %s. The academy floor for this market is %g bps per side. A backtest that "
                     "pays less than real trading pays is a backtest that lies to you" % (c, floor))

    # audit lists
    vt = spec.get("vague_terms_resolved")
    if not isinstance(vt, list):
        errs.add("vague_terms_resolved", "missing. Use [] if there was genuinely nothing vague")
    else:
        for i, item in enumerate(vt):
            if not isinstance(item, dict) or "term" not in item or "became" not in item:
                errs.add("vague_terms_resolved[%d]" % i, "needs a 'term' and a 'became'")
    un = spec.get("unresolved")
    if not isinstance(un, list):
        errs.add("unresolved", "missing. Use [] if nothing is unresolved")
    elif any(not isinstance(u, str) or not u.strip() for u in un):
        errs.add("unresolved", "has an empty entry. Each one is a question, written out")

    return errs


# ---------------------------------------------------------------- card

def _series_text(op, inst=None):
    name = op.get("series")
    p = op.get("params") or {}
    off = op.get("offset") or 0
    if name == "constant":
        base = str(p.get("value"))
    elif name in ("open", "high", "low", "close", "volume", "day_of_week"):
        base = name
    elif name in ("highest", "lowest"):
        base = "%s %s of last %s bars" % (name, p.get("field"), p.get("period"))
    elif name == "pct_below_highest":
        base = "%% below the %s-bar high %s" % (p.get("period"), p.get("field"))
    elif name in ("bb_upper", "bb_lower"):
        base = "%s(%s, %sx)" % (name.replace("bb_", "Bollinger "), p.get("period"), p.get("mult"))
    elif name in ("day_level", "day_position"):
        back = p.get("days_back")
        day = "today's range so far" if back == 0 else (
            "yesterday's range" if back == 1 else "the range %s days ago" % back)
        when = "(day starts %s %s)" % (p.get("day_start"), p.get("tz"))
        if name == "day_level":
            base = "%s%% of %s %s" % (p.get("level"), day, when)
        else:
            base = "where close sits in %s, 0-100 %s" % (day, when)
    elif name == "hour_of_day":
        base = "hour of day (%s)" % p.get("tz")
    elif name == SMT:
        a = (inst or {}).get("symbol") or "this market"
        b = (inst or {}).get("second_symbol") or "the second market"
        ext = "lowest low" if p.get("side") == "bullish" else "highest high"
        who = {"main": (a, b), "second": (b, a)}.get(p.get("taken_by"))
        took = ("%s takes out its %s of the %s bars before and %s does not" % (who[0], ext, p.get("period"), who[1])
                if who else "%s or %s takes out its %s of the %s bars before and the other does not"
                % (a, b, ext, p.get("period")))
        within = p.get("within")
        base = "SMT divergence, %s: %s, %s" % (p.get("side"), took, "on this bar" if within == 1 else
                                               "on this bar or the %s before it" % (within - 1 if isinstance(within, int) else within))
    elif name == "custom":
        base = "CUSTOM: %s" % p.get("formula")
    elif "period" in p:
        base = "%s(%s)" % (name.upper(), p.get("period"))
    else:
        base = name
    if off:
        base += " [%s bars ago]" % off
    return base


def _cond_text(c, inst=None):
    ops = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "=",
           "crosses_above": "crosses above", "crosses_below": "crosses below"}
    right = (c["right"].get("params") or {}).get("value") if c["right"].get("series") == "constant" else None
    if c["left"].get("series") == SMT and (c["op"], right) in (("==", 1), (">=", 1), (">", 0)):
        s = _series_text(c["left"], inst)        # "smt ... = 1" reads as just the divergence
    else:
        s = "%s %s %s" % (_series_text(c["left"], inst), ops.get(c["op"], c["op"]),
                          _series_text(c["right"], inst))
    if c.get("persist_bars", 1) > 1:
        s += ", %s bars in a row" % c["persist_bars"]
    return s


def _wrap(text, width):
    out, line = [], ""
    for word in str(text).split():
        if len(line) + len(word) + (1 if line else 0) > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return out or [""]


def card(spec, warns=None):
    if warns is None:
        warns = warnings(spec)
    W, K = 68, 13
    L, R = "│  ", "│"
    VW = W - 4 - K          # value column width
    lines = []

    def row(text=""):
        for piece in _wrap(text, W - 4):
            lines.append(L + piece.ljust(W - 4) + " " + R)

    def kv(key, val):
        key = str(key)[:K]
        val = str(val)
        hang = "   " if re.match(r"^(\d+\.|-) ", val) else ""
        pieces = _wrap(val, VW)
        if hang and len(pieces) > 1:
            pieces = [pieces[0]] + _wrap(" ".join(pieces[1:]), VW - len(hang))
        for i, piece in enumerate(pieces):
            body = (hang if i else "") + piece
            lines.append(L + (key if i == 0 else "").ljust(K) + body.ljust(VW) + " " + R)

    def rule(ch="─", left="├", right="┤"):
        lines.append(left + ch * (W - 1) + right)

    lines.append("┌" + "─" * (W - 1) + "┐")
    row(spec["name"].upper())
    row("Part-Time Quant Academy · Day 3 spec · v%d" % spec["spec_version"])
    rule()

    inst, tf = spec["instrument"], spec["timeframe"]
    bar_word = {"1d": "daily", "1wk": "weekly", "1h": "hourly",
                "30m": "30-minute", "15m": "15-minute", "5m": "5-minute", "1m": "1-minute"}[tf["bar"]]
    kv("INSTRUMENT", "%s  (%s, %s, %s bars)" % (
        inst["symbol"] + (" vs %s" % inst["second_symbol"] if inst.get("second_symbol") else ""),
        inst["asset_class"], inst["currency"], bar_word))
    kv("HISTORY", "%s to today, via %s" % (tf["history_start"], inst["data_source"]))
    kv("DIRECTION", "%s only" % spec["direction"])
    rule()

    en = spec["entry"]
    kv("ENTRY", "%s of these true, fill %s" % (en["combine"], en["fill"].replace("_", " ")))
    for i, c in enumerate(en["conditions"], 1):
        kv("", "%d. %s" % (i, _cond_text(c, inst)))
    if en["cooldown_bars"]:
        kv("", "wait %s bars after a flat before re-entering" % en["cooldown_bars"])
    rule()

    ex = spec["exit"]
    label = "first one to fire" if ex["combine"] == "any" else "all of these true"
    kv("EXIT", "%s, fill %s" % (label, ex["fill"].replace("_", " ")))
    n = 0
    for c in ex["conditions"]:
        n += 1
        kv("", "%d. %s" % (n, _cond_text(c, inst)))
    if ex["time_stop_bars"]:
        n += 1
        kv("", "%d. %s bars in the trade" % (n, ex["time_stop_bars"]))
    if ex["target"]:
        n += 1
        unit = {"atr_multiple": "x ATR", "percent": "%", "r_multiple": "x the stop distance (R)"}[ex["target"]["type"]]
        kv("", "%d. target %s%s in profit" % (n, ex["target"]["value"], unit))
    if n == 0:
        kv("", "stop loss only")
    rule()

    st = spec["stop"]
    side = "below" if spec["direction"] == "long" else "above"
    if st["type"] == "none":
        kv("STOP", "none. Nothing closes a losing trade except the exit rules")
    elif st["type"] == "atr_multiple":
        kv("STOP", "%s x ATR(%s) %s entry, %s, checked %s"
           % (st["value"], st["atr_period"], side,
              "trailing" if st["trailing"] else "fixed",
              "intrabar" if st["intrabar"] else "on the close"))
    elif st["type"] == "swing":
        kv("STOP", "at the swing %s of the last %s bars, %s, checked %s"
           % ("low" if spec["direction"] == "long" else "high", st["value"],
              "trailing" if st["trailing"] else "fixed",
              "intrabar" if st["intrabar"] else "on the close"))
    else:
        kv("STOP", "%s%% %s entry, %s, checked %s"
           % (st["value"], side, "trailing" if st["trailing"] else "fixed",
              "intrabar" if st["intrabar"] else "on the close"))

    sz = spec["sizing"]
    if sz["method"] == "risk_percent":
        kv("SIZING", "risk %.2f%% of account per trade" % sz["risk_per_trade_pct"])
    else:
        kv("SIZING", "%.2f%% of account into each position" % sz["fraction_pct"])
    kv("", "max %s position(s) open, max %.2f%% risk on at once"
       % (spec["entry"]["max_open_positions"], sz["max_risk_open_pct"]))
    kv("COSTS", "%.1f bps per side" % spec["costs"]["per_side_bps"])
    rule()

    unresolved = spec["unresolved"]
    kv("TERMS PINNED", str(len(spec["vague_terms_resolved"])))
    kv("UNRESOLVED", str(len(unresolved)))
    kv("STATUS", "CHECKED, not yet backtested" if not unresolved else "BLOCKED, see unresolved below")
    if warns:
        rule()
        row("WARNINGS. Read these before day 4:")
        for _, short, _ in warns:
            kv("", "- %s" % short)
    if unresolved:
        rule()
        row("STILL UNRESOLVED. Day 4 will not run until these are answered:")
        for u in unresolved:
            kv("", "- %s" % u)
    lines.append("└" + "─" * (W - 1) + "┘")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Validate a PTQ Academy strategy spec.")
    ap.add_argument("path", help="path to strategy.json")
    ap.add_argument("--card", action="store_true", help="print the strategy card when the spec is valid")
    ap.add_argument("--write-card", metavar="PATH", help="also save the card to a markdown file")
    ap.add_argument("--quiet", action="store_true", help="print nothing, use the exit code")
    ap.add_argument("--data", action="store_true",
                    help="measure the typical bar move from the member's price data (cache, key or CSV)")
    args = ap.parse_args()

    path = os.path.expanduser(args.path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            spec = json.load(fh)
    except FileNotFoundError:
        if not args.quiet:
            print("No spec at %s.\nRun /strategy-spec to build one." % path)
        return 2
    except json.JSONDecodeError as e:
        if not args.quiet:
            print("%s is not readable as JSON: %s (line %d)\nRe-run /strategy-spec to rewrite it."
                  % (path, e.msg, e.lineno))
        return 2

    errs = validate(spec)
    if errs:
        if not args.quiet:
            print("Spec is not valid yet. %d thing(s) to fix:\n" % len(errs.items))
            for where, msg in errs.items:
                print("  %s %s" % ((where + ":") if where else "", msg))
            print("\nFix these and run this check again. Do not hand a broken spec to day 4.")
        return 1

    warns = warnings(spec, bars_for(spec) if args.data else None)
    if not args.quiet:
        if args.card:
            print(card(spec, warns))
        else:
            print("Spec is valid. %s, %s, %s bars."
                  % (spec["name"], spec["instrument"]["symbol"], spec["timeframe"]["bar"]))
        if spec["unresolved"]:
            print("\n%d unresolved question(s). Day 4 will refuse to run until they are answered."
                  % len(spec["unresolved"]))
        if warns:
            print("\n%d warning(s). The spec is valid, but tell the member each one in plain words "
                  "and let them decide:\n" % len(warns))
            for where, _, msg in warns:
                print("  %s: %s\n" % (where, msg))

    if args.write_card:
        out = os.path.expanduser(args.write_card)
        body = "# %s\n\n```\n%s\n```\n\nSource rule, day 2, unedited:\n\n> %s\n" % (
            spec["name"], card(spec, warns), spec["source_rule"].replace("\n", "\n> "))
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(body)
        if not args.quiet:
            print("\nCard saved to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
