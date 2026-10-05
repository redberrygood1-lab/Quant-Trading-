#!/usr/bin/env python3
"""Part-Time Quant Academy - week one card.

Reads what days 3 to 6 actually wrote and prints a card the member can
screenshot, plus the partner intake block that gets them matched.

Standard library only. Same behaviour on macOS, Linux and Windows.

Design rule: every value on the card comes out of a file on disk. Anything
missing prints as "not found" and is listed in the notes. Anything a day
explicitly reported as unrunnable prints as "could not run", which is a
different thing and must not be shown as a pass. Nothing here invents a number.

Bound to the shipped day contracts:
  day 3  ~/quant/strategy.json          strategy spec v1  (day-3/skill/schema.json)
  day 4  ~/quant/backtest-result.json   day-4/skill/reference/SPEC-CONTRACT.md
  day 5  ~/quant/verdict.json           schema ptq-academy/verdict/1
  day 6  ~/quant/run-state.json + run-log.jsonl   day-6/skill/reference/OUTPUTS.md
Keyword discovery stays as a fallback for members who moved things.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

MISSING = "not found"
UNRUN = "could not run"

# ---------------------------------------------------------------------------
# where the week lives
# ---------------------------------------------------------------------------

# Day 1 creates ~/quant and starts the member there. Day 3 writes into it.
PRIMARY_ROOT = "quant"
FALLBACK_ROOTS = ["quant-week", "ptq-week", "ptq-academy/work", "part-time-quant"]

# Day 6 writes into the workspace, in the open, alongside days 3 to 5:
#   ~/quant/run-state.json   and   ~/quant/run-log.jsonl
# See day-6/skill/reference/OUTPUTS.md. These are the bound names; the older
# hidden-home layout is kept below only as a fallback for early members.
DAY6_STATE_NAMES = ["run-state.json", "state.json"]
DAY6_JOURNAL_NAMES = ["run-log.jsonl", "journal.jsonl"]
DAY6_HOMES = [
    os.environ.get("PTQ_ACADEMY_HOME"),
    str(Path.home() / ".ptq-academy" / "day-6"),
]

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "site-packages",
    ".cache",
    "dist",
    "build",
    ".next",
    "bars-cache",
}
DATA_EXT = {".json", ".jsonl", ".md", ".txt", ".csv"}

# Exact filenames each day ships, in order of preference. Relative to the
# workspace; nested folders are searched too.
BOUND_NAMES = {
    3: ["strategy.json", "strategy-spec.json", "spec.json"],
    4: ["backtest-result.json"],
    5: ["verdict.json"],
    6: ["run-state.json", "state.json"],
}

# Fallback only. Used when the bound filename is not there.
KEYWORD_PATTERNS = {
    3: [r"\bstrategy\b", r"\bspec\b", r"day[-_ ]?0?3"],
    4: [r"backtest", r"day[-_ ]?0?4", r"\bresults?\b"],
    5: [r"verdict", r"validation", r"day[-_ ]?0?5"],
    6: [r"run[-_ ]?state", r"\bstate\b", r"run[-_ ]?log", r"journal", r"paper[-_ ]?log", r"day[-_ ]?0?6"],
}


def find_workspace(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None

    for var in ("PTQ_QUANT_HOME", "PTQ_QUANT_DIR", "PTQ_ACADEMY_WORK", "PTQ_WEEK_DIR"):
        val = os.environ.get(var)
        if val:
            p = Path(val).expanduser()
            if p.is_dir():
                return p

    home = Path.home()
    here = Path.cwd()

    primary = home / PRIMARY_ROOT
    if primary.is_dir():
        return primary

    for base in (here, home, home / "Documents", home / "Desktop"):
        for name in [PRIMARY_ROOT] + FALLBACK_ROOTS:
            p = base / name
            if p.is_dir():
                return p

    if _looks_like_week(here):
        return here
    return None


def _looks_like_week(root: Path) -> bool:
    try:
        names = {f.name.lower() for f in root.iterdir() if f.is_file()}
    except OSError:
        return False
    bound = {n for names_ in BOUND_NAMES.values() for n in names_}
    return len(names & bound) >= 2


def walk_files(root: Path, max_depth: int = 4) -> list[Path]:
    out: list[Path] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if len(d.resolve().parts) - len(root.parts) >= max_depth:
            dirnames[:] = []
        dirnames[:] = [
            x for x in dirnames if x not in SKIP_DIRS and not x.startswith(".")
        ]
        for fn in filenames:
            if Path(fn).suffix.lower() in DATA_EXT:
                out.append(d / fn)
    return out


def resolve_days(
    workspace: Path,
) -> tuple[dict[int, Path | None], Path | None, dict[int, str]]:
    """Return (day file per day, day-6 journal path, how each day was found)."""
    files = walk_files(workspace)
    by_name: dict[str, list[Path]] = {}
    for f in files:
        by_name.setdefault(f.name.lower(), []).append(f)

    chosen: dict[int, Path | None] = {}
    how: dict[int, str] = {}

    for day, names in BOUND_NAMES.items():
        hit = None
        for want in names:
            candidates = by_name.get(want.lower())
            if candidates:
                hit = sorted(candidates, key=lambda p: -p.stat().st_mtime)[0]
                break
        if hit is not None:
            chosen[day] = hit
            how[day] = "bound"
            continue
        # fallback: keyword match on filename
        matches = []
        for f in files:
            low = f.name.lower()
            for rank, pat in enumerate(KEYWORD_PATTERNS[day]):
                if re.search(pat, low):
                    matches.append((rank, -f.stat().st_mtime, f))
                    break
        matches.sort()
        chosen[day] = matches[0][2] if matches else None
        how[day] = "keyword" if matches else "missing"

    # Day 6 needs two files: the state and the event journal. Normally both sit
    # in the workspace next to days 3-5. The hidden home is a fallback only.
    journal = None
    if how.get(6) == "bound" and chosen.get(6) is not None:
        for name in DAY6_JOURNAL_NAMES:
            sibling = chosen[6].parent / name
            if sibling.is_file():
                journal = sibling
                break

    if chosen.get(6) is None or how.get(6) != "bound":
        for home in DAY6_HOMES:
            if not home:
                continue
            h = Path(home).expanduser()
            state = next(
                (h / n for n in DAY6_STATE_NAMES if (h / n).is_file()), None
            )
            if state is not None:
                chosen[6] = state
                how[6] = "bound"
                journal = next(
                    (h / n for n in DAY6_JOURNAL_NAMES if (h / n).is_file()), None
                )
                break

    if journal is None:
        cands: list[Path] = []
        for name in DAY6_JOURNAL_NAMES:
            cands.extend(by_name.get(name) or [])
        if cands:
            journal = sorted(cands, key=lambda p: -p.stat().st_mtime)[0]
        elif chosen.get(6) is not None:
            for name in DAY6_JOURNAL_NAMES:
                sibling = chosen[6].parent / name
                if sibling.is_file():
                    journal = sibling
                    break

    return chosen, journal, how


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_json(path: Path | None):
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        return {"__text__": raw}


def text_of(payload) -> str | None:
    if isinstance(payload, dict) and "__text__" in payload:
        return payload["__text__"]
    return None


def path_get(payload, dotted: str):
    """Read a dotted path out of a dict. Missing key -> None."""
    node = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def has_path(payload, dotted: str) -> bool:
    node = payload
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return isinstance(node, dict) and parts[-1] in node


def dig(payload, aliases: list[str]):
    """Fallback: case-insensitive recursive search for the first alias hit."""
    if payload is None:
        return None
    wanted = {a.lower() for a in aliases}

    def _walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in wanted and v not in (None, "", [], {}):
                    return v
            for v in node.values():
                r = _walk(v)
                if r is not None:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = _walk(v)
                if r is not None:
                    return r
        return None

    return _walk(payload)


def from_text(text: str | None, labels: list[str]) -> str | None:
    if not text:
        return None
    for lab in labels:
        m = re.search(
            r"^[#*\s>-]*\**\s*%s\s*\**\s*[:\-]\s*(.+)$" % re.escape(lab),
            text,
            re.I | re.M,
        )
        if m:
            val = m.group(1).strip().strip("*`").strip()
            if val:
                return val
    return None


def num(v, places: int = 2) -> str | None:
    if v is None:
        return None
    try:
        return ("%%.%df" % places) % float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# the week
# ---------------------------------------------------------------------------


class Week:
    def __init__(self) -> None:
        self.strategy = None
        self.asset = None
        self.rule = None
        self.source_rule = None
        self.vague_resolved = None
        self.period = None
        self.trades = None
        self.costs = None
        self.configurations = None
        self.verdict = None
        self.gates: list[dict] = []
        self.pbo = None
        self.dsr = None
        self.oos = None
        self.runs = None
        self.fills = None
        self.stopped = None
        self.stopped_at = None
        self.missing_days: list[int] = []
        self.unbound_days: list[int] = []
        self.sources: dict[int, str] = {}
        self.journal_source = None


def read_day3(w: Week, payload) -> None:
    if payload is None:
        return
    txt = text_of(payload)
    if txt is not None:
        w.strategy = from_text(txt, ["Name", "Strategy", "Called"]) or _first_heading(
            txt
        )
        w.asset = from_text(txt, ["Asset", "Symbol", "Instrument", "Ticker"])
        w.rule = from_text(txt, ["Rule", "Entry", "In one line", "Summary"])
        return

    # strategy spec v1
    w.strategy = path_get(payload, "name") or dig(payload, ["strategyName", "title"])
    symbol = path_get(payload, "instrument.symbol") or dig(
        payload, ["symbol", "ticker", "instrument"]
    )
    bar = path_get(payload, "timeframe.bar") or dig(
        payload, ["bar", "interval", "signalTimeframe"]
    )
    if isinstance(symbol, str) and isinstance(bar, str):
        w.asset = "%s %s" % (symbol, bar)
    elif isinstance(symbol, str):
        w.asset = symbol
    w.rule = path_get(payload, "entry.plain_english") or dig(
        payload, ["plain_english", "oneLiner", "summary", "description"]
    )
    w.source_rule = path_get(payload, "source_rule")
    vague = path_get(payload, "vague_terms_resolved")
    if isinstance(vague, list):
        w.vague_resolved = len(vague)
    per_side = path_get(payload, "costs.per_side_bps")
    if per_side is not None:
        w.costs = "%g bps per side" % float(per_side)


def _first_heading(text: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def read_day4(w: Week, payload) -> None:
    if payload is None:
        return
    txt = text_of(payload)
    if txt is not None:
        w.period = w.period or from_text(txt, ["Period", "Date range", "Tested"])
        w.trades = w.trades or from_text(txt, ["Trades", "Number of trades"])
        return

    start = path_get(payload, "data.start") or dig(
        payload, ["start", "startDate", "from"]
    )
    end = path_get(payload, "data.end") or dig(payload, ["end", "endDate", "to"])
    if start and end:
        w.period = "%s to %s" % (str(start)[:11].strip(), str(end)[:11].strip())
    bars = path_get(payload, "data.bars")
    if bars:
        w.period = ("%s (%s bars)" % (w.period, bars)) if w.period else "%s bars" % bars

    trades = path_get(payload, "metrics.trades")
    if trades is None:
        trades = dig(payload, ["trades", "nTrades", "tradeCount"])
    if isinstance(trades, list):
        trades = len(trades)
    if trades is not None:
        try:
            w.trades = str(int(trades))
        except (TypeError, ValueError):
            w.trades = str(trades)

    bps = path_get(payload, "methodology.costBpsPerSide")
    if bps is None:
        bps = dig(payload, ["costBpsPerSide", "costBps", "per_side_bps", "costsBps"])
    if bps is not None:
        try:
            w.costs = "%g bps per side" % float(bps)
        except (TypeError, ValueError):
            pass

    if not w.strategy:
        w.strategy = path_get(payload, "strategy.name")
    if not w.asset:
        ticker = path_get(payload, "data.ticker")
        tf = path_get(payload, "data.timeframe")
        if ticker and tf:
            w.asset = "%s %s" % (ticker, tf)
        elif ticker:
            w.asset = ticker


GATE_SHORT = {"pbo": "PBO", "dsr": "Deflated Sharpe", "wf": "Out-of-sample"}


def read_day5(w: Week, payload) -> None:
    if payload is None:
        return
    txt = text_of(payload)
    if txt is not None:
        v = from_text(txt, ["Verdict", "Result", "Outcome"])
        w.verdict = v.strip().upper() if v else None
        w.pbo = from_text(txt, ["PBO", "Overfitting"])
        w.dsr = from_text(txt, ["Deflated Sharpe", "DSR"])
        w.oos = from_text(txt, ["Out-of-sample", "OOS", "Walk-forward"])
        return

    verdict = path_get(payload, "engineVerdict") or dig(
        payload, ["verdict", "engineVerdict"]
    )
    if isinstance(verdict, str):
        w.verdict = verdict.strip().upper()

    if not w.strategy:
        w.strategy = path_get(payload, "strategyName")
    if not w.asset:
        asset = path_get(payload, "asset")
        interval = path_get(payload, "interval")
        if asset and interval:
            w.asset = "%s %s" % (asset, interval)
        elif asset:
            w.asset = asset
    cost_bps = path_get(payload, "costBps")
    if cost_bps is not None and not w.costs:
        try:
            w.costs = "%g bps per side" % float(cost_bps)
        except (TypeError, ValueError):
            pass
    tried = path_get(payload, "configurationsTried")
    if tried is not None:
        w.configurations = str(tried)

    # Day 5 hands over a fully rendered gate list. Use it rather than
    # re-deriving thresholds here, so a day-5 change carries through.
    gates = path_get(payload, "gates")
    if isinstance(gates, list):
        for g in gates:
            if not isinstance(g, dict):
                continue
            label = GATE_SHORT.get(
                g.get("key"), g.get("label") or g.get("key") or "gate"
            )
            ran = bool(g.get("ran", True))
            detail = str(g.get("detail") or "").strip()
            # A gate that ran: keep the threshold clause only, the asides
            # (SR*, walk-forward efficiency) stay in verdict.json.
            # A gate that did NOT run: keep the whole reason. It is the point.
            if ran:
                detail = detail.split(";")[0].strip()
            # "needs to be under 50%" -> "needs under 50%", so the threshold
            # sits on the same line as the number it judges.
            detail = re.sub(r"^needs to be\s+", "needs ", detail)
            value = str(g.get("value") or MISSING).replace("OOS Sharpe", "Sharpe")
            # The fold count is in verdict.json; the card carries the number.
            value = re.sub(r"\s+over \d+ folds?$", "", value)
            w.gates.append(
                {
                    "label": label,
                    "value": value,
                    "detail": detail,
                    "pass": bool(g.get("pass")),
                    "ran": ran,
                }
            )

    # Explicit null means the check could not run. That is not "not found".
    if has_path(payload, "pboPct"):
        v = path_get(payload, "pboPct")
        w.pbo = ("%g%%" % float(v)) if v is not None else UNRUN
    if has_path(payload, "deflatedSharpe"):
        v = path_get(payload, "deflatedSharpe")
        w.dsr = num(v) if v is not None else UNRUN
    wf = path_get(payload, "walkForward")
    if isinstance(wf, dict):
        if wf.get("run"):
            sharpe = num(wf.get("oosSharpe"))
            folds = wf.get("folds")
            w.oos = (
                "%s Sharpe over %s folds" % (sharpe, folds)
                if folds
                else "%s Sharpe" % sharpe
            )
        else:
            w.oos = UNRUN


DAY6_FILL_EVENTS = {"open", "close"}


def read_day6(w: Week, state, journal) -> None:
    if isinstance(state, dict) and "__text__" not in state:
        cycles = path_get(state, "loop.cycle_count")
        if cycles is not None:
            w.runs = str(cycles)
        engaged = path_get(state, "kill_switch.engaged")
        if engaged is not None:
            w.stopped = bool(engaged)
        at = path_get(state, "kill_switch.engaged_at")
        if at:
            w.stopped_at = str(at)[:10]

    if isinstance(journal, list):
        fills = 0
        for row in journal:
            if not isinstance(row, dict):
                continue
            event = str(row.get("event") or "").strip().lower()
            if event in DAY6_FILL_EVENTS:
                fills += 1
            elif event == "kill-switch-engaged":
                w.stopped = True
                if not w.stopped_at and row.get("at"):
                    w.stopped_at = str(row["at"])[:10]
        w.fills = str(fills)
        if w.runs is None:
            cycles = [
                r.get("cycle")
                for r in journal
                if isinstance(r, dict) and isinstance(r.get("cycle"), int)
            ]
            if cycles:
                w.runs = str(max(cycles))


def gather(
    day_files: dict[int, Path | None], journal_path: Path | None, how: dict[int, str]
) -> Week:
    w = Week()
    for day, path in day_files.items():
        if path is None:
            w.missing_days.append(day)
        else:
            w.sources[day] = str(path)
            if how.get(day) == "keyword":
                w.unbound_days.append(day)
    if journal_path:
        w.journal_source = str(journal_path)

    read_day3(w, load_json(day_files.get(3)))
    read_day4(w, load_json(day_files.get(4)))
    read_day5(w, load_json(day_files.get(5)))
    read_day6(w, load_json(day_files.get(6)), load_json(journal_path))
    return w


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def wrap(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for word in str(text).split():
        while len(word) > width:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:width])
            word = word[width:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= width:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


class Card:
    INDENT = 12

    def __init__(self, width: int) -> None:
        self.w = width
        self.rows: list[str] = []

    @property
    def avail(self) -> int:
        return self.w - 5 - self.INDENT

    def rule(self) -> None:
        self.rows.append("+" + "-" * (self.w - 2) + "+")

    def blank(self) -> None:
        self.rows.append("|" + " " * (self.w - 2) + "|")

    def line(self, text: str = "") -> None:
        body = (" " + text)[: self.w - 3].ljust(self.w - 2)
        self.rows.append("|" + body + "|")

    def kv(self, label: str, value) -> None:
        val = MISSING if value in (None, "", []) else str(value)
        parts = wrap(val, self.avail)
        self.line(label.ljust(self.INDENT) + parts[0])
        for extra in parts[1:]:
            self.line(" " * self.INDENT + extra)

    def two_col(self, left: str, right: str, col: int = 28) -> None:
        """One line if it fits, otherwise the right half on its own line."""
        placed = left + " " * max(2, col - len(left))
        if right and len(placed) + len(right) <= self.avail:
            self.line(" " * self.INDENT + placed + right)
            return
        for row in wrap(left, self.avail):
            self.line(" " * self.INDENT + row)
        for row in wrap(right, self.avail - 2):
            self.line(" " * (self.INDENT + 2) + row)

    def render(self) -> str:
        return "\n".join(self.rows)


FOOTER = "   Paper only. A pass is not permission to trade real money."


def build_card(w: Week, name: str, learned: str | None, width: int) -> str:
    c = Card(width)
    c.rule()
    c.line("PART-TIME QUANT ACADEMY  -  WEEK ONE")
    c.line("%s  -  %s" % ((name.strip() or "member"), date.today().isoformat()))
    c.rule()
    c.blank()

    c.kv("BUILT", w.strategy)
    detail = " - ".join(x for x in [w.asset, w.rule] if x)
    if detail:
        c.kv("", detail)
    c.blank()

    tested = " - ".join(
        x
        for x in [
            w.period,
            ("%s trades" % w.trades) if w.trades else None,
        ]
        if x
    )
    c.kv("TESTED", tested or None)
    if w.costs:
        c.kv("", "costs %s" % w.costs)
    if w.configurations:
        c.kv("", "%s configurations tried" % w.configurations)
    c.blank()

    c.kv("VERDICT", w.verdict)
    if w.gates:
        for g in w.gates:
            mark = "PASS" if g["pass"] else ("FAIL" if g["ran"] else "N/A ")
            c.two_col("[%s] %s %s" % (mark, g["label"], g["value"]), g["detail"])
    else:
        c.two_col("PBO %s" % (w.pbo or MISSING), "needs to be under 50%")
        c.two_col("Deflated Sharpe %s" % (w.dsr or MISSING), "needs to be over 0.95")
        c.two_col("Out-of-sample %s" % (w.oos or MISSING), "")
    c.blank()

    bits = []
    if w.runs is not None:
        bits.append("ran %s time%s" % (w.runs, "" if w.runs == "1" else "s"))
    if w.fills is not None:
        bits.append("%s paper fill%s" % (w.fills, "" if w.fills == "1" else "s"))
    if w.stopped is True:
        bits.append("I stopped it" + (" on %s" % w.stopped_at if w.stopped_at else ""))
    elif w.stopped is False:
        bits.append("still armed")
    c.kv("AGENT", " - ".join(bits) if bits else None)
    c.blank()

    c.kv("LEARNED", learned.strip() if learned else "< one line, in your words >")
    c.blank()
    c.rule()
    return c.render() + "\n" + FOOTER


def build_partner_block(w: Week, tz, hours, nxt, experience) -> str:
    return "\n".join(
        [
            "PARTNER INTAKE  -  paste this under your post",
            "",
            "  Timezone:        %s" % (tz or "< city or UTC offset >"),
            "  I can show up:   %s" % (hours or "< days + how long >"),
            "  Week two goal:   %s" % (nxt or "< one line, concrete >"),
            "  Verdict:         %s" % (w.verdict or MISSING),
            "  Experience:      %s" % (experience or "< one line >"),
        ]
    )


DAY_LABEL = {3: "the spec", 4: "the backtest", 5: "the verdict", 6: "the agent"}


def build_notes(w: Week, workspace: Path) -> str:
    out = ["Read from: %s" % workspace]
    for day in sorted(w.sources):
        flag = (
            "  (matched by name, not the expected filename)"
            if day in w.unbound_days
            else ""
        )
        out.append("  day %d  %s%s" % (day, w.sources[day], flag))
    if w.journal_source:
        out.append("  day 6  %s" % w.journal_source)

    if w.missing_days:
        out.append("")
        out.append(
            "Not found: %s."
            % ", ".join(
                "day %d (%s)" % (d, DAY_LABEL[d]) for d in sorted(w.missing_days)
            )
        )
        out.append(
            "Those lines print as '%s'. Nothing has been filled in for you." % MISSING
        )
        if 5 in w.missing_days:
            out.append(
                "Day 5 is the verdict. Without it the card has no answer on it -"
            )
            out.append("go back and run day 5 before you post.")
        out.append("If your week lives somewhere else, re-run with:  --dir <path>")

    if any(g for g in w.gates if not g["ran"]):
        out.append("")
        out.append("A check marked N/A could not run. That is not a check that passed.")
    return "\n".join(out)


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the week-one card.")
    ap.add_argument(
        "--dir", dest="dir", default=None, help="academy workspace (default ~/quant)"
    )
    ap.add_argument("--name", default="", help="member's display name")
    ap.add_argument("--learned", default=None, help="the one line they wrote")
    ap.add_argument("--tz", default=None, help="timezone, city or UTC offset")
    ap.add_argument("--hours", default=None, help="when they can show up")
    ap.add_argument("--next", dest="nxt", default=None, help="week two goal, one line")
    ap.add_argument("--experience", default=None, help="one line of background")
    ap.add_argument("--width", type=int, default=68, help="card width in characters")
    ap.add_argument(
        "--json",
        dest="want_json",
        action="store_true",
        help="also write week-one-card.json",
    )
    args = ap.parse_args()

    workspace = find_workspace(args.dir)
    if workspace is None:
        print("Couldn't find your academy workspace.")
        print("Day 1 made a folder called 'quant' in your home folder. I looked there,")
        print(
            "in this folder, in Documents and on the Desktop, and it isn't in any of them."
        )
        print("")
        print("Tell me where this week's work is and re-run with:")
        print("  python3 build_card.py --dir /path/to/your/quant")
        return 2

    day_files, journal_path, how = resolve_days(workspace)
    week = gather(day_files, journal_path, how)

    card = build_card(week, args.name, args.learned, args.width)
    partner = build_partner_block(week, args.tz, args.hours, args.nxt, args.experience)
    notes = build_notes(week, workspace)

    print(card)
    print("")
    print(partner)
    print("")
    print(notes)

    try:
        out = workspace / "week-one-card.txt"
        out.write_text(
            card + "\n\n" + partner + "\n\n" + notes + "\n", encoding="utf-8"
        )
        print("")
        print("Saved: %s" % out)
    except OSError as exc:
        print("")
        print(
            "Couldn't save the card to disk (%s). It's printed above - screenshot it."
            % exc
        )

    if args.want_json:
        payload = {
            "schema": "ptq-academy/week-one-card/1",
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "workspace": str(workspace),
            "sources": week.sources,
            "journalSource": week.journal_source,
            "missingDays": sorted(week.missing_days),
            "unboundDays": sorted(week.unbound_days),
            "strategy": week.strategy,
            "asset": week.asset,
            "rule": week.rule,
            "sourceRule": week.source_rule,
            "vagueTermsResolved": week.vague_resolved,
            "period": week.period,
            "trades": week.trades,
            "costs": week.costs,
            "configurationsTried": week.configurations,
            "verdict": week.verdict,
            "gates": week.gates,
            "pbo": week.pbo,
            "deflatedSharpe": week.dsr,
            "outOfSample": week.oos,
            "runs": week.runs,
            "paperFills": week.fills,
            "stoppedByMember": week.stopped,
            "stoppedAt": week.stopped_at,
            "learned": args.learned,
            "partner": {
                "timezone": args.tz,
                "availability": args.hours,
                "weekTwoGoal": args.nxt,
                "verdict": week.verdict,
                "experience": args.experience,
            },
        }
        try:
            (workspace / "week-one-card.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            print("Saved: %s" % (workspace / "week-one-card.json"))
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
