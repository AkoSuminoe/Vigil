"""python -m vigil path/to.log [--save] [--json]"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .pipeline import DEFAULT_TIMEOUT_S, analyze
from .tools import save_report


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m vigil", description="Triage a server log (read-only).")
    parser.add_argument("log", type=Path)
    parser.add_argument("--save", action="store_true", help="also save the report to reports/<timestamp>.md")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # the report has arrows; Windows may default to cp1252

    text = args.log.read_text(encoding="utf-8", errors="replace")
    result = asyncio.run(analyze(text, args.log.name, timeout=args.timeout))

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result["report_markdown"])
    if args.save:
        print(f"Saved {save_report(result['report_markdown'])}", file=sys.stderr)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
