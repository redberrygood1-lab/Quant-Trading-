"""Render Day 4 evidence from an explicit workspace; never infer success from missing data."""
import argparse
import importlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile


def _reject_constant(value):
    raise ValueError("Non-finite JSON value: " + value)


def load_json(workspace, name):
    with (workspace / name).open(encoding="utf-8") as handle:
        value = json.load(handle, parse_constant=_reject_constant)
    if not isinstance(value, dict) or not value:
        raise ValueError(name + " must contain a non-empty object")
    return value


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return value


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected non-empty text")
    return value


def render_card(card, output):
    renderer = importlib.import_module("card_render")
    # Publish only a completed render; a failed attempt must not replace an old card.
    fd, temporary = tempfile.mkstemp(prefix=".card-", suffix=".png", dir=output.parent)
    os.close(fd)
    try:
        renderer.render(card, temporary)
        if Path(temporary).stat().st_size == 0:
            raise OSError("Renderer produced no image")
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=str(Path.home() / "quant"))
    parser.add_argument("--incomplete", action="store_true",
                        help="Render an explicitly incomplete card when evidence is unavailable")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    try:
        if args.incomplete:
            card, status = {
                "eyebrow": EY,
                "title": "Day 4 could not finish",
                "subtitle": "No completed backtest is shown",
                "badge": "INCOMPLETE",
                "blocks": [("text", "The assistant could not produce a current, complete result."),
                           ("text", "Keep the error in your conversation and ask the assistant to help."),
                           ("text", "No earlier performance figures or passing checks are carried forward.")],
                "footer": "Incomplete evidence. Paper only.",
            }, 2
        else:
            card, status = build_card(workspace)
        output = workspace / ("day-%d-card.png" % DAY)
        render_card(card, output)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as exc:
        print("No new Day %d card: %s" % (DAY, exc))
        print("Check the source files and use the academy environment with its bundled dependencies.")
        return 2
    print("Your card: %s" % output)
    if status:
        print("Evidence is missing or invalid; the card records the incomplete result.")
    return status

DAY = 4
EY = "PART-TIME QUANT ACADEMY / DAY 4"

def build_card(workspace):
    if (workspace / ".day4-incomplete").exists():
        raise ValueError("The latest backtest did not finish. An older result is not a new card.")
    result = load_json(workspace, "backtest-result.json")
    if result.get("result_version") != 1:
        raise ValueError("Unsupported backtest result version")
    data, metrics = result["data"], result["metrics"]
    context = result.get("context", {})
    fields = [("NET RETURN", "netReturnPct", "%+.1f%%"),
              ("SHARPE", "sharpe", "%.2f"),
              ("TRADES", "trades", "%g"),
              ("WIN RATE", "winRatePct", "%.1f%%"),
              ("MAX DRAWDOWN", "maxDrawdownPct", "%.1f%%")]
    blocks = [("kv", label, fmt % number(metrics[key])) for label, key, fmt in fields]
    blocks.append(("kv", "COSTS PER SIDE", "%g bps" % number(data["costBps"])))
    if "buyAndHoldPct" in context:
        blocks.append(("kv", "BUY AND HOLD", "%+.1f%%" % number(context["buyAndHoldPct"])))
    warnings = result.get("warnings", [])
    if not isinstance(warnings, list):
        raise ValueError("Invalid result warnings")
    blocks.extend(("text", text(warning)) for warning in warnings)
    return {
        "eyebrow": EY, "title": text(result["specName"]),
        "subtitle": "%s / %s / %g bars / %s to %s" % (
            text(data["symbol"]), text(data["bar"]), number(data["bars"]),
            text(data["start"]), text(data["end"])),
        "blocks": blocks, "footer": "Historical result only. Future performance is unknown.",
    }, 0


if __name__ == "__main__":
    sys.exit(main())
