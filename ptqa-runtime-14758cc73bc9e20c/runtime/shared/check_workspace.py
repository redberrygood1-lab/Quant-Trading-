"""Desktop-neutral checks. Does not require a separately installed agent CLI."""
import argparse
import importlib
import json
from pathlib import Path
import platform
import sys
import tempfile
from datetime import datetime, timedelta, timezone


def price_row(workspace):
    row = {"name": "Price data", "state": "WARN", "detail": "A separate real data probe is required"}
    try:
        probe = json.loads((workspace / "data-probe.json").read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(probe["checked_at"])
        if (probe.get("schema") == "ptqa/data-probe/1"
                and timedelta(0) <= age <= timedelta(hours=24)
                and isinstance(probe.get("passed"), bool)):
            row.update(state="PASS" if probe["passed"] else "FAIL",
                       detail="Sample request succeeded" if probe["passed"] else "Sample request failed")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return row


def check(workspace, app):
    rows = []
    rows.append({"name": "Python", "state": "PASS" if sys.version_info >= (3, 10) else "FAIL",
                 "detail": platform.python_version()})
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=workspace) as stream:
            stream.write(b"ptqa-check")
            stream.seek(0)
            if stream.read() != b"ptqa-check":
                raise OSError("Read-back did not match")
        rows.append({"name": "Saving work", "state": "PASS", "detail": "Write and read-back succeeded"})
    except OSError:
        rows.append({"name": "Saving work", "state": "FAIL", "detail": "Cannot write and read in the selected folder"})
    for module in ("numpy", "pandas", "PIL"):
        try:
            imported = importlib.import_module(module)
            rows.append({"name": "Pillow" if module == "PIL" else module,
                         "state": "PASS", "detail": str(imported.__version__)})
        except ImportError:
            rows.append({"name": "Pillow" if module == "PIL" else module,
                         "state": "FAIL", "detail": "Missing from this Python environment"})
    rows.append(price_row(workspace))
    return {"schema": "ptqa/setup/1", "app_route": app, "machine": platform.system(),
            "rows": rows, "status": "FAIL" if any(r["state"] == "FAIL" for r in rows) else "PENDING DATA" if any(r["state"] == "WARN" for r in rows) else "CHECKED",
            "note": "App route is the requested route, not an automatic verification of the desktop UI."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.home() / "quant")
    parser.add_argument("--app", choices=("claude", "codex"), required=True)
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve()
    result = check(workspace, args.app)
    try:
        (workspace / "setup-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except OSError:
        print("Setup check could not be saved. Check folder permissions.")
        return 2
    for row in result["rows"]:
        print(f'[{row["state"]}] {row["name"]}: {row["detail"]}')
    print("Result:", result["status"])
    return 2 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
