#!/usr/bin/env python3
"""Day 5 — edge or luck.

Three checks on yesterday's backtest:
  1. PBO   — probability of backtest overfitting, via combinatorially
             symmetric cross-validation (Bailey, Borwein, Lopez de Prado,
             Zhu, "The Probability of Backtest Overfitting", 2016).
  2. DSR   — deflated Sharpe ratio (Bailey & Lopez de Prado, "The Deflated
             Sharpe Ratio", 2014), built on the probabilistic Sharpe ratio
             (same authors, 2012).
  3. OOS   — anchored K-fold walk-forward. The configuration is re-chosen on
             in-sample rows only, scored on the rows immediately after, and
             the out-of-sample segments are stitched into one honest stream.

The maths here is ported from the PTQ engine's validation module, not
re-derived. Same formulas, same gate thresholds, same verdict rule.

No scipy. Normal CDF by erf, inverse normal by Acklam's approximation.

It does not run a new backtest. It reads day 4's result from the member's
workspace (~/quant), rebuilds the settings grid using DAY 4's OWN simulator so
the two days can never disagree, then judges it. Writes verdict.json
for day 7.

Usage:
    python3 verdict.py                 # workspace is ~/quant
    python3 verdict.py --input PATH    # a different workspace folder
    python3 verdict.py --json          # machine-readable, no card
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import os
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

try:
    import numpy as np
except ImportError:
    _venv = Path.home() / ".ptq-academy" / "venv"
    _py = _venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    sys.exit(
        "This needs numpy and pandas, and the Python you just ran it with has "
        "neither.\n\n"
        "Day 4 already built an environment that has both. Run this with that "
        "one instead:\n"
        f"  {_py} {Path(__file__).resolve()}\n\n"
        "If that path does not exist, day 4's setup never finished. Build it:\n"
        "  python3 <day-4 skill folder>/scripts/setup_env.py\n"
        "  (on Windows:  py <day-4 skill folder>\\scripts\\setup_env.py)\n"
        "It prints the path to the right Python on its last line.\n\n"
        "Do not pip install these into your system Python. On a Homebrew Mac "
        "that fails with 'externally-managed-environment', which is what the "
        "day-4 environment exists to avoid."
    )

SCHEMA = "ptq-academy/verdict/1"
RESULT_FILE = "verdict.json"
CARD_FILE = "verdict-card.txt"

EULER_GAMMA = 0.5772156649015329

# Gate thresholds — identical to the engine's.
PBO_MAX = 0.50  # pass when PBO < 50%
DSR_MIN = 0.95  # pass when deflated Sharpe (a probability) > 0.95
OOS_SHARPE_MIN = 0.0  # pass when the stitched OOS Sharpe > 0

CSCV_SPLITS = 8  # S in the CSCV paper; C(8,4) = 70 splits
MIN_FOLD_ROWS = 20
DEFAULT_FOLDS = 5


# ---------------------------------------------------------------------------
# Normal distribution helpers (ported verbatim)
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam's inverse normal CDF approximation."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [
        -39.69683028665376,
        220.9460984245205,
        -275.9285104469687,
        138.3577518672690,
        -30.66479806614716,
        2.506628277459239,
    ]
    b = [
        -54.47609879822406,
        161.5858368580409,
        -155.6989798598866,
        66.80131188771972,
        -13.28068155288572,
    ]
    c = [
        -0.007784894002430293,
        -0.3223964580411365,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    ]
    d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


# ---------------------------------------------------------------------------
# Check 1 — PBO via CSCV
# ---------------------------------------------------------------------------


def _sharpe_cols(M: np.ndarray) -> np.ndarray:
    mean = M.mean(axis=0)
    sd = M.std(axis=0, ddof=1)
    return np.divide(mean, sd, out=np.zeros_like(mean), where=sd > 0)


def cscv_pbo(M: np.ndarray, S: int = CSCV_SPLITS) -> float:
    """Probability of backtest overfitting via combinatorially symmetric CV.

    The T x N matrix of per-bar returns (one column per configuration you
    tried) is cut into S equal blocks along time. Every way of choosing S/2
    blocks as in-sample is used, so with S=8 that is C(8,4) = 70 splits. On
    each split: pick the column with the best in-sample Sharpe, then look up
    where that same column ranks out-of-sample. Logit of its relative rank is
    lambda. PBO is the share of splits where lambda <= 0, i.e. the share of
    splits where the in-sample winner landed in the bottom half out-of-sample.

    Bailey, Borwein, Lopez de Prado & Zhu (2016). This is the full split set,
    not a sampled subset.
    """
    T, N = M.shape
    if N < 2:
        raise ValueError(
            f"This check compares versions of your rule and there is only {N}, "
            "so there is nothing to compare."
        )
    if T < 2 * S:
        raise ValueError(
            f"CSCV needs at least {2 * S} bars to cut into {S} blocks; got {T}."
        )
    rows = T // S
    Tt = rows * S
    M = M[:Tt]
    chunk_rows = [np.arange(k * rows, (k + 1) * rows) for k in range(S)]
    lambdas = []
    for comb in combinations(range(S), S // 2):
        is_mask = np.zeros(Tt, dtype=bool)
        for k in comb:
            is_mask[chunk_rows[k]] = True
        oos_mask = ~is_mask
        nstar = int(np.argmax(_sharpe_cols(M[is_mask])))
        oos = _sharpe_cols(M[oos_mask])
        order = oos.argsort()
        ranks = np.empty(N)
        ranks[order] = np.arange(1, N + 1)
        w = min(max(ranks[nstar] / (N + 1), 1e-6), 1 - 1e-6)
        lambdas.append(math.log(w / (1 - w)))
    return float((np.array(lambdas) <= 0).mean())


# ---------------------------------------------------------------------------
# Check 2 — probabilistic and deflated Sharpe
# ---------------------------------------------------------------------------


def probabilistic_sharpe(returns: np.ndarray, sr_benchmark: float) -> float:
    """P(true Sharpe > sr_benchmark), correcting for skew, fat tails and
    sample length. Bailey & Lopez de Prado (2012)."""
    n = len(returns)
    if n < 8:
        return 0.0
    sd = returns.std(ddof=1)
    if sd == 0:
        return 0.0
    sr = returns.mean() / sd
    m = returns - returns.mean()
    skew = float((m**3).mean() / sd**3)
    kurt = float((m**4).mean() / sd**4)  # non-excess
    denom = math.sqrt(max(1e-12, 1 - skew * sr + ((kurt - 1) / 4) * sr**2))
    return _norm_cdf((sr - sr_benchmark) * math.sqrt(n - 1) / denom)


def deflated_sharpe(returns: np.ndarray, trial_sharpes: np.ndarray) -> float:
    """Deflated Sharpe ratio. Bailey & Lopez de Prado (2014).

    The benchmark is not zero. It is the Sharpe you would expect the BEST of
    N independent random trials to show, given how much the trial Sharpes
    varied. Search hard enough and that expected maximum climbs, so the bar
    your strategy has to clear climbs with it.
    """
    n_trials = max(len(trial_sharpes), 1)
    var_sr = float(np.var(trial_sharpes, ddof=1)) if n_trials > 1 else 0.0
    if var_sr <= 0 or n_trials < 2:
        return probabilistic_sharpe(returns, 0.0)
    sr_star = math.sqrt(var_sr) * (
        (1 - EULER_GAMMA) * _norm_ppf(1 - 1 / n_trials)
        + EULER_GAMMA * _norm_ppf(1 - 1 / (n_trials * math.e))
    )
    return probabilistic_sharpe(returns, sr_star)


def expected_max_sharpe(trial_sharpes: np.ndarray) -> float | None:
    """SR* — the benchmark DSR deflates against. Reported so the member can
    see what bar the search itself raised."""
    n_trials = max(len(trial_sharpes), 1)
    if n_trials < 2:
        return None
    var_sr = float(np.var(trial_sharpes, ddof=1))
    if var_sr <= 0:
        return None
    return math.sqrt(var_sr) * (
        (1 - EULER_GAMMA) * _norm_ppf(1 - 1 / n_trials)
        + EULER_GAMMA * _norm_ppf(1 - 1 / (n_trials * math.e))
    )


# ---------------------------------------------------------------------------
# Check 3 — anchored walk-forward out-of-sample
# ---------------------------------------------------------------------------


def _ann_sharpe(returns: np.ndarray, ppy: float) -> float:
    if len(returns) < 3:
        return 0.0
    sd = returns.std(ddof=1)
    return float(returns.mean() / sd * math.sqrt(ppy)) if sd > 0 else 0.0


def _period_sharpe(returns: np.ndarray) -> float:
    if len(returns) < 2:
        return 0.0
    sd = returns.std(ddof=1)
    return float(returns.mean() / sd) if sd > 0 else 0.0


def _compound_return_pct(returns: np.ndarray) -> float:
    if len(returns) == 0:
        return 0.0
    return float((np.prod(1.0 + returns) - 1.0) * 100.0)


def walk_forward(
    M: np.ndarray,
    labels: list,
    ppy: float,
    scheme: str = "anchored",
    folds: int = DEFAULT_FOLDS,
) -> dict:
    """K-fold anchored (expanding) or rolling walk-forward.

    On each fold the best configuration is chosen on in-sample rows only and
    then scored on the immediately following out-of-sample rows. Selection can
    never see its own test window, so there is no look-ahead. The OOS segments
    are stitched into one return stream and that stream is what the gate reads.

    A fixed configuration split into two halves does NOT test this. It tests
    the rule. This tests whether the way you PICKED the rule generalises.
    """
    T, N = M.shape
    max_folds = T // MIN_FOLD_ROWS - 1
    k = min(folds, max_folds)
    if k < 1 or N < 1:
        return {
            "scheme": scheme,
            "folds": 0,
            "run": False,
            "oosSharpe": 0.0,
            "oosReturnPct": 0.0,
            "isSharpe": 0.0,
            "efficiency": 0.0,
            "perFold": [],
            "note": (
                f"insufficient history for walk-forward "
                f"({T} bars; need at least {2 * MIN_FOLD_ROWS})"
            ),
        }

    blocks = [b for b in np.array_split(np.arange(T), k + 1) if len(b)]
    per_fold: list[dict] = []
    oos_stream: list[np.ndarray] = []
    is_sharpes: list[float] = []
    oos_sharpes: list[float] = []

    for j in range(1, len(blocks)):
        oos_rows = blocks[j]
        is_rows = blocks[j - 1] if scheme == "rolling" else np.concatenate(blocks[:j])
        is_mat = M[is_rows]
        col_sharpes = [_period_sharpe(is_mat[:, c]) for c in range(N)]
        chosen = int(np.argmax(col_sharpes))
        is_ret = M[is_rows, chosen]
        oos_ret = M[oos_rows, chosen]
        oos_stream.append(oos_ret)

        is_sr = _ann_sharpe(is_ret, ppy)
        oos_sr = _ann_sharpe(oos_ret, ppy)
        is_sharpes.append(is_sr)
        oos_sharpes.append(oos_sr)
        per_fold.append(
            {
                "fold": j,
                "isPeriods": int(len(is_ret)),
                "oosPeriods": int(len(oos_rows)),
                "chosen": labels[chosen] if chosen < len(labels) else chosen,
                "isSharpe": round(is_sr, 2),
                "oosSharpe": round(oos_sr, 2),
                "oosReturnPct": round(_compound_return_pct(oos_ret), 2),
            }
        )

    stitched = np.concatenate(oos_stream) if oos_stream else np.array([])
    oos_sharpe = _ann_sharpe(stitched, ppy)
    is_avg = float(np.mean(is_sharpes)) if is_sharpes else 0.0
    oos_avg = float(np.mean(oos_sharpes)) if oos_sharpes else 0.0
    efficiency = (oos_avg / is_avg) if is_avg > 0 else 0.0

    return {
        "scheme": scheme,
        "folds": len(per_fold),
        "run": True,
        "oosSharpe": round(oos_sharpe, 2),
        "oosReturnPct": round(_compound_return_pct(stitched), 2),
        "oosPeriods": int(len(stitched)),
        "isSharpe": round(is_avg, 2),
        "efficiency": round(efficiency, 2),
        "perFold": per_fold,
        "note": (
            f"{scheme} walk-forward, {len(per_fold)} folds: best configuration "
            "chosen in-sample only, scored out-of-sample, segments stitched."
        ),
    }


# ---------------------------------------------------------------------------
# Loading day 4's run, and building the trial grid
# ---------------------------------------------------------------------------


def verify_identity(meta, spec, day4, df=None):
    identity = meta.get("inputIdentity") or {}
    if (
        identity.get("version") != 1
        or not identity.get("specSha256")
        or not identity.get("barsSha256")
    ):
        raise InputError(
            "Day 4 has no verified input identity. Re-run Day 4 with this download, then retry Day 5."
        )
    if not callable(getattr(day4, "spec_fingerprint", None)) or not callable(
        getattr(day4, "bars_fingerprint", None)
    ):
        raise InputError(
            "The Day 4 tools are from an older download. Restore the complete Academy pack and re-run Day 4."
        )
    if day4.spec_fingerprint(spec) != identity["specSha256"]:
        raise InputError(
            "Your strategy has changed since Day 4. Re-run Day 4 before checking this version."
        )
    if df is not None and day4.bars_fingerprint(df) != identity["barsSha256"]:
        raise InputError(
            "The price history has changed since Day 4. Re-run Day 4, then retry Day 5 so both use the same prices."
        )


class InputError(Exception):
    """Something about the day-4 handoff is missing or wrong. Say so, stop."""


RESULT_CANDIDATES = ("backtest-result.json", "backtest.json")


def quant_home(input_arg: str | None) -> Path:
    """The member's workspace. Day 1 creates ~/quant and every day works in it."""
    if input_arg:
        return Path(input_arg).expanduser().resolve()
    env = os.environ.get("PTQ_QUANT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / "quant"


def _day4_candidates(explicit: Path | None):
    """Places day 4's run_backtest.py could be, most certain first.

    A generator, so the caller stops at the first usable file. The folder
    searches at the end walk whole trees, which is slow, so each one only runs
    when nothing before it was usable."""
    if explicit:
        yield explicit
    here = Path(__file__).resolve()
    # .../day-5/skill/scripts/verdict.py -> .../day-5/skill -> day-5 -> academy
    academy = here.parent.parent.parent
    yield academy / "day-4" / "skill" / "scripts" / "run_backtest.py"
    yield academy.parent / "day-4" / "skill" / "scripts" / "run_backtest.py"

    home = Path.home()
    roots = [home / ".claude", home / ".ptq-academy", home / "ptq-academy"]
    # The current folder is searched only when it is a project folder. The home
    # folder, or anything above it, holds every file the member owns: the walk
    # does not finish, and another project's run_backtest.py could match.
    cwd = Path.cwd()
    if cwd.resolve() not in (home.resolve(), *home.resolve().parents):
        roots.append(cwd)
    # An installed Claude skill lands at ~/.claude/skills/<name>/scripts/, with
    # no "day-4/skill" in the path, so match the bare filename there too.
    for root in roots:
        if not root.is_dir():
            continue
        yield from sorted(root.glob("**/day-4/skill/scripts/run_backtest.py"))[:3]
        yield from sorted(root.glob("**/run_backtest.py"))[:5]


def _load_day4_module(explicit: str | None):
    """Import day 4's run_backtest.py so the trial grid is simulated by the SAME
    code that produced yesterday's number. One simulator, so the two days can
    never disagree, and day 5 never re-derives the trade logic."""
    ex = None
    if explicit:
        ex = Path(explicit).expanduser()
        if not ex.is_file():
            raise InputError(f"No such file: {ex} (from --day4)")

    for c in _day4_candidates(ex):
        if not c.is_file():
            continue
        spec = importlib.util.spec_from_file_location("ptq_day4_backtest", c)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:
            raise InputError(f"Day 4's backtester would not load ({c}): {exc}")
        missing = [
            fn
            for fn in ("simulate", "fetch_bars", "spec_warmup", "periods_per_year")
            if not hasattr(mod, fn)
        ]
        if missing:
            raise InputError(
                f"Day 4's backtester at {c} is missing {', '.join(missing)}.\n"
                "Day 5 will not simulate with its own copy of the trade logic. "
                "Update day 4, or point me at the right file with --day4."
            )
        return mod, c

    raise InputError(
        "Cannot find day 4's backtester (run_backtest.py).\n\n"
        "Day 5 runs the same simulator day 4 used, across a grid of settings, so "
        "the two days can never disagree. Without it I will not simulate "
        "anything with different code and call it yours.\n\n"
        "Point me at it:\n"
        "  python3 verdict.py --day4 /path/to/day-4/skill/scripts/run_backtest.py"
    )


# --- the trial grid --------------------------------------------------------
# A day-3 spec is an arbitrary condition tree, so there is no fixed parameter
# grid to sweep. The engine's answer for exactly this case (composed.py) is
# length-scaled variants of the member's OWN tree: the same rule, with every
# lookback stretched and squeezed. That is the search a person actually runs
# when they "try a few settings", and it is never an invented strategy.

LENGTH_SCALES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
LENGTH_MIN = 2
LENGTH_MAX = 400
# The engine clamps a scaled length to the same bounds its own validator
# enforces. Here that validator is day 3's: every series needs a lookback of 2
# or more, except pct_change, which takes 1 ("yesterday's close").
LENGTH_MIN_BY_SERIES = {"pct_change": 1}


def _scale_operand(operand: dict, scale: float) -> None:
    params = operand.get("params") or {}
    if isinstance(params.get("period"), (int, float)) and not isinstance(
        params.get("period"), bool
    ):
        floor = LENGTH_MIN_BY_SERIES.get(operand.get("series"), LENGTH_MIN)
        scaled = int(round(params["period"] * scale))
        params["period"] = max(floor, min(LENGTH_MAX, scaled))


def _scale_block(block: dict | None, scale: float) -> None:
    if not block:
        return
    for cond in block.get("conditions", []) or []:
        for side in ("left", "right"):
            if isinstance(cond.get(side), dict):
                _scale_operand(cond[side], scale)


def _scale_stop(stop: dict | None, scale: float) -> None:
    # A swing stop's bar count is a lookback like any other.
    if stop and stop.get("type") == "swing" and isinstance(stop.get("value"), int):
        stop["value"] = max(LENGTH_MIN, min(LENGTH_MAX, int(round(stop["value"] * scale))))


def _variant_key(spec: dict) -> str:
    return json.dumps(
        {"entry": spec.get("entry"), "exit": spec.get("exit"), "stop": spec.get("stop")},
        sort_keys=True,
    )


def spec_variants(spec: dict) -> tuple[list[str], list[dict], int]:
    """(labels, specs, base_index) — deduped length-scaled variants of the
    member's own spec. Ported from the engine's composed_variants.

    The member's own spec is never scaled or clamped. It goes in exactly as
    written, labelled "your settings", and a scaled variant that lands on the
    same rule is dropped in its favour. The engine can clamp every scale
    because it refuses an out-of-range length before it gets here; day 3 allows
    lookbacks the clamp would change, so the base has to be left alone."""
    labels: list[str] = []
    variants: list[dict] = []
    seen: dict[str, int] = {}
    base_key = _variant_key(spec)
    for s in LENGTH_SCALES:
        v = copy.deepcopy(spec)
        if s != 1.0:
            _scale_block(v.get("entry"), s)
            _scale_block(v.get("exit"), s)
            _scale_stop(v.get("stop"), s)
        key = _variant_key(v)
        if key in seen:
            continue
        seen[key] = len(variants)
        labels.append("your settings" if key == base_key else f"lookbacks x{s:g}")
        variants.append(v)
    return labels, variants, seen[base_key]


def build_matrix(meta: dict, spec: dict, day4, ppy: float) -> dict:
    """Re-run day 4's simulator across the length-scaled variants of the
    member's own spec, and stack the per-bar return streams into the T x N
    matrix the checks need."""
    data = meta["data"]
    symbol, bar = data["symbol"], data["bar"]

    try:
        df, source, _clipped = day4.fetch_bars(spec, symbol, bar)
    except Exception as exc:
        raise InputError(
            f"Could not load the price data day 4 used ({symbol}, {bar}):\n"
            f"  {exc}\n"
            "Day 5 will not judge a run on data it could not load."
        )

    verify_identity(meta, spec, day4, df)
    notes: list[str] = []

    labels, variants, base_idx = spec_variants(spec)
    if len(variants) < 2:
        notes.append(
            "Your rule has no lookback in it (nothing like a 20-day average), "
            "so there is nothing to stretch or squeeze and only one version of "
            "it to test"
        )
    cols: list[np.ndarray] = []
    kept_labels: list[str] = []
    dead: list[str] = []

    for label, variant in zip(labels, variants):
        try:
            warmup = day4.spec_warmup(variant)
            sim = day4.simulate(df, variant, warmup)
        except Exception as exc:
            if label == "your settings":
                raise InputError(
                    f"The simulator failed on your own settings: {exc}\n"
                    "Re-run day 4 first."
                )
            dead.append(label)
            continue
        if not sim["trades"] and not sim.get("openAtEnd"):
            # A variant that never triggers is a flat line, not a trial. Dropping
            # it is honest; keeping it would hand PBO a fake column to rank.
            if label == "your settings":
                raise InputError(
                    "Your rule never triggered on this data. There is nothing "
                    "to validate. Re-run day 4 and read what it tells you."
                )
            dead.append(label)
            continue
        r = sim["equity"].pct_change().fillna(0.0).to_numpy()
        cols.append(r)
        kept_labels.append(label)

    base_idx = kept_labels.index("your settings")
    if dead:
        notes.append(
            f"{len(dead)} of {len(labels)} settings never triggered a trade and "
            "were dropped rather than counted as flat trials (" + ", ".join(dead) + ")"
        )
    if len(kept_labels) == 2:
        notes.append(
            "Only 2 versions of your rule could be compared. That is the fewest "
            "the overfitting check can run on, so treat that check as a rough one"
        )
    M = np.column_stack(cols)
    if not np.isfinite(M).all():
        raise InputError(
            "The simulated returns contain non-finite values. Something in the "
            "price data is broken. Re-run day 4 first."
        )
    return {
        "M": M,
        "labels": kept_labels,
        "baseIndex": base_idx,
        "bars": len(df),
        "ppy": ppy,
        "source": source,
        "notes": notes,
        "gridSource": (
            f"{len(kept_labels)} length-scaled variants of your own spec, "
            "simulated with day 4's backtester"
        ),
    }


def load_run(input_arg: str | None, day4_arg: str | None) -> dict:
    home = quant_home(input_arg)
    if (home / ".day4-incomplete").exists():
        raise InputError(
            "The latest Day 4 attempt did not finish. Complete it before Day 5."
        )
    if not home.is_dir():
        raise InputError(
            f"There is no workspace at {home}.\n"
            "That folder is created on day 1 and every day works inside it. "
            "If yours is somewhere else:\n"
            "  python3 verdict.py --input /path/to/your/quant/folder"
        )

    result_path = next(
        (home / n for n in RESULT_CANDIDATES if (home / n).is_file()), None
    )
    if result_path is None:
        raise InputError(
            f"No backtest-result.json in {home}.\n\n"
            "Day 5 judges day 4's run. It does not run a new backtest of its "
            "own. Go back and finish day 4, then come here."
        )
    try:
        meta = json.loads(result_path.read_text())
    except json.JSONDecodeError as exc:
        raise InputError(f"{result_path} is not valid JSON: {exc}")

    for key in ("strategy", "data", "metrics"):
        if key not in meta:
            raise InputError(
                f"{result_path.name} has no {key!r} block. It is not a day-4 "
                "result, or day 4 did not finish. Re-run day 4."
            )
    for key in ("symbol", "bar"):
        if not meta["data"].get(key):
            raise InputError(f"{result_path.name} is missing data.{key}. Re-run day 4.")

    # Costs are a precondition, not a gate. A frictionless curve is not a curve
    # anybody could have traded, so there is nothing honest to judge.
    cost_bps = meta["data"].get("costBps")
    if not cost_bps:
        raise InputError(
            f"Day 4's run has no transaction costs in it (costBps is "
            f"{cost_bps!r}).\nA frictionless curve is not judgeable. Re-run day "
            "4 with costs on. The academy default is 6 bps per side."
        )

    # The spec is the thing the grid is built from. The workspace's own copy
    # wins, so a moved or copied folder is judged on the spec sitting in it and
    # never on a stale file somewhere else that day 4 happened to record.
    spec_path = home / "strategy.json"
    if not spec_path.is_file() and meta.get("specPath"):
        spec_path = Path(meta["specPath"])
    if not spec_path.is_file():
        raise InputError(
            f"Cannot find your strategy spec (looked for {spec_path}).\n"
            "Day 5 builds the settings grid from your own spec. Without it "
            "there is nothing to vary. Re-run day 3."
        )
    try:
        spec = json.loads(spec_path.read_text())
    except json.JSONDecodeError as exc:
        raise InputError(f"{spec_path} is not valid JSON: {exc}")
    if not spec.get("entry", {}).get("conditions"):
        raise InputError(f"{spec_path.name} has no entry conditions. Re-run day 3.")

    day4, day4_path = _load_day4_module(day4_arg)
    verify_identity(meta, spec, day4)

    ppy = meta["data"].get("ppy")
    if not ppy:
        ppy, _note = day4.periods_per_year(
            meta["data"]["bar"],
            meta["data"].get("session", "regular"),
            meta["data"].get("assetClass", "equity"),
        )

    built = build_matrix(meta, spec, day4, float(ppy))

    return {
        "home": home,
        "meta": meta,
        "specPath": str(spec_path),
        "costBps": float(cost_bps),
        "trades": (meta.get("metrics") or {}).get("trades"),
        "day4Path": str(day4_path),
        **built,
    }


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def judge(run: dict) -> dict:
    M = run["M"]
    labels = run["labels"]
    meta = run["meta"]
    base_idx = run["baseIndex"]
    T, N = M.shape

    base_ret = M[:, base_idx]
    trial_sharpes = _sharpe_cols(M)

    # --- check 1: PBO -----------------------------------------------------
    pbo_ran, pbo, pbo_note = True, None, ""
    try:
        pbo = cscv_pbo(M)
    except ValueError as exc:
        pbo_ran = False
        pbo_note = str(exc)

    # --- check 2: DSR -----------------------------------------------------
    deflated = N >= 2 and float(np.var(trial_sharpes, ddof=1)) > 0
    dsr = deflated_sharpe(base_ret, trial_sharpes)
    sr_star = expected_max_sharpe(trial_sharpes)
    dsr_ran = len(base_ret) >= 8

    # --- check 3: walk-forward OOS ---------------------------------------
    wf = walk_forward(M, labels, run["ppy"])

    gates = [
        {
            "key": "pbo",
            "label": "Overfitting (PBO, CSCV)",
            "value": f"{pbo * 100:.0f}%" if pbo_ran else "could not run",
            "detail": (
                f"needs to be under {PBO_MAX * 100:.0f}%" if pbo_ran else pbo_note
            ),
            "pass": bool(pbo_ran and pbo < PBO_MAX),
            "ran": pbo_ran,
        },
        {
            "key": "dsr",
            "label": (
                "Deflated Sharpe" if deflated else "Probabilistic Sharpe (not deflated)"
            ),
            "value": f"{dsr:.2f}" if dsr_ran else "could not run",
            "detail": (
                (
                    f"needs to be over {DSR_MIN:.2f}"
                    + (
                        f"; the search raised the bar to SR* = {sr_star:.3f} per period"
                        if sr_star is not None
                        else ""
                    )
                )
                if dsr_ran
                else "fewer than 8 bars of returns"
            ),
            "pass": bool(dsr_ran and dsr > DSR_MIN),
            "ran": dsr_ran,
            "deflated": deflated,
        },
        {
            "key": "wf",
            "label": "Out-of-sample (walk-forward)",
            "value": (
                f"{wf['oosSharpe']:.2f} OOS Sharpe over {wf['folds']} "
                f"fold{'s' if wf['folds'] != 1 else ''}"
                if wf["run"]
                else "could not run"
            ),
            "detail": (
                f"needs to be over {OOS_SHARPE_MIN:.2f}; walk-forward "
                f"efficiency {wf['efficiency']:.2f} (OOS Sharpe over in-sample)"
                if wf["run"]
                else wf["note"]
            ),
            "pass": bool(wf["run"] and wf["oosSharpe"] > OOS_SHARPE_MIN),
            "ran": wf["run"],
        },
    ]

    fails = sum(1 for g in gates if not g["pass"])
    # Same three-state rule the engine uses.
    engine_verdict = "block" if fails >= 2 else "reduce" if fails == 1 else "approve"
    headline = "PASS" if fails == 0 else "FAIL"

    return {
        "schema": SCHEMA,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "day": 5,
        "strategyName": meta["strategy"].get("name")
        or meta.get("specName")
        or "unnamed",
        "asset": meta["data"]["symbol"],
        "bar": meta["data"]["bar"],
        "assetClass": meta["data"].get("assetClass"),
        "periodsPerYear": run["ppy"],
        "direction": meta["strategy"].get("direction"),
        "entryPlain": meta["strategy"].get("entryPlain"),
        "bars": int(T),
        "trades": run.get("trades"),
        "configurationsTried": int(N),
        "yourConfiguration": labels[base_idx],
        "trialLabels": labels,
        "costBps": run["costBps"],
        "headline": headline,
        "engineVerdict": engine_verdict,
        "failedGates": [g["key"] for g in gates if not g["pass"]],
        "gates": gates,
        "pboPct": round(pbo * 100, 1) if pbo_ran else None,
        "deflatedSharpe": round(dsr, 2) if dsr_ran else None,
        "expectedMaxSharpe": round(sr_star, 4) if sr_star is not None else None,
        "walkForward": wf,
        "methodology": {
            "cscvSplits": CSCV_SPLITS,
            "cscvCombinations": math.comb(CSCV_SPLITS, CSCV_SPLITS // 2),
            "purging": False,
            "purgingNote": (
                "Splits are NOT purged by trade label in this manual version. "
                "Trades that straddle an in-sample / out-of-sample boundary "
                "stay on the training side, which can flatter in-sample "
                "scores slightly."
            ),
            "costBps": run["costBps"],
            "gridSource": run.get("gridSource"),
            "day4Simulator": run.get("day4Path"),
            "dataSource": run.get("source"),
            "specPath": run.get("specPath"),
            "dataNotes": run.get("notes", []),
            "thresholds": {
                "pboMax": PBO_MAX,
                "dsrMin": DSR_MIN,
                "oosSharpeMin": OOS_SHARPE_MIN,
            },
            "papers": [
                "Bailey, Borwein, Lopez de Prado & Zhu (2016) — PBO via CSCV",
                "Bailey & Lopez de Prado (2014) — the Deflated Sharpe Ratio",
                "Bailey & Lopez de Prado (2012) — the Probabilistic Sharpe Ratio",
            ],
        },
    }


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

W = 66


def _line(s: str = "") -> str:
    return "  " + s


def _rule(ch: str = "-") -> str:
    return "  " + ch * W


def render_card(v: dict) -> str:
    out: list[str] = []
    a = out.append

    a("")
    a(_rule("="))
    a(_line(f"  DAY 5 VERDICT   {v['strategyName']}"))
    a(
        _line(
            f"  {v['asset']} · {v['bar']} · {v['bars']} bars · "
            f"{v['costBps']} bps per side"
        )
    )
    a(_rule("="))
    a("")

    if v["headline"] == "PASS":
        a(_line("   >>>  PASS  —  all three checks held  <<<"))
    else:
        broke = [g for g in v["gates"] if not g["pass"] and g["ran"]]
        skipped = [g for g in v["gates"] if not g["ran"]]
        bits = []
        if broke:
            bits.append(f"{len(broke)} of 3 checks did not hold")
        if skipped:
            bits.append(f"{len(skipped)} could not run")
        a(_line("   >>>  FAIL  —  " + ", ".join(bits) + "  <<<"))
        if skipped and not broke:
            a("")
            a(_line("   A check that could not run is not a check that passed."))
    a("")
    a(_rule())
    a("")

    for g in v["gates"]:
        mark = "PASS" if g["pass"] else ("FAIL" if g["ran"] else "N/A ")
        a(_line(f"[{mark}]  {g['label']}"))
        a(_line(f"         {g['value']}"))
        a(_line(f"         {g['detail']}"))
        a("")

    a(_rule())
    a("")
    a(
        _line(
            f"Your rule was run at {v['configurationsTried']} lookback "
            f"setting{'s' if v['configurationsTried'] != 1 else ''}. "
            f"The one judged above is: {v['yourConfiguration']}."
        )
    )
    wf = v["walkForward"]
    if wf["run"]:
        a(
            _line(
                f"Walk-forward: {wf['folds']} "
                f"fold{'s' if wf['folds'] != 1 else ''}, {wf['oosPeriods']} "
                f"bars out-of-sample, {wf['scheme']}."
            )
        )
    for note in v["methodology"].get("dataNotes", []):
        a(_line("Note: " + note))
    a("")

    if v["headline"] == "PASS":
        a(_line("This is not permission to trade real money."))
        a(_line("It is one clean result, on one asset, on paper."))
        a(_line("Day 6 puts it on a schedule. Still paper."))
    elif not any(g["ran"] for g in v["gates"]):
        a(_line("Nothing could be computed here. This is not a result about"))
        a(_line("your rule, it is a result about your data: there is not"))
        a(_line("enough of it to test anything. Go back to day 4 with a"))
        a(_line("longer history, then come back."))
    else:
        a(_line("This run did not pass every check."))
        a(
            _line(
                "Read the failing or unavailable row before deciding what to test next."
            )
        )
        a(_line("Post the card with its warnings intact."))
    a("")
    a(_rule("="))
    a("")
    return "\n".join(out)


def render_diagnosis(v: dict) -> str:
    """What each failure actually means, in plain words."""
    fails = {g["key"]: g for g in v["gates"] if not g["pass"]}
    if fails and not any(g["ran"] for g in v["gates"]):
        return (
            "None of the three checks could run. Nothing above is a judgement "
            "of your rule — there is simply not enough data to test it. The "
            "reasons are printed next to each check. Fix the shortest one "
            "first, usually the history length, and re-run day 4."
        )
    if not fails:
        return (
            "All three held. The most common reason that happens on a rule "
            "with no real edge is a short history or very few trades, so check "
            f"the bar count ({v['bars']}) and the trade count from day 4 "
            "before you believe it."
        )
    lines = ["What broke, and what it means:", ""]
    if "pbo" in fails:
        g = fails["pbo"]
        if not g["ran"] and v["configurationsTried"] < 2:
            lines.append(
                "- The overfitting check (PBO) could not run. It works by "
                "comparing versions of your rule with the lookbacks stretched "
                "and squeezed, and only one version was left to test. That is "
                "not a pass and it is not a fail. The note on the card says "
                "why only one was left."
            )
        elif not g["ran"]:
            lines.append(
                "- The overfitting check (PBO) could not run. It needs at "
                f"least {2 * CSCV_SPLITS} bars and this run has {v['bars']}. "
                "Re-run day 4 with a longer history."
            )
        else:
            lines.append(
                f"- PBO came out at {v['pboPct']:.0f}%. Across the CSCV splits, "
                "the setting that won in-sample landed in the bottom half "
                "out-of-sample that often. The number you liked yesterday was "
                "mostly a function of which settings you happened to try."
            )
    if "dsr" in fails:
        g = fails["dsr"]
        if not g["ran"]:
            lines.append("- The Sharpe test could not run: not enough return history.")
        else:
            if g.get("deflated"):
                lines.append(
                    f"- Deflated Sharpe came out at {v['deflatedSharpe']:.2f}, "
                    f"under {DSR_MIN}. Stretching your rule's lookbacks over "
                    f"{v['configurationsTried']} settings raised the bar "
                    f"to SR* = {v['expectedMaxSharpe']} per period, and your "
                    "result does not clear it. Priced against the size of your "
                    "own search, it is not separable from luck."
                )
            else:
                lines.append(
                    f"- Probabilistic Sharpe came out at "
                    f"{v['deflatedSharpe']:.2f}, under {DSR_MIN}. This is the "
                    "chance your true Sharpe is above zero at all, corrected "
                    "for skew, fat tails and how few bars you have. It is not "
                    "deflated, because there was no spread of trials to "
                    "deflate against."
                )
    if "wf" in fails:
        wf = v["walkForward"]
        if not wf["run"]:
            lines.append(f"- Walk-forward could not run: {wf['note']}")
        else:
            lines.append(
                f"- Out-of-sample Sharpe was {wf['oosSharpe']:.2f}. When the "
                "settings were re-chosen on past data only and then scored on "
                "data that selection never saw, the edge did not carry. "
                f"Walk-forward efficiency was {wf['efficiency']:.2f} — that is "
                "the out-of-sample Sharpe divided by the in-sample one, so "
                "anything at or below zero means none of it survived."
            )
    lines.append("")
    lines.append(
        "None of this says you are bad at this. It says this rule, on this "
        "asset, over this history, does not hold up. Those are four separate "
        "things you can change."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Day 5 — edge or luck.")
    ap.add_argument("--input", help="Workspace folder (default ~/quant)")
    ap.add_argument("--day4", help="Path to day 4's run_backtest.py, if not found")
    ap.add_argument("--json", action="store_true", help="Print JSON, no card")
    ap.add_argument("--no-save", action="store_true", help="Do not write result files")
    args = ap.parse_args()

    marker = quant_home(args.input) / ".day5-incomplete"
    if not args.no_save and marker.parent.is_dir():
        marker.write_text("The latest validation has not completed successfully.\n")
    try:
        run = load_run(args.input, args.day4)
        verdict = judge(run)
    except InputError as exc:
        print("\nStopped. Nothing was made up.\n")
        print(str(exc))
        print("")
        return 2

    card = render_card(verdict)
    diagnosis = render_diagnosis(verdict)

    if not args.no_save:
        home = run["home"]
        (home / RESULT_FILE).write_text(json.dumps(verdict, indent=2))
        (home / CARD_FILE).write_text(card + "\n" + diagnosis + "\n")
        marker.unlink(missing_ok=True)

    if args.json:
        print(json.dumps(verdict, indent=2))
        return 0

    print(card)
    print(diagnosis)
    print("")
    if not args.no_save:
        print(f"  Saved: {run['home'] / CARD_FILE}")
        print(f"  Saved: {run['home'] / RESULT_FILE}   (day 7 reads this)")
        print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
