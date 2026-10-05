"""Make the Day 1 card from checked setup evidence, not an agent-written status."""
import argparse
import json
from pathlib import Path
import sys
from check_workspace import price_row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.home() / "quant")
    args = parser.parse_args()
    work = args.workspace.expanduser().resolve()
    try:
        result = json.loads((work / "setup-result.json").read_text(encoding="utf-8"))
        if result.get("schema") != "ptqa/setup/1" or not result.get("rows"):
            raise ValueError
        rows = result["rows"]
        if any(r.get("state") not in ("PASS", "FAIL", "WARN") for r in rows):
            raise ValueError
    except (OSError, ValueError, AttributeError, TypeError):
        print("No valid setup check. Run the check before making the card.")
        return 2
    for row in rows:
        if row["name"] == "Price data":
            row.update(price_row(work))
    try:
        from card_render import render
    except ImportError:
        print("Use the academy environment with Pillow and numpy to make the picture.")
        return 2
    blocks = [("check", r["name"], r["detail"], "", "warn" if r["state"] == "WARN" else r["state"] == "PASS") for r in rows]
    failed = any(r["state"] == "FAIL" for r in rows)
    pending = any(r["state"] == "WARN" for r in rows)
    render({"eyebrow": "PART-TIME QUANT ACADEMY - DAY 1", "title": "My workspace check",
            "subtitle": "Checks run on this computer", "badge": "FIX" if failed else "PENDING" if pending else "CHECKED",
            "blocks": blocks, "footer": "Price access is a sample check. Day 4 checks the history your rule needs."}, str(work / "day-1-card.png"))
    print("Card saved:", work / "day-1-card.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
