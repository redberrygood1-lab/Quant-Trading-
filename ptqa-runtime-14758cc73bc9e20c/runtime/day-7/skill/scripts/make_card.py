"""Render Day 7 evidence from an explicit workspace; never infer success from missing data."""
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
                        help="Keep available evidence but omit invalidated Day 4/5 results")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    try:
        card, status = build_card(workspace, allow_incomplete=args.incomplete)
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

DAY = 7
EY = "PART-TIME QUANT ACADEMY / DAY 7"

def gate_blocks(verdict):
    gates = verdict["gates"]
    if not isinstance(gates, list) or len(gates) != 3:
        raise ValueError("Expected the pbo, dsr and wf gates")
    if {g.get("key") for g in gates if isinstance(g, dict)} != {"pbo", "dsr", "wf"}:
        raise ValueError("Missing or duplicate gate keys")
    blocks = []
    failed = unavailable = 0
    for gate in gates:
        if type(gate.get("ran")) is not bool or type(gate.get("pass")) is not bool:
            raise ValueError("Every gate needs boolean ran and pass fields")
        label = text(gate["label"])
        detail = text(gate["detail"])
        if not gate["ran"]:
            unavailable += 1
            blocks.append(("text", label + ": COULD NOT RUN. " + detail))
        else:
            failed += not gate["pass"]
            blocks.append(("check", label, text(gate["value"]), detail, gate["pass"]))
    badge = "INCOMPLETE" if unavailable else ("FAIL" if failed else "PASS")
    blocks.append(("text", "%d passed; %d failed; %d could not run." %
                   (3 - unavailable - failed, failed, unavailable)))
    return blocks, badge

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

def build_card(workspace, allow_incomplete=False):
    invalidated = any((workspace / name).exists() for name in (".day4-incomplete", ".day5-incomplete"))
    if invalidated and not allow_incomplete:
        raise ValueError("A recent test did not finish. Complete it before combining the results.")
    blocks, missing, sources = [], [], {}
    for name in ("strategy.json", "backtest-result.json", "verdict.json"):
        if invalidated and name != "strategy.json":
            missing.append(name)
            blocks.append(("text", name + ": omitted because a recent test did not finish."))
            continue
        try:
            sources[name] = load_json(workspace, name)
        except (OSError, ValueError) as exc:
            missing.append(name)
            blocks.append(("text", name + ": not available. " + str(exc)))
    verdict = sources.get("verdict.json")
    if verdict:
        if verdict.get("schema") != "ptq-academy/verdict/1":
            raise ValueError("Unsupported verdict schema")
        gates, verdict_badge = gate_blocks(verdict)
        blocks.extend(gates)
        blocks.append(("text", "Validation checks: " + verdict_badge))
    else:
        verdict_badge = "INCOMPLETE"
    result = sources.get("backtest-result.json")
    if result:
        if result.get("result_version") != 1:
            raise ValueError("Unsupported backtest result version")
        data, metrics = result["data"], result["metrics"]
        blocks.append(("text", "Historical test: %g trades; %g bps per side; %s to %s." %
                       (number(metrics["trades"]), number(data["costBps"]),
                        text(data["start"]), text(data["end"]))))
    if verdict and result and (verdict["strategyName"] != result["specName"]
                               or verdict["asset"] != result["data"]["symbol"]
                               or verdict["bar"] != result["data"]["bar"]):
        missing.append("matching results")
        blocks.append(("text", "Day 4 and Day 5 identify different tests. Rerun the affected days."))
    spec = sources.get("strategy.json")
    if spec and (spec.get("spec_version") != 1 or not isinstance(spec.get("unresolved"), list)):
        raise ValueError("Invalid strategy source")
    if spec and spec["unresolved"]:
        missing.append("resolved specification")
        blocks.extend(("text", "Unresolved: " + text(item)) for item in spec["unresolved"])
    if spec and verdict and spec.get("name") != verdict["strategyName"]:
        missing.append("matching strategy")
        blocks.append(("text", "Saved strategy name differs from the verdict. Check which version was tested."))
    try:
        paper_blocks, recorded = paper_evidence(workspace)
        blocks.extend(paper_blocks)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        recorded = False
        missing.append("Day 6 evidence")
        blocks.append(("text", "Day 6 evidence not available: " + str(exc)))
    reflection = workspace / "week-one-card.json"
    if reflection.exists():
        saved = load_json(workspace, reflection.name)
        if spec and saved.get("strategy") != spec.get("name"):
            blocks.append(("text", "Saved reflection belongs to another strategy; review it before posting."))
        else:
            for label, value in (("What I learned", saved.get("learned")),
                                 ("Next experiment", saved.get("partner", {}).get("weekTwoGoal"))):
                blocks.append(("text", label + ": " + (value.strip() if isinstance(value, str) and value.strip() else "not supplied")))
    else:
        blocks.append(("text", "Member reflection: not supplied to this card."))
    return {
        "eyebrow": EY, "title": "Week one evidence",
        "subtitle": "Saved results and remaining checks",
        "badge": "INCOMPLETE" if missing or not recorded or verdict_badge == "INCOMPLETE" else "REVIEW",
        "blocks": blocks,
        "footer": "Paper only. Process cleanup and readiness for live trading are not certified.",
    }, 2 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
