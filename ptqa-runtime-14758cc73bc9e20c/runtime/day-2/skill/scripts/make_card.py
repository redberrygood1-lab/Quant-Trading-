"""Writes the day 2 card as a PNG the member posts.

Day 2 is a conversation, so there is no script that computes anything. The skill
writes the five answers to ~/quant/rule.json in the member's own words, and this
turns that into the card. Their words, verbatim. Nothing is rephrased here.
"""
import argparse
import json, os, sys

WORK = os.path.join(os.path.expanduser("~"), "quant")
SRC  = os.path.join(WORK, "rule.json")
PNG  = os.path.join(WORK, "day-2-card.png")

FIELDS = [("what",  "WHAT I TRADE"),
          ("in",    "I GET IN WHEN"),
          ("profit","OUT WITH PROFIT"),
          ("loss",  "OUT WITH A LOSS"),
          ("size",  "HOW MUCH")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=WORK)
    args = parser.parse_args(argv)
    workspace = os.path.abspath(os.path.expanduser(args.workspace))
    source = os.path.join(workspace, "rule.json")
    output = os.path.join(workspace, "day-2-card.png")
    if not os.path.exists(source):
        print("  No rule.json yet. Finish and save the five questions first."); return 2
    try:
        with open(source, encoding="utf-8") as fh:
            r = json.load(fh)
    except (OSError, ValueError):
        print("  rule.json could not be read. No new card was made."); return 2

    required = [key for key, _ in FIELDS] + ["one_sentence"]
    if not isinstance(r, dict) or any(
        not isinstance(r.get(key), str) or not r[key].strip() for key in required
    ):
        print("  Save all five answers and the summary before making the card."); return 2
    count = r.get("rule_count")
    if type(count) is not int or count < 1:
        print("  Confirm the number of rules before making the card."); return 2

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import card_render as C
    except ImportError:
        print("  Card image unavailable. Run with the academy environment containing Pillow and numpy.")
        print("  Your saved answers remain intact. No new card was made.")
        return 2

    blocks = [("text", label + ": " + r[key]) for key, label in FIELDS]
    if r.get("one_sentence"):
        blocks += [("rule",), ("text", r["one_sentence"])]
    prediction = r.get("prediction")
    if isinstance(prediction, str) and prediction.strip():
        # Optional for older saved rules; preserve the member's words verbatim.
        blocks += [("rule",),
                   ("text", "MY PREDICTION BEFORE TESTING - UNTESTED, NOT A RESULT"),
                   ("text", prediction)]
    blocks += [("rule",), ("kv", "RULES IN THERE", str(count))]

    C.render({
        "eyebrow": "PART-TIME QUANT ACADEMY   ·   DAY 2",
        "title": "My rule, written down",
        "subtitle": "My answers, saved for tomorrow",
        "blocks": blocks,
        "footer": "Day 3 turns this into a spec with a number in every slot.",
    }, output)
    print("  Your card:  %s" % output)
    print("  Open the new card and check your answers before posting it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
