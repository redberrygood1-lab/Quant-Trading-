"""Render Day 6 evidence from an explicit workspace; never infer success from missing data."""
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

DAY = 6
EY = "PART-TIME QUANT ACADEMY / DAY 6"

def timestamp(value):
    from datetime import datetime
    result = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Evidence timestamps must include a timezone")
    return result


def paper_evidence(workspace):
    state = load_json(workspace, "run-state.json")
    if (state.get("schema") != "ptq-academy/day6-run-state/1"
            or state.get("paper_only") is not True
            or state.get("live_trading") is not False
            or state.get("broker_connected") is not False):
        raise ValueError("Expected paper-only Day 6 state")
    loop, switch = state["loop"], state["kill_switch"]
    if type(loop.get("running")) is not bool or type(switch.get("engaged")) is not bool:
        raise ValueError("Missing runtime or kill-switch state")
    count = loop["cycle_count"]
    if type(count) is not int or count < 0:
        raise ValueError("Invalid cycle count")
    cycles, fills = state["cycles"], state["fills"]
    if not isinstance(cycles, list) or not isinstance(fills, list):
        raise ValueError("Missing cycle/fill evidence")
    for cycle in cycles:
        if not isinstance(cycle, dict) or type(cycle.get("n")) is not int:
            raise ValueError("Invalid cycle evidence")
        timestamp(cycle["at"])
        text(cycle["outcome"])
    for fill in fills:
        if not isinstance(fill, dict) or fill.get("event") not in ("open", "close"):
            raise ValueError("Invalid state fill evidence")
        timestamp(fill["at"])
    events = []
    with (workspace / "run-log.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            event = json.loads(line, parse_constant=_reject_constant)
            if (not isinstance(event, dict) or event.get("paper") is not True
                    or event.get("simulated") is not True or event.get("live") is not False):
                raise ValueError("Invalid paper journal event")
            timestamp(event["at"])
            text(event["event"])
            events.append(event)
    if not events:
        raise ValueError("Paper journal is empty")
    # Require current-session evidence, not old proved flags or a stop timestamp alone.
    starts = [e for e in events if e["event"] == "loop-start"]
    start = max(starts, key=lambda e: timestamp(e["at"])) if starts else None
    started = timestamp(start["at"]) if start else None
    kills = [e for e in events if e["event"] == "kill-switch-engaged"
             and (started is None or timestamp(e["at"]) >= started)]
    kill = max(kills, key=lambda e: timestamp(e["at"])) if kills else None
    killed = timestamp(kill["at"]) if kill else None
    matched_kill = bool(kill and switch["engaged"] and switch.get("engaged_at")
                        and timestamp(switch["engaged_at"]) <= killed
                        and (started is None or timestamp(switch["engaged_at"]) >= started)
                        and kill.get("by") == switch.get("engaged_by")
                        and isinstance(kill.get("by"), str) and kill["by"].strip())
    released = bool(killed and any(e["event"] == "kill-switch-released"
                    and timestamp(e["at"]) >= killed for e in events))
    ran = bool(started and any(timestamp(c["at"]) >= started
               and (killed is None or timestamp(c["at"]) < killed)
               and c["outcome"] not in ("skipped", "idle") for c in cycles))
    refusal = bool(matched_kill and not released and any(
        e["event"] == "skipped" and e.get("reason") == "kill switch engaged"
        and timestamp(e["at"]) >= killed
        and any(c["n"] == e.get("cycle") and c["outcome"] == "skipped"
                and timestamp(c["at"]) >= killed for c in cycles)
        for e in events))
    later_fill = bool(killed and any(e["event"] in ("open", "close")
                      and timestamp(e["at"]) >= killed for e in events + fills))
    refusal = refusal and not later_fill
    recorded_inactive = loop["running"] is False and loop.get("pid") is None
    fill_events = [e for e in events if e["event"] in ("open", "close")]
    blocks = [
        ("kv", "CYCLES RECORDED", str(count)),
        ("kv", "JOURNALED PAPER FILLS", str(len(fill_events))),
        ("text", "Loop start and subsequent cycle: " + ("recorded" if ran else "not established")),
        ("text", "Member stop: " + ("recorded" if matched_kill and not released else "not established")),
        ("text", "Refusal after stop: " + ("recorded" if refusal else "not established")),
        ("text", "Runtime state: " + ("recorded inactive" if recorded_inactive else "active or not cleared")),
        ("text", "Process and scheduler cleanup have not been checked by this card."),
    ]
    if later_fill:
        blocks.append(("text", "Journal contains a fill after the stop. Investigate before continuing."))
    complete_record = ran and matched_kill and refusal and recorded_inactive and not released
    return blocks, complete_record

def build_card(workspace):
    blocks, complete_record = paper_evidence(workspace)
    return {
        "eyebrow": EY, "title": "Paper drill evidence",
        "subtitle": "Recorded run and refusal" if complete_record else "Evidence incomplete",
        "badge": "RECORDED" if complete_record else "INCOMPLETE",
        "blocks": blocks,
        "footer": "Historical-bar paper simulation. Live runtime cleanup remains a separate check.",
    }, 0


if __name__ == "__main__":
    sys.exit(main())
