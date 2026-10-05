"""Day 4 — first real backtest.

Reads the day-3 strategy spec (schema v1, see
~/ptq-academy/day-3/skill/SCHEMA.md), pulls real historical bars, runs the
rule, and prints one result card.

WHAT IS PORTED, AND FROM WHERE
The trade loop, the mark-to-market equity curve, the ATR-stopped risk sizing,
the cost model and the metrics come from the production engine at
  ~/business-context/zero-one-restructure/trading-os/engine/trading_os_engine/
    backtest.py  simulate / compute_metrics / atr / _max_drawdown_pct
    data.py      resample and periods-per-year handling
Nothing about how a trade is opened, stopped, sized, charged or scored was
invented here.

WHERE THE BARS COME FROM
Not from here. `shared/ptq_data.py` is the one data layer days 4, 5 and 6 all
use, so the three days can never disagree about what a bar is. It runs on the
member's own free API key (day 1 sets that up), falls back to a CSV on disk or
an unkeyed public endpoint, and prints on the card which of those actually
served the bars and what the prices are adjusted for.

WHAT IS NEW
The signal layer. Day 3's spec is an arbitrary condition tree over an operand
allow-list, which the production engine expresses as a `composed` manifest.
Rather than reach into that, the allow-list is evaluated directly here, exactly
as SCHEMA.md defines it. The calibration layer (data window, trading calendar,
sufficiency gates) is also new and every part of it is printed on the card
rather than applied silently.

ONE DISCLOSED DIFFERENCE
ATR is the mean of true range over N bars — the production engine's definition
(`backtest.atr`). SCHEMA.md calls it Wilder true range; the true range is the
same, the smoothing is the simple mean rather than Wilder's. It is stated on
the card. The brief says port, do not invent, so the engine's version wins.

FAILURE POLICY
If the spec has unresolved questions, if the data is missing, too short, or the
rule never triggers, this prints why and exits non-zero. It never draws a curve
out of nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


def spec_fingerprint(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def bars_fingerprint(df: pd.DataFrame) -> str:
    # Include every simulator input and timestamp, independent of column order.
    payload = df.reindex(sorted(df.columns), axis=1).to_json(orient="split", date_format="iso", double_precision=15)
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_ptq_data():
    """Find the one shared data module.

    A skill is installed as its own folder, so day 4 and day 6 each ship a copy
    of `ptq_data.py` next to their scripts and both copies are generated from
    `shared/ptq_data.py` by `shared/sync.py`. Whichever copy is found first is
    the same file; if several are on disk the newest version wins, so a member
    who updated one day's skill and not another still gets one behaviour."""
    import importlib.util

    here = Path(__file__).resolve()
    candidates = [
        here.parent / "ptq_data.py",
        here.parents[3] / "shared" / "ptq_data.py",  # ptq-academy/shared
        Path.home() / ".ptq-academy" / "lib" / "ptq_data.py",
    ]
    for root in (Path.home() / ".claude" / "skills", Path.home() / "ptq-academy"):
        if root.is_dir():
            candidates.extend(sorted(root.glob("**/ptq_data.py"))[:6])

    best = None
    for c in candidates:
        if not c.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            f"ptq_data_{abs(hash(str(c)))}", c
        )
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
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
    if best is None:
        raise HonestFailure(
            "Cannot find ptq_data.py, the shared price-data module.\n"
            "It should sit next to this script. Reinstall the day-4 skill "
            "folder, or copy shared/ptq_data.py in beside run_backtest.py.\n"
            "Nothing was tested."
        )
    return best[1]


_DATA = None


def data_layer():
    global _DATA
    if _DATA is None:
        _DATA = _load_ptq_data()
    return _DATA


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPEC_VERSION = 1
DEFAULT_COST_BPS = 6.0  # per side: spread + commission + slippage
OHLCV = ["open", "high", "low", "close", "volume"]

# Calibration gates. Printed on the card, never applied quietly.
HARD_MIN_BARS = 250
THIN_BARS = 500
MAX_WARMUP_FRACTION = 0.20
DAY5_HOLDOUT_MONTHS = 12  # day 5 needs a real out-of-sample slice

# Where the member's workspace lives. First hit wins.
WORKSPACE_CANDIDATES = [
    Path.home() / "quant",
    Path.home() / "part-time-quant",
    Path.cwd(),
    Path.home() / "ptq",
    Path.home() / "Documents" / "part-time-quant",
]

# The bar sizes this runs. Same set as day 3's schema. How far back any one
# of them actually goes is the data provider's business, and whatever comes
# back is what the card reports — no ceiling is assumed here.
SUPPORTED_BARS = ("1d", "1wk", "1h", "30m", "15m", "5m", "1m")  # 1m: own CSV only
INTRADAY_BARS = ("1h", "30m", "15m", "5m", "1m")

# Bars per session. The "regular equity" column reproduces the production
# PERIODS_PER_YEAR table (252 x 7 for hourly, and so on).
BARS_PER_SESSION = {
    "regular_equity": {"1m": 390, "5m": 78, "15m": 26, "30m": 13, "1h": 7, "1d": 1},
    "round_clock": {"1m": 1440, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "1d": 1},
}
NEAR_24H_CLASSES = {"fx", "futures", "crypto"}


class HonestFailure(Exception):
    """Something is genuinely missing, blocked or insufficient. Say so and stop."""


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


# Where the card gets written as plain text, so a member who cannot copy out of
# their terminal can open the file instead. Set as soon as the spec is found.
WORKSPACE: Path = Path.home() / "quant"


def find_spec(explicit: str | None) -> Path:
    global WORKSPACE
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise HonestFailure(f"No spec at {p}.")
        WORKSPACE = p.parent
        return p
    for base in WORKSPACE_CANDIDATES:
        p = base / "strategy.json"
        if p.exists():
            WORKSPACE = p.parent
            return p
    raise HonestFailure(
        "Could not find strategy.json.\n"
        "It should be at ~/quant/strategy.json — that is what day 3 writes.\n"
        "Run the day-3 skill first. Nothing here invents a strategy for you."
    )


