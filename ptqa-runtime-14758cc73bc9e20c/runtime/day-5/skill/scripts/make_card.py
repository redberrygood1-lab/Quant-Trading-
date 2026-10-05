"""Render Day 5 evidence from an explicit workspace; never infer success from missing data."""
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
                "title": "Day 5 could not finish",
                "subtitle": "No completed validation is shown",
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

DAY = 5
EY = "PART-TIME QUANT ACADEMY / DAY 5"

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

def build_card(workspace):
    if any((workspace / name).exists() for name in (".day4-incomplete", ".day5-incomplete")):
        raise ValueError("A recent test did not finish. An older verdict is not a new card.")
    verdict = load_json(workspace, "verdict.json")
    if verdict.get("schema") != "ptq-academy/verdict/1":
        raise ValueError("Unsupported verdict schema")
    blocks, badge = gate_blocks(verdict)
    return {
        "eyebrow": EY, "title": text(verdict["strategyName"]),
        "subtitle": "%s / %s / %g bars / %g configurations" % (
            text(verdict["asset"]), text(verdict["bar"]), number(verdict["bars"]),
            number(verdict["configurationsTried"])),
        "badge": badge, "blocks": blocks,
        "footer": "These checks do not establish future profitability. Paper only.",
    }, 0


if __name__ == "__main__":
    sys.exit(main())
