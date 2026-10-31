"""Run vigil on each eval log and check it names the known root cause.

    python -m evals.run [--case dns_outage]

Each evals/answers/<name>.json names the log and the root cause:
{"log": "...", "root_cause": {"service": "redis", "signature_contains": ["optional", "substrings"]}}
A case passes when the result's root_cause_group_id points at a group from that
service (and, if given, whose signature contains one of the substrings).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
LOGS_DIR = EVALS_DIR / "logs"
ANSWERS_DIR = EVALS_DIR / "answers"


def load_cases(only: str | None = None) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(ANSWERS_DIR.glob("*.json")):
        if only and path.stem != only:
            continue
        case = json.loads(path.read_text(encoding="utf-8"))
        case["name"] = path.stem
        cases.append(case)
    return cases


def matches_root_cause(group: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    if group is None or group["service"] != expected["service"]:
        return False
    needles = expected.get("signature_contains")
    return not needles or any(n.lower() in group["signature"].lower() for n in needles)


async def run_case(case: dict[str, Any]) -> dict[str, Any]:
    from vigil.pipeline import analyze

    text = (LOGS_DIR / case["log"]).read_text(encoding="utf-8")
    result = await analyze(text, case["log"])
    groups = {g["id"]: g for g in result["baseline"]}
    found = groups.get(result["root_cause_group_id"]) if result["root_cause_group_id"] else None
    findings = result["findings"]
    return {
        "name": case["name"],
        "status": result["status"],
        "error": result["error"],
        "found": result["status"] == "ok" and matches_root_cause(found, case["root_cause"]),
        "named": f"{found['id']} [{found['service']}]" if found else "none",
        "baseline_found": bool(result["baseline"]) and matches_root_cause(result["baseline"][0], case["root_cause"]),
        "verified": sum(f["verification"] == "verified" for f in findings),
        "findings": len(findings),
        "metrics": result["metrics"],
    }


async def main_async(only: str | None) -> int:
    from vigil import agent  # noqa: F401  (loads .env)

    if not os.getenv("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY is not set (put it in .env). Evals call the real model.", file=sys.stderr)
        return 2
    cases = load_cases(only)
    if not cases:
        print("No eval cases found.", file=sys.stderr)
        return 2

    results = []
    for case in cases:
        outcome = await run_case(case)
        results.append(outcome)
        m = outcome["metrics"]
        mark = "PASS" if outcome["found"] else "FAIL"
        print(
            f"{mark}  {outcome['name']:<24} expected [{case['root_cause']['service']}] named {outcome['named']:<20} "
            f"verified {outcome['verified']}/{outcome['findings']}  "
            f"{m['latency_ms'] / 1000:.1f}s  {m['tokens']['total']} tokens  {m['tool_calls']} tool calls"
        )
        if outcome["status"] != "ok":
            print(f"      error: {outcome['error']}")

    total = len(results)
    found = sum(r["found"] for r in results)
    baseline = sum(r["baseline_found"] for r in results)
    tokens = sum(r["metrics"]["tokens"]["total"] for r in results)
    print()
    print(f"Root cause found: {found}/{total}")
    print(f"Baseline #1 was the root cause: {baseline}/{total}")
    print(f"Total tokens: {tokens}")
    return 0 if found == total else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--case", help="run a single case by name (answers/<name>.json)")
    args = parser.parse_args()
    return asyncio.run(main_async(args.case))


if __name__ == "__main__":
    sys.exit(main())