def validate_with_day3(spec_path: Path) -> None:
    """Day 3 ships the validator and SCHEMA.md says to run it before using a
    spec for anything. If it is on this machine, run it. If it is not, the
    checks below still catch everything day 4 depends on."""
    for candidate in (
        Path.home()
        / "ptq-academy"
        / "day-3"
        / "skill"
        / "scripts"
        / "validate_spec.py",
        Path(__file__).resolve().parents[3]
        / "day-3"
        / "skill"
        / "scripts"
        / "validate_spec.py",
    ):
        if not candidate.exists():
            continue
        result = subprocess.run(
            [sys.executable, str(candidate), str(spec_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise HonestFailure(
                "Your spec did not pass day 3's own checks, so nothing was "
                "tested.\n\n"
                + (result.stdout or result.stderr).strip()
                + "\n\nFix it in day 3 and come back."
            )
        return


def load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HonestFailure(f"{path} is not readable as JSON: {exc}")
    if not isinstance(spec, dict):
        raise HonestFailure(f"{path} should hold one strategy object.")

    version = spec.get("spec_version")
    if version != SPEC_VERSION:
        raise HonestFailure(
            f"This spec is version {version!r}. This skill reads version "
            f"{SPEC_VERSION}.\n"
            "Refusing rather than guessing what changed. Re-run day 3."
        )

    unresolved = spec.get("unresolved") or []
    if unresolved:
        items = "\n".join(f"  - {u}" for u in unresolved)
        raise HonestFailure(
            "Your spec still has open questions on it, so there is nothing "
            "honest to test yet:\n\n"
            f"{items}\n\n"
            "A backtest of a rule with a hole in it produces a number that "
            "means nothing, and the whole point of today is that the number "
            "means something.\n"
            "Go back to day 3, answer those, then run this again."
        )

    for key in (
        "name",
        "instrument",
        "timeframe",
        "direction",
        "entry",
        "exit",
        "stop",
        "sizing",
        "costs",
    ):
        if key not in spec:
            raise HonestFailure(
                f"The spec is missing {key!r}. Re-run day 3 — it writes every "
                "field this needs."
            )
    if spec["direction"] not in ("long", "short"):
        raise HonestFailure(
            f"direction is {spec['direction']!r}. Week one runs one side: long "
            "or short. Pick one in day 3."
        )
    if int(spec["entry"].get("max_open_positions", 1)) != 1:
        raise HonestFailure(
            f"Your spec allows {spec['entry']['max_open_positions']} positions "
            "open at once. This skill runs one position at a time.\n"
            "Set max_open_positions to 1 in day 3. Portfolios are a different "
            "problem and they are not week one."
        )
    return spec


# ---------------------------------------------------------------------------
# Series — the day-3 operand allow-list
# ---------------------------------------------------------------------------


def _true_range(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    return pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Ported from backtest.atr — the mean of true range over n bars."""
    return _true_range(df).rolling(n).mean()


def _adx(df: pd.DataFrame, n: int) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    a = 1.0 / n
    tr = _true_range(df).ewm(alpha=a, adjust=False).mean()
    pdi = (
        100 * pd.Series(plus_dm, index=df.index).ewm(alpha=a, adjust=False).mean() / tr
    )
    mdi = (
        100 * pd.Series(minus_dm, index=df.index).ewm(alpha=a, adjust=False).mean() / tr
    )
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=a, adjust=False).mean()


def _day_keys(index: pd.DatetimeIndex, tz: str, day_start: str) -> pd.Series:
    """Which trading day each bar belongs to, on the member's clock. A day that
    starts at 17:00 New York (the FX convention) puts a 18:00 bar in the next
    day. Daily and weekly bars are already one candle each."""
    hh, mm = (int(x) for x in day_start.split(":"))
    try:
        local = index.tz_convert(tz)
    except Exception as exc:  # noqa: BLE001 - unknown zone
        raise HonestFailure(f"The time zone {tz!r} in your spec is not one I know: {exc}")
    shifted = local - pd.Timedelta(hours=hh, minutes=mm)
    return pd.Series(shifted.tz_localize(None).normalize(), index=index)


def day_range(df: pd.DataFrame, p: dict) -> tuple[pd.Series, pd.Series]:
    """(high, low) of the day `days_back` days before each bar's own day.
    days_back 0 is today so far: the running high and low up to and including
    this bar, never the rest of the day."""
    keys = _day_keys(df.index, str(p["tz"]), str(p["day_start"]))
    back = int(p["days_back"])
    if back == 0:
        return df["high"].groupby(keys).cummax(), df["low"].groupby(keys).cummin()
    daily = pd.DataFrame({"h": df["high"], "l": df["low"], "k": keys}).groupby("k").agg(
        h=("h", "max"), l=("l", "min")
    )
    prior = daily.shift(back)
    return keys.map(prior["h"]).astype(float), keys.map(prior["l"]).astype(float)


def smt_divergence(df: pd.DataFrame, p: dict) -> pd.Series:
    """1.0 on a bar where one market takes out its lowest low (bullish) or highest
    high (bearish) of the `period` bars before it and the other market does not,
    0.0 otherwise. `taken_by` says which market has to take it out: main (the
    spec's symbol), second, or either. `within` widens it to this bar or the
    within-1 bars before it. Reads bars up to this one only. The second market's
    bars sit beside the main ones on the same timestamps (fetch_bars). A bar with
    no second-market price reads as no divergence; a missing second-market bar
    inside the lookback is skipped, not filled in."""
    if "second_low" not in df.columns:
        raise HonestFailure(
            "Your rule uses smt_divergence, which compares two markets, but no "
            "second market was loaded. Set instrument.second_symbol in day 3, "
            "plus second_csv_path when your prices come from a CSV. Nothing was "
            "tested."
        )
    n, within = int(p["period"]), int(p["within"])
    bull = p["side"] == "bullish"

    def took_held(field: str) -> tuple[pd.Series, pd.Series]:
        s = df[field]
        prior = s.shift(1).rolling(n, min_periods=1)
        level = prior.min() if bull else prior.max()
        if bull:
            return s < level, s >= level
        return s > level, s <= level  # both False where a price is missing

    a_took, a_held = took_held("low" if bull else "high")
    b_took, b_held = took_held("second_low" if bull else "second_high")
    main, second = a_took & b_held, b_took & a_held
    hit = {"main": main, "second": second, "either": main | second}[p["taken_by"]]
    hit = hit.astype(float)
    hit.iloc[:n] = np.nan  # no level to take out before `period` bars exist
    return hit.rolling(within, min_periods=1).max()


def series_values(df: pd.DataFrame, operand: dict) -> pd.Series:
    """One operand -> one aligned series. The allow-list is SCHEMA.md's."""
    name = operand["series"]
    p = operand.get("params") or {}
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    if name in ("open", "high", "low", "close", "volume"):
        out = df[name]
    elif name == "constant":
        out = pd.Series(float(p["value"]), index=df.index)
    elif name == "sma":
        out = close.rolling(int(p["period"])).mean()
    elif name == "ema":
        out = close.ewm(span=int(p["period"]), adjust=False).mean()
    elif name == "rsi":
        n = int(p["period"])
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
        loss = (-delta).clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
        out = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    elif name == "atr":
        out = atr(df, int(p["period"]))
    elif name == "stdev":
        out = close.rolling(int(p["period"])).std()
    elif name in ("highest", "lowest"):
        field = df[p.get("field", "close")]
        r = field.rolling(int(p["period"]))
        out = r.max() if name == "highest" else r.min()
    elif name == "pct_change":
        out = close.pct_change(int(p["period"])) * 100
    elif name == "volume_sma":
        out = vol.rolling(int(p["period"])).mean()
    elif name in ("bb_upper", "bb_lower"):
        n, mult = int(p["period"]), float(p["mult"])
        ma, sd = close.rolling(n).mean(), close.rolling(n).std()
        out = ma + mult * sd if name == "bb_upper" else ma - mult * sd
    elif name == "adx":
        out = _adx(df, int(p["period"]))
    elif name == "vwap":
        n = int(p["period"])
        typical = (high + low + close) / 3
        out = (typical * vol).rolling(n).sum() / vol.rolling(n).sum().replace(0, np.nan)
    elif name == "day_of_week":
        out = pd.Series(df.index.dayofweek, index=df.index, dtype=float)
    elif name == "pct_below_highest":
        field = df[p.get("field", "close")]
        hi = field.rolling(int(p["period"])).max()
        out = (hi - close) / hi.replace(0, np.nan) * 100
    elif name in ("day_level", "day_position"):
        hi, lo = day_range(df, p)
        width = (hi - lo).replace(0, np.nan)
        if name == "day_level":
            out = lo + (hi - lo) * float(p["level"]) / 100.0
        else:
            out = (close - lo) / width * 100.0
    elif name == "hour_of_day":
        try:
            local = df.index.tz_convert(str(p["tz"]))
        except Exception as exc:  # noqa: BLE001
            raise HonestFailure(f"The time zone {p['tz']!r} in your spec is not one I know: {exc}")
        out = pd.Series(local.hour + local.minute / 60.0, index=df.index, dtype=float)
    elif name == "smt_divergence":
        out = smt_divergence(df, p)
    elif name == "custom":
        raise HonestFailure(
            "Your rule contains a custom condition that day 3 could not put "
            "into a formula this can run:\n"
            f'  "{p.get("formula", "")}"\n'
            "That should have blocked the spec on day 3. Go back and either "
            "express it with the standard building blocks or drop it."
        )
    else:
        raise HonestFailure(
            f"The spec asks for a series called {name!r}, which is not one of "
            "the building blocks. Re-run day 3."
        )

    off = int(operand.get("offset", 0))
    return out.shift(off) if off else out


def operand_warmup(operand: dict) -> int:
    p = operand.get("params") or {}
    period = int(p["period"]) if isinstance(p.get("period"), (int, float)) else 0
    if operand.get("series") == "smt_divergence":
        period += int(p.get("within", 1))
    return period + int(operand.get("offset", 0))


def condition_series(df: pd.DataFrame, cond: dict) -> pd.Series:
    left = series_values(df, cond["left"])
    right = series_values(df, cond["right"])
    op = cond["op"]

    if op == ">":
        hit = left > right
    elif op == ">=":
        hit = left >= right
    elif op == "<":
        hit = left < right
    elif op == "<=":
        hit = left <= right
    elif op == "==":
        hit = left == right
    elif op == "crosses_above":
        hit = (left > right) & (left.shift(1) <= right.shift(1))
    elif op == "crosses_below":
        hit = (left < right) & (left.shift(1) >= right.shift(1))
    else:
        raise HonestFailure(f"The spec uses a comparison this cannot run: {op!r}.")

    hit = hit.fillna(False)
    persist = int(cond.get("persist_bars", 1) or 1)
    if persist > 1:
        hit = hit.rolling(persist).sum() == persist
        hit = hit.fillna(False)
    return hit.astype(bool)


def block_signal(df: pd.DataFrame, block: dict) -> pd.Series:
    """One entry/exit block -> a boolean series. `combine` is all or any."""
    conditions = block.get("conditions") or []
    if not conditions:
        return pd.Series(False, index=df.index)
    parts = [condition_series(df, c) for c in conditions]
    combined = parts[0]
    for part in parts[1:]:
        combined = (
            (combined & part) if block.get("combine") == "all" else (combined | part)
        )
    return combined


def spec_warmup(spec: dict) -> int:
    """Bars of history the rule needs before its first honest signal."""
    need = [int(spec["stop"].get("atr_period") or 0)]
    if spec["stop"].get("type") == "swing":
        need.append(int(spec["stop"]["value"]))
    for block in ("entry", "exit"):
        for cond in spec[block].get("conditions") or []:
            need.append(
                max(operand_warmup(cond["left"]), operand_warmup(cond["right"]))
                + int(cond.get("persist_bars", 1) or 1)
            )
    return max(need + [2])


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def fetch_bars(spec: dict, symbol: str, bar: str) -> tuple[pd.DataFrame, str, bool]:
    """(bars, where they came from, whether the history is shorter than asked).

    All of the real work is in the shared data layer. This is the adapter that
    turns its bars into the DataFrame the rest of day 4 runs on, and nothing
    else. Provider choice, keys, caching and the honest-failure text all live
    in one place so days 4, 5 and 6 cannot drift apart.
    """
    inst = spec["instrument"]
    df, source, clipped = _fetch_one(
        spec, symbol, bar, inst.get("csv_path") if inst.get("data_source") == "csv" else None
    )
    if inst.get("second_symbol"):
        df, source = attach_second(df, source, spec, symbol, bar)
    return df, source, clipped


# A second market (smt_divergence) is compared bar by bar on the same timestamps.
# Below this share of matching bar times, or this return correlation, the two
# files are not on the same bars or the same clock, and the test is refused.
SECOND_MATCH_MIN = 0.90
SECOND_CORR_MIN = 0.20


def _returns(close: pd.Series) -> pd.Series:
    return close / close.shift(1) - 1.0


def attach_second(
    df: pd.DataFrame, source: str, spec: dict, symbol: str, bar: str
) -> tuple[pd.DataFrame, str]:
    """Load instrument.second_symbol through the same data routing as the main
    symbol (same data_source and asset class; its own CSV when the spec reads
    CSVs) and put its high and low beside each main bar with the same timestamp.
    Nothing is shifted or filled forward. Refuses in plain words when it has no
    data or its bars do not line up with the main ones."""
    inst = spec["instrument"]
    second = str(inst["second_symbol"])
    csv = inst.get("data_source") == "csv"
    if csv and not inst.get("second_csv_path"):
        raise HonestFailure(
            f"Your rule compares {symbol} with {second} (SMT divergence) and your "
            f"prices come from a CSV, but the spec has no second_csv_path for "
            f"{second}. Export {second} the same way as {symbol} (same bar size, "
            "same dates, same clock), add the file in day 3, and run this again. "
            "Nothing was tested."
        )
    try:
        df2, source2, _ = _fetch_one(
            spec, second, bar, inst.get("second_csv_path") if csv else None
        )
    except HonestFailure as exc:
        raise HonestFailure(
            f"Your rule compares {symbol} with {second} (SMT divergence), and "
            f"{second} could not be loaded, so nothing was tested.\n\n{exc}"
        )

    def span(frame: pd.DataFrame) -> str:
        return (
            f"{frame.index[0].strftime('%Y-%m-%d %H:%M')} to "
            f"{frame.index[-1].strftime('%Y-%m-%d %H:%M')} UTC"
        )

    matched = df2.reindex(df.index)
    share = float(matched["low"].notna().mean())
    if share < SECOND_MATCH_MIN:
        raise HonestFailure(
            f"{second}'s bars line up with only {int(share * 100)}% of {symbol}'s bar "
            f"times. {symbol} runs {span(df)}, {second} runs {span(df2)}.\n"
            "SMT divergence compares the two bar by bar, so both need the same "
            "bar size, the same dates and the same clock (time zone). Export both "
            "the same way and run this again. Nothing was tested."
        )
    corr = _returns(df["close"]).corr(_returns(matched["close"]))
    corr = 0.0 if pd.isna(corr) else float(corr)
    if corr < SECOND_CORR_MIN:
        if corr <= -SECOND_CORR_MIN:
            why = (
                f"{symbol} and {second} move in opposite directions on these bars "
                f"(correlation {corr:.2f}). SMT divergence here needs two markets "
                f"that move together, like EURUSD and GBPUSD."
            )
        else:
            why = (
                f"{symbol} and {second} barely move together on these bars "
                f"(correlation {corr:.2f}). Two markets that normally move together "
                "doing that usually means the two files are on different clocks."
            )
            best = (corr, 0)
            for h in [x for x in range(-12, 13) if x]:
                moved = df2.set_axis(df2.index + pd.Timedelta(hours=h)).reindex(df.index)
                c = _returns(df["close"]).corr(_returns(moved["close"]))
                if pd.notna(c) and c > best[0] + 0.2:
                    best = (float(c), h)
            if best[1]:
                why += (
                    f" They line up when {second}'s times are moved "
                    f"{abs(best[1])} hour{'s' if abs(best[1]) != 1 else ''} "
                    f"{'later' if best[1] > 0 else 'earlier'}, so one file is "
                    "probably in a different time zone. Export both in UTC."
                )
        raise HonestFailure(why + "\nNothing was tested.")

    df["second_high"] = matched["high"].astype(float)
    df["second_low"] = matched["low"].astype(float)
    missing = int(matched["low"].isna().sum())
    if missing:
        df.attrs["second_note"] = (
            f"{missing:,} of {symbol}'s {len(df):,} bars have no {second} bar at "
            "the same time. SMT divergence reads those bars as no divergence."
        )
    return df, f"{source}; {second}: {source2}"


def _fetch_one(
    spec: dict, symbol: str, bar: str, csv_path: str | None
) -> tuple[pd.DataFrame, str, bool]:
    d = data_layer()
    inst = spec["instrument"]
    start = spec["timeframe"].get("history_start", "2010-01-01")

    if bar not in SUPPORTED_BARS:
        raise HonestFailure(
            f"Timeframe {bar!r} is not one this skill runs. "
            f"Use one of: {', '.join(SUPPORTED_BARS)}."
        )

    try:
        bars, meta = d.get_bars(
            symbol,
            bar,
            start,
            data_source=inst.get("data_source", "auto"),
            csv_path=csv_path,
            asset_class=inst.get("asset_class", "equity"),
            # Day 4 is research on history. Bars a few hours old are the same
            # bars, and reusing them keeps the member well inside the free
            # rate limits when day 5 re-runs on the same data tomorrow.
            max_age=6 * 3600,
            allow_stale=False,
        )
    except d.DataUnavailable as exc:
        raise HonestFailure(str(exc))
    except Exception as exc:  # noqa: BLE001 - report anything, honestly
        raise HonestFailure(
            f"Could not get prices for {symbol}. It said: "
            f"{type(exc).__name__}: {exc}\nNothing was tested."
        )

    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df = df[df["time"].notna()].set_index("time").sort_index()
    df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if df.empty:
        raise HonestFailure(
            f"{meta['source']} returned bars, but none of them are on or after "
            f"{start}, which is the history_start in your spec. Nothing was "
            "tested."
        )

    # "Clipped" now means the plain, provider-agnostic thing: the history that
    # came back starts later than the spec asked for. Intraday bars are where
    # this bites, because every feed keeps far less of them.
    asked = pd.Timestamp(start, tz="UTC")
    clipped = bool((df.index[0] - asked).days > 5)

    df.attrs["adjustment"] = meta.get("adjustment", "unknown")
    df.attrs["provider"] = meta.get("provider", "unknown")
    return df[OHLCV].astype(float), meta["source"], clipped


def check_sufficiency(
    df: pd.DataFrame, warmup: int, symbol: str, bar: str
) -> list[str]:
    n = len(df)
    if n < HARD_MIN_BARS:
        raise HonestFailure(
            f"Only {n} bars of {bar} history came back for {symbol}. This needs "
            f"at least {HARD_MIN_BARS}.\n"
            "No result was produced, because a result off this little data "
            "would be meaningless.\n"
            "Run it on 1d bars, which go back years, or push history_start "
            "further back in day 3."
        )
    if warmup > n * MAX_WARMUP_FRACTION:
        raise HonestFailure(
            f"Your rule needs {warmup} bars of history before it can produce "
            f"its first signal, and there are only {n} bars. That is too much "
            "of the sample spent warming up.\n"
            "No result was produced. Keep your strategy rules and timeframe unchanged. "
            "Check the requested history_start and the dates the provider returned. "
            "Request more history for the same rule if available, or use a verified "
            "CSV of the same instrument and timeframe. If enough history is unavailable, "
            "keep this test blocked. Shortening a lookback creates a different strategy; "
            "it is not a data repair."
        )

    warnings = []
    if n < THIN_BARS:
        warnings.append(
            f"THIN DATA: {n} bars. Enough to run, not much to judge. Tomorrow's "
            "holdout will be short."
        )
    span_days = (df.index[-1] - df.index[0]).days
    if span_days < DAY5_HOLDOUT_MONTHS * 30 * 1.5:
        warnings.append(
            f"SHORT SPAN: {span_days} days of history. Day 5 wants at least "
            f"{DAY5_HOLDOUT_MONTHS} months held back that your rule has never "
            "seen. There may not be enough here to give it that."
        )
    return warnings


# ---------------------------------------------------------------------------
# The backtest — ported trade loop, extended for the spec's options
# ---------------------------------------------------------------------------


def _max_drawdown_pct(equity: pd.Series) -> float:
    """Ported from backtest._max_drawdown_pct."""
    if len(equity) == 0:
        return 0.0
    peak = equity.cummax()
    return float(((equity - peak) / peak).min() * -100)


def simulate(df: pd.DataFrame, spec: dict, warmup: int) -> dict:
    """Ported from backtest.simulate. The trade construction is unchanged:
    mark-to-market equity at every bar's close, a realised exit-only curve
    alongside, costs charged both sides and booked at the close of the trade,
    a stop distance that sizing is worked back from, and a position still open
    at the end reported as open rather than given an invented exit.

    Extended, per the day-3 spec, with: next-bar-open fills, exit condition
    blocks, time stops, percent stops and targets, trailing stops, close-only
    stop checking, a cooldown, and fixed-fraction sizing."""
    entry_sig = block_signal(df, spec["entry"]).to_numpy().copy()
    exit_sig = block_signal(df, spec["exit"]).to_numpy().copy()
    entry_sig[:warmup] = False
    exit_sig[:warmup] = False

    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(df)

    stop_cfg, sizing, direction = spec["stop"], spec["sizing"], spec["direction"]
    side = 1.0 if direction == "long" else -1.0
    cost_bps = float(spec["costs"]["per_side_bps"])
    equity = float(sizing["starting_equity"])
    start_equity = equity

    a = (
        atr(df, int(stop_cfg["atr_period"])).to_numpy()
        if stop_cfg["type"] == "atr_multiple"
        else np.zeros(n)
    )
    # Swing stop: the lowest low (long) or highest high (short) of the last N
    # bars up to the bar the signal fired on. Known before the entry fills.
    if stop_cfg["type"] == "swing":
        look = int(stop_cfg["value"])
        swing = (
            df["low"].rolling(look).min() if side > 0 else df["high"].rolling(look).max()
        ).to_numpy()
    else:
        swing = np.zeros(n)
    target_cfg = spec["exit"].get("target")
    time_stop = spec["exit"].get("time_stop_bars")
    entry_next_open = spec["entry"].get("fill", "next_bar_open") == "next_bar_open"
    exit_next_open = spec["exit"].get("fill", "next_bar_open") == "next_bar_open"
    cooldown = int(spec["entry"].get("cooldown_bars", 0) or 0)
    trailing = bool(stop_cfg.get("trailing"))
    intrabar = bool(stop_cfg.get("intrabar"))

    curve = np.empty(n)  # mark-to-market
    realized_curve = np.empty(n)  # exit-only
    trades: list[float] = []
    events: list[dict] = []
    in_pos = False
    qty = entry = stop = target = entry_cost = stop_dist = 0.0
    entry_index = -1
    extreme = 0.0
    flat_since = -(10**9)
    current: dict | None = None
    total_cost = 0.0
    no_stop_refusal = stop_cfg["type"] == "none"

    def close_trade(i: int, price: float, reason: str) -> None:
        nonlocal in_pos, equity, total_cost, current, qty, flat_since
        exit_cost = qty * price * cost_bps / 1e4
        pnl = qty * (price - entry) * side - entry_cost - exit_cost
        equity += pnl
        total_cost += entry_cost + exit_cost
        trades.append(pnl)
        if current is not None:
            current.update(
                {
                    "exitIndex": i,
                    "exitPrice": float(price),
                    "exitReason": reason,
                    "pnl": float(pnl),
                }
            )
            events.append(current)
            current = None
        in_pos = False
        qty = 0.0
        flat_since = i

    for i in range(n):
        if in_pos:
            bars_held = i - entry_index
            exited = False

            # 1. Anything decided on the previous bar fills at this open.
            if exit_next_open:
                if time_stop and bars_held > int(time_stop):
                    close_trade(i, o[i], "time")
                    exited = True
                elif exit_sig[i - 1]:
                    close_trade(i, o[i], "signal")
                    exited = True

            # 2. Then the bar trades, and the stop or target can be hit in it.
            if not exited:
                if trailing:
                    extreme = max(extreme, h[i]) if side > 0 else min(extreme, l[i])
                    trailed = extreme - side * stop_dist
                    stop = max(stop, trailed) if side > 0 else min(stop, trailed)
                probe_low = l[i] if intrabar else c[i]
                probe_high = h[i] if intrabar else c[i]
                if stop_dist > 0:
                    if side > 0 and probe_low <= stop:
                        close_trade(i, stop, "stop")
                        exited = True
                    elif side < 0 and probe_high >= stop:
                        close_trade(i, stop, "stop")
                        exited = True
                if not exited and target > 0:
                    if side > 0 and probe_high >= target:
                        close_trade(i, target, "target")
                        exited = True
                    elif side < 0 and probe_low <= target:
                        close_trade(i, target, "target")
                        exited = True

            # 3. Same-bar-close exits settle last.
            if not exited and not exit_next_open:
                if time_stop and bars_held >= int(time_stop):
                    close_trade(i, c[i], "time")
                elif exit_sig[i]:
                    close_trade(i, c[i], "signal")

        if not in_pos and i - flat_since > cooldown:
            fires = entry_sig[i - 1] if entry_next_open else entry_sig[i]
            price = o[i] if entry_next_open else c[i]
            if fires and i > 0 and price > 0:
                if stop_cfg["type"] == "atr_multiple":
                    if math.isnan(a[i]) or a[i] <= 0:
                        fires = False
                    else:
                        stop_dist = float(stop_cfg["value"]) * a[i]
                elif stop_cfg["type"] == "percent":
                    stop_dist = price * float(stop_cfg["value"]) / 100.0
                elif stop_cfg["type"] == "swing":
                    j = i - 1 if entry_next_open else i
                    level = swing[j]
                    dist = (price - level) * side if not math.isnan(level) else 0.0
                    if dist <= 0:
                        # Price is already through the swing point: there is
                        # no stop to place, so there is no trade.
                        fires = False
                    else:
                        stop_dist = float(dist)
                else:
                    stop_dist = 0.0

            if fires and i > 0 and price > 0:
                entry = price
                entry_index = i
                extreme = price
                stop = entry - side * stop_dist if stop_dist > 0 else 0.0
                if target_cfg:
                    if target_cfg["type"] == "atr_multiple":
                        tdist = float(target_cfg["value"]) * a[i]
                    elif target_cfg["type"] == "r_multiple":
                        tdist = float(target_cfg["value"]) * stop_dist
                    else:
                        tdist = entry * float(target_cfg["value"]) / 100.0
                    target = entry + side * tdist
                else:
                    target = 0.0

                # Sizing. Fixed-fraction sizing worked back from the stop
                # distance is the ported behaviour; the spec's fixed_fraction
                # method sizes off notional instead.
                if sizing["method"] == "risk_percent":
                    if stop_dist <= 0:
                        raise HonestFailure(
                            "Your spec sizes by risk per trade but has no stop, "
                            "so there is nothing to size against.\n"
                            "Either add a stop or switch sizing.method to "
                            "fixed_fraction in day 3."
                        )
                    qty = max(
                        (equity * float(sizing["risk_per_trade_pct"]) / 100.0)
                        / stop_dist,
                        0.0,
                    )
                else:
                    qty = max(
                        (equity * float(sizing["fraction_pct"]) / 100.0) / entry, 0.0
                    )
                entry_cost = qty * entry * cost_bps / 1e4
                in_pos = True
                current = {
                    "entryIndex": i,
                    "entryPrice": float(entry),
                    "side": "long" if side > 0 else "short",
                    "qty": float(qty),
                    "stop": float(stop),
                    "target": float(target),
                }

        realized_curve[i] = equity
        curve[i] = equity + (qty * (c[i] - entry) * side if in_pos else 0.0)

    if in_pos and current is not None:
        # Open at the end of the data. Reported open, never given an invented
        # exit or an invented profit.
        current.update(
            {"exitIndex": None, "exitPrice": None, "exitReason": None, "pnl": None}
        )
        events.append(current)

    return {
        "equity": pd.Series(curve, index=df.index),
        "realizedEquity": pd.Series(realized_curve, index=df.index),
        "trades": trades,
        "events": events,
        "total_cost": total_cost,
        "openAtEnd": in_pos,
        "startEquity": start_equity,
        "noStop": no_stop_refusal,
    }


def compute_metrics(
    equity: pd.Series,
    trades: list[float],
    total_cost: float,
    ppy: float,
    start_equity: float,
    realized_equity: pd.Series | None = None,
) -> dict:
    """Ported from backtest.compute_metrics. Only the periods-per-year figure
    is passed in rather than derived from an interval string."""
    rets = equity.pct_change().dropna().to_numpy()
    mean, sd = (
        (float(rets.mean()), float(rets.std(ddof=1))) if len(rets) > 2 else (0.0, 0.0)
    )
    sharpe = (mean / sd * math.sqrt(ppy)) if sd > 0 else 0.0
    downside = rets[rets < 0]
    dsd = float(downside.std(ddof=1)) if len(downside) > 2 else 0.0
    sortino = (mean / dsd * math.sqrt(ppy)) if dsd > 0 else 0.0

    max_dd = _max_drawdown_pct(equity)
    realized_dd = _max_drawdown_pct(
        realized_equity if realized_equity is not None else equity
    )

    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
    gp, gl = sum(wins), abs(sum(losses))
    profit_factor = (gp / gl) if gl > 0 else (gp if gp > 0 else 0.0)
    net = float((equity.iloc[-1] / equity.iloc[0] - 1) * 100) if len(equity) else 0.0

    return {
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "maxDrawdownPct": round(max_dd, 1),
        "realizedMaxDrawdownPct": round(realized_dd, 1),
        "winRatePct": round(win_rate, 1),
        "profitFactor": round(min(profit_factor, 99), 2),
        "trades": len(trades),
        "netReturnPct": round(net, 1),
        "costDragPct": round(total_cost / start_equity * 100, 1),
    }


def extra_stats(df: pd.DataFrame, trades: list[float], events: list[dict]) -> dict:
    """Context rows the member can actually use. Plain arithmetic over the same
    trades the ported loop produced. Buy-and-hold is here because a rule that
    trails simply owning the thing is worth knowing about before anyone gets
    excited."""
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    streak = worst_streak = 0
    for t in trades:
        streak = streak + 1 if t <= 0 else 0
        worst_streak = max(worst_streak, streak)
    bars_in = sum(
        (e["exitIndex"] if e["exitIndex"] is not None else len(df) - 1)
        - e["entryIndex"]
        for e in events
    )
    hold = float((df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100)
    return {
        "avgWin": round(sum(wins) / len(wins), 0) if wins else 0.0,
        "avgLoss": round(sum(losses) / len(losses), 0) if losses else 0.0,
        "bestTrade": round(max(trades), 0) if trades else 0.0,
        "worstTrade": round(min(trades), 0) if trades else 0.0,
        "longestLosingStreak": worst_streak,
        "timeInMarketPct": round(bars_in / len(df) * 100, 1) if len(df) else 0.0,
        "buyAndHoldPct": round(hold, 1),
    }


def periods_per_year(bar: str, session: str, asset_class: str) -> tuple[float, str]:
    """(bars per year, the note printed on the card). For a regular equity
    session this reproduces the production PERIODS_PER_YEAR table exactly."""
    if bar == "1wk":
        return 52.0, "52 weeks"
    round_clock = session == "24h" or asset_class in NEAR_24H_CLASSES
    table = BARS_PER_SESSION["round_clock" if round_clock else "regular_equity"]
    per_session = table.get(bar, 1)
    if session == "24h":
        return 365 * per_session, "365-day calendar, this market never closes"
    if round_clock:
        return 252 * per_session, "252 sessions, near-24-hour sessions"
    return 252 * per_session, "252 sessions, 6.5-hour equity day"


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

W = 66


def line(t: str = "") -> str:
    return "  " + t


def rule_line(ch: str = "-") -> str:
    return "  " + ch * W


def row(label: str, value: str) -> str:
    return "  " + label.ljust(30) + value


def render_card(p: dict) -> str:
    m, d, s, x = p["metrics"], p["data"], p["strategy"], p["context"]
    ccy = d["currency"]
    out = [
        "",
        rule_line("="),
        line(f"DAY 4 BACKTEST  ·  {s['name'].upper()}"),
        rule_line("="),
        "",
        line("THE RULE"),
    ]
    for label, text in (("In: ", s["entryPlain"]), ("Out:", s["exitPlain"])):
        for j, seg in enumerate(textwrap.wrap(text, W - 8) or [""]):
            out.append(line(f"  {label if j == 0 else '    '} {seg}"))
    out += [
        "",
        row("Direction", s["direction"]),
        row("Stop", s["stopHuman"]),
        row("Target", s["targetHuman"]),
        row("Size", s["sizingHuman"]),
        row("Fills", s["fillHuman"]),
        "",
        row("Asset", f"{d['symbol']}  ({d['assetClass']}, {ccy})"),
        *(
            [row("Compared with", f"{d['secondSymbol']}  (SMT divergence, same bar times)")]
            if d.get("secondSymbol")
            else []
        ),
        row("Timeframe", d["bar"]),
        row("Bars tested", f"{d['bars']:,}  (warm-up {d['warmup']})"),
        row("Period", f"{d['start']} to {d['end']}"),
        row("Data", d["source"]),
        row("Prices adjusted for", d.get("adjustment", "unknown")),
        row("Costs", f"{d['costBps']:g}bps per side, both sides charged"),
        row("Annualised on", f"{d['ppy']:,.0f} bars/year ({d['calendarNote']})"),
        row("ATR", "mean of true range, as in the PTQ engine"),
        "",
        rule_line(),
        line("RESULT"),
        rule_line(),
        row("Net return", f"{m['netReturnPct']:+.1f}%"),
        row("Sharpe", f"{m['sharpe']:.2f}"),
        row("Sortino", f"{m['sortino']:.2f}"),
        row("Max drawdown", f"{m['maxDrawdownPct']:.1f}%  (mark-to-market)"),
        row("Max drawdown, exits only", f"{m['realizedMaxDrawdownPct']:.1f}%"),
        row("Trades", f"{m['trades']}"),
        row("Win rate", f"{m['winRatePct']:.1f}%"),
        row("Profit factor", f"{m['profitFactor']:.2f}"),
        row("Cost drag", f"{m['costDragPct']:.1f}% of starting equity"),
        "",
        rule_line(),
        line("CONTEXT"),
        rule_line(),
        row("Average win / loss", f"{x['avgWin']:+,.0f} / {x['avgLoss']:+,.0f}"),
        row("Best / worst trade", f"{x['bestTrade']:+,.0f} / {x['worstTrade']:+,.0f}"),
        row("Longest run of losers", f"{x['longestLosingStreak']} in a row"),
        row("Time holding a position", f"{x['timeInMarketPct']:.1f}% of the bars"),
        row("Same period, just holding", f"{x['buyAndHoldPct']:+.1f}%"),
        line(f"(money figures in {ccy}, on {d['startEquity']:,.0f} starting equity)"),
        "",
    ]
    for w in p.get("warnings", []):
        for wrapped in textwrap.wrap(w, W):
            out.append(line(wrapped))
    if p.get("warnings"):
        out.append("")
    out += [
        rule_line(),
        line("WHAT THIS NUMBER IS"),
        rule_line(),
        line("A rule measured against prices that have already happened."),
        line("Fitted to the past. The past is the one thing you cannot trade."),
        "",
        line("What it does NOT tell you yet:"),
        line("  · whether the result is the rule or the luck of one sample"),
        line("  · whether it holds on data the rule has never seen"),
        line("  · whether it survives being one of many settings you could"),
        line("    have picked"),
        "",
        line("Day 5 runs those three checks. Do not act on this number, do not"),
        line("change your rule because of it, and do not call it a win."),
        line("Post it. Sit with it. Find out tomorrow."),
        "",
        rule_line("="),
    ]
    return "\n".join(out)


def stop_human(stop: dict) -> str:
    trail = " trailing" if stop.get("trailing") else ""
    where = "checked intrabar" if stop.get("intrabar") else "checked on the close"
    if stop["type"] == "atr_multiple":
        return f"{stop['value']:g}x ATR({stop['atr_period']}){trail}, {where}"
    if stop["type"] == "percent":
        return f"{stop['value']:g}% from entry{trail}, {where}"
    if stop["type"] == "swing":
        return f"swing point of last {stop['value']:g} bars{trail}, {where}"
    return "none"


def target_human(target: dict | None, time_stop) -> str:
    parts = []
    if target:
        parts.append(
            {
                "atr_multiple": f"{target['value']:g}x ATR",
                "r_multiple": f"{target['value']:g}R (times the stop distance)",
            }.get(target["type"], f"{target['value']:g}%")
        )
    if time_stop:
        parts.append(f"time stop {int(time_stop)} bars")
    return ", ".join(parts) if parts else "none"


def sizing_human(sizing: dict) -> str:
    if sizing["method"] == "risk_percent":
        return f"risk {sizing['risk_per_trade_pct']:g}% of equity per trade"
    return f"{sizing['fraction_pct']:g}% of equity per position"


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Day 4 backtest")
    ap.add_argument("--spec", default=None, help="path to strategy.json")
    ap.add_argument("--ticker", default=None, help="override the spec's symbol")
    ap.add_argument("--timeframe", default=None, help="override the spec's bar")
    ap.add_argument("--out", default=None, help="where to write backtest-result.json")
    args = ap.parse_args()

    spec_path = find_spec(args.spec)
    attempt_dir = Path(args.out).expanduser().parent if args.out else spec_path.parent
    attempt_marker = attempt_dir / ".day4-incomplete"
    attempt_marker.write_text("The latest backtest has not completed successfully.\n")
    validate_with_day3(spec_path)
    spec = load_spec(spec_path)

    symbol = args.ticker or spec["instrument"]["symbol"]
    bar = args.timeframe or spec["timeframe"]["bar"]
    overrides = []
    if args.ticker and args.ticker != spec["instrument"]["symbol"]:
        overrides.append(f"symbol {spec['instrument']['symbol']} -> {symbol}")
    if args.timeframe and args.timeframe != spec["timeframe"]["bar"]:
        overrides.append(f"bar {spec['timeframe']['bar']} -> {bar}")

    df, source, clipped = fetch_bars(spec, symbol, bar)
    warmup = spec_warmup(spec)
    warnings = check_sufficiency(df, warmup, symbol, bar)
    if clipped:
        warnings.append(
            f"The data source had {bar} bars back to "
            f"{df.index[0].strftime('%d %b %Y')} only, not the "
            f"{spec['timeframe']['history_start']} in your spec. "
            + (
                "Intraday history is short on every free feed. "
                if bar in INTRADAY_BARS
                else ""
            )
            + "The test ran on what actually exists."
        )
    adj = df.attrs.get("adjustment", "unknown")
    if adj == "however you saved it":
        warnings.append(
            "These prices come from your own CSV, so any split or dividend "
            "adjustment is whatever that file already has. On shares or an "
            "ETF that pays dividends, missing dividends understate the return "
            "and the comparison to just holding."
        )
    elif adj not in ("splits and dividends",):
        warnings.append(
            f"These prices are adjusted for: {adj}. A long test on shares or "
            "an ETF that pays dividends understates the return when the "
            "dividends are missing. The comparison to just holding is affected "
            "the same way. A Tiingo key gives dividend-adjusted bars."
        )
    if df.attrs.get("second_note"):
        warnings.append(df.attrs["second_note"])
    if overrides:
        warnings.append(
            "Run with overrides you typed today, not what is in the spec: "
            + "; ".join(overrides)
            + ". The spec file was not changed."
        )

    sim = simulate(df, spec, warmup)

    if not sim["trades"] and not sim["openAtEnd"]:
        raise HonestFailure(
            f"Your rule never triggered once across {len(df):,} bars of {bar} "
            f"{symbol}.\n"
            "There is no result to show, and a flat line is not a backtest.\n"
            "That is real information: on this asset, on this timeframe, your "
            "rule as written does nothing.\n"
            "Either the conditions are tighter than you realised, or this is "
            "the wrong asset for it. Post that finding instead — it counts."
        )

    ppy, calendar_note = periods_per_year(
        bar,
        spec["timeframe"].get("session", "regular"),
        spec["instrument"].get("asset_class", "equity"),
    )
    metrics = compute_metrics(
        sim["equity"],
        sim["trades"],
        sim["total_cost"],
        ppy,
        sim["startEquity"],
        sim["realizedEquity"],
    )

    if metrics["trades"] < 30:
        warnings.append(
            f"FEW TRADES: {metrics['trades']}. Under about 30 trades none of "
            "these numbers mean much. Tomorrow will say so louder."
        )
    if sim["openAtEnd"]:
        warnings.append(
            "One position is still open at the end of the data. It has no exit "
            "and no booked profit or loss. It is marked at the last close, not "
            "given an invented result."
        )

    payload = {
        "result_version": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "specPath": str(spec_path),
        "inputIdentity": {"version": 1, "specSha256": spec_fingerprint(spec), "barsSha256": bars_fingerprint(df)},
        "specName": spec["name"],
        "specCreated": spec.get("created"),
        "strategy": {
            "name": spec["name"],
            "direction": spec["direction"],
            "entryPlain": spec["entry"]["plain_english"],
            "exitPlain": spec["exit"]["plain_english"],
            "stopHuman": stop_human(spec["stop"]),
            "targetHuman": target_human(
                spec["exit"].get("target"), spec["exit"].get("time_stop_bars")
            ),
            "sizingHuman": sizing_human(spec["sizing"]),
            "fillHuman": (
                f"in {spec['entry'].get('fill')}, out {spec['exit'].get('fill')}"
            ),
        },
        "data": {
            "symbol": symbol,
            **(
                {"secondSymbol": spec["instrument"]["second_symbol"]}
                if spec["instrument"].get("second_symbol")
                else {}
            ),
            "assetClass": spec["instrument"].get("asset_class", "equity"),
            "currency": spec["instrument"].get("currency", "USD"),
            "bar": bar,
            "session": spec["timeframe"].get("session", "regular"),
            "bars": len(df),
            "warmup": warmup,
            "start": df.index[0].strftime("%d %b %Y"),
            "end": df.index[-1].strftime("%d %b %Y"),
            "startISO": df.index[0].strftime("%Y-%m-%d"),
            "endISO": df.index[-1].strftime("%Y-%m-%d"),
            "source": source,
            "provider": df.attrs.get("provider", "unknown"),
            "adjustment": df.attrs.get("adjustment", "unknown"),
            "costBps": float(spec["costs"]["per_side_bps"]),
            "startEquity": sim["startEquity"],
            "ppy": ppy,
            "calendarNote": calendar_note,
            "historyStartClipped": clipped,
        },
        "metrics": metrics,
        "context": extra_stats(df, sim["trades"], sim["events"]),
        "methodology": {
            "markToMarket": True,
            "markPrice": "close",
            "costBooking": "trade-close",
            "atr": "mean of true range over N bars (PTQ engine definition)",
            "note": (
                "Open positions are valued bar-by-bar at each bar's close, so "
                "drawdown and the Sharpe inputs reflect intra-trade swings, not "
                "only realised exits. Costs are charged on both sides and booked "
                "when the trade closes."
            ),
        },
        "warnings": warnings,
        "provenance": "ported-from-trading-os-engine",
    }

    card = render_card(payload)
    print(card)

    payload["equityCurve"] = [
        {"t": ts.strftime("%Y-%m-%d %H:%M"), "equity": round(float(v), 2)}
        for ts, v in sim["equity"].items()
    ]
    payload["realizedEquityCurve"] = [
        {"t": ts.strftime("%Y-%m-%d %H:%M"), "equity": round(float(v), 2)}
        for ts, v in sim["realizedEquity"].items()
    ]
    payload["tradePnl"] = [round(float(t), 2) for t in sim["trades"]]
    payload["trades"] = [
        {
            **e,
            "entryTime": df.index[e["entryIndex"]].strftime("%Y-%m-%d %H:%M"),
            "exitTime": (
                df.index[e["exitIndex"]].strftime("%Y-%m-%d %H:%M")
                if e["exitIndex"] is not None
                else None
            ),
        }
        for e in sim["events"]
    ]

    out_path = (
        Path(args.out).expanduser()
        if args.out
        else spec_path.parent / "backtest-result.json"
    )
    out_path.write_text(json.dumps(payload, indent=2))
    attempt_marker.unlink(missing_ok=True)
    card_path = out_path.parent / "backtest-card.txt"
    try:
        card_path.write_text(card + "\n", encoding="utf-8")
    except OSError:
        card_path = None
    print(line(f"Saved for day 5: {out_path}"))
    if card_path is not None:
        print(line(f"Card as text:   {card_path}"))
    print("")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HonestFailure as exc:
        out = ["", "  " + "=" * W, "  NO RESULT", "  " + "=" * W, ""]
        for ln in str(exc).splitlines():
            # Never wrap a line carrying a URL or an indented instruction —
            # a broken link in the one message a stuck member has to act on
            # is worse than a ragged right edge.
            if "http" in ln or ln.startswith(" ") or len(ln) <= W:
                out.append("  " + ln)
                continue
            for wrapped in textwrap.wrap(ln, W) or [""]:
                out.append("  " + wrapped)
        out += [
            "",
            "  Nothing was estimated, guessed or filled in. There is no card",
            "  because there is no honest card to print.",
            "",
        ]
        # The member is asked to post this block. Put it in a file as well so
        # they never have to copy it out of a terminal window.
        try:
            refusal_path = WORKSPACE / "backtest-card.txt"
            refusal_path.write_text("\n".join(out).strip("\n") + "\n", encoding="utf-8")
            out.append(f"  This block is also saved at: {refusal_path}")
            out.append("")
        except OSError:
            pass
        print("\n".join(out))
        sys.exit(2)
