"""Render Day 3 evidence from an explicit workspace; never infer success from missing data."""
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
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    try:
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

DAY = 3
EY = "PART-TIME QUANT ACADEMY / DAY 3"

def build_card(workspace):
    spec = load_json(workspace, "strategy.json")
    validator = importlib.import_module("validate_spec")
    errors = validator.validate(spec)
    if errors:
        return {
            "eyebrow": EY, "title": "Specification incomplete", "badge": "INVALID",
            "subtitle": "Format validation did not pass",
            "blocks": [("text", "%s: %s" % item) for item in errors.items],
            "footer": "Return to Day 3 to resolve these fields.",
        }, 2
    stop, sizing = spec["stop"], spec["sizing"]
    if stop["type"] == "none":
        stop_text = "None"
    elif stop["type"] == "percent":
        stop_text = "%g%% from entry" % number(stop["value"])
    elif stop["type"] == "swing":
        stop_text = "swing %s of the last %d bars" % ("low" if spec["direction"] == "long" else "high",
                                                       stop["value"])
    else:
        stop_text = "%g x ATR(%d)" % (number(stop["value"]), stop["atr_period"])
    if stop["type"] != "none":
        stop_text += "; " + ("trailing" if stop["trailing"] else "fixed")
        stop_text += "; " + ("intrabar" if stop["intrabar"] else "close only")
    if sizing["method"] == "fixed_fraction":
        size_text = "%g%% of account allocated" % number(sizing["fraction_pct"])
    else:
        size_text = "%g%% planned risk at stop" % number(sizing["risk_per_trade_pct"])
    pending = list(spec["unresolved"])
    custom = any(c[side]["series"] == "custom" for group in ("entry", "exit")
                 for c in spec[group]["conditions"] for side in ("left", "right"))
    if custom:
        pending.append("Custom condition requires simulator support review.")
    if spec["entry"]["max_open_positions"] != 1:
        pending.append("Day 4 supports one open position at a time.")
    blocks = [
        ("kv", "INSTRUMENT", spec["instrument"]["symbol"]
         + (" vs " + spec["instrument"]["second_symbol"] if spec["instrument"].get("second_symbol") else "")
         + " / " + spec["timeframe"]["bar"]),
        ("kv", "DIRECTION", spec["direction"]),
        ("text", "STOP: " + stop_text),
        ("text", "SIZING: " + size_text),
        ("text", "Format validation passed."),
    ]
    blocks.extend(("text", "Warning: " + short) for _, short, _ in validator.warnings(spec))
    blocks.extend(("text", "Unresolved: " + item) for item in pending)
    blocks.append(("text", "Data availability and full simulator compatibility are not verified by this card."))
    return {
        "eyebrow": EY, "title": spec["name"],
        "subtitle": "Unresolved specification" if pending else "Specification format checked",
        "badge": "BLOCKED" if pending else "CHECKED",
        "blocks": blocks, "footer": "No historical performance test has been run by this card.",
    }, 0


if __name__ == "__main__":
    sys.exit(main())
