"""Save evidence from an explicit data request, without storing keys or prices."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from ptq_data import DataUnavailable, get_bars


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.home() / "quant")
    parser.add_argument("--source", choices=("tiingo", "twelvedata", "csv"), default="tiingo")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--csv")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    result = {"schema": "ptqa/data-probe/1", "checked_at": now.isoformat(),
              "requested_source": args.source, "symbol": args.symbol, "passed": False}
    try:
        bars, meta = get_bars(args.symbol, "1d", (now - timedelta(days=800)).strftime("%Y-%m-%d"),
                              data_source=args.source, csv_path=args.csv,
                              use_cache=False, allow_unkeyed=False, allow_stale=False)
        result.update(passed=bool(bars), provider=meta["provider"], bars=len(bars),
                      note="Sample request only; the strategy's history requirements are checked separately.")
        if bars:
            result.update(start=bars[0]["time"], end=bars[-1]["time"])
    except DataUnavailable:
        result["note"] = "No prices returned. Check the provider account, key, symbol and connection."
    workspace = args.workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "data-probe.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Price request worked." if result["passed"] else "Price request did not work.")
    print(result["note"])
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
