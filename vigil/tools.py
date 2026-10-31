"""Tools for the vigil agent.

Parsing and grouping are plain, deterministic Python. The LLM never sees the
raw log; it only sees the summary from ``read_logs`` and the clustered groups
from ``group_errors``. The only thing vigil ever writes is a markdown report
under ``reports/``.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from google.adk.tools.tool_context import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"

# Refuse to load anything bigger than this; vigil is for incident triage, not bulk ingestion.
MAX_LOG_BYTES = 50 * 1024 * 1024
MAX_SAMPLES_PER_GROUP = 3
MAX_GROUPS_RETURNED = 40

STATE_ENTRIES = "vigil_entries"
STATE_SOURCE = "vigil_source"

# "2026-10-31 10:15:02 ERROR [payments] message"
# Also tolerates a 'T' separator, fractional seconds and a trailing timezone.
LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
    r"\s+(?P<level>[A-Za-z]+)"
    r"\s+\[(?P<service>[^\]]+)\]"
    r"\s*(?P<message>.*)$"
)

LEVEL_ALIASES = {
    "WARN": "WARNING",
    "ERR": "ERROR",
    "FATAL": "CRITICAL",
    "CRIT": "CRITICAL",
}
LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
ISSUE_LEVELS = {"WARNING", "ERROR", "CRITICAL"}

# Order matters: the most specific patterns run first so a UUID is not eaten
# by the hex rule and an IP:port is not split into numbers.
_NORMALISERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"), "<IP>"),
    (re.compile(r"\b[A-Za-z]+[-_](?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,}\b"), "<ID>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b"), "<HEX>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<N>"),
]
_WHITESPACE = re.compile(r"\s+")


def normalise_message(message: str) -> str:
    """Strip volatile tokens (UUIDs, IPs, hex/ids, numbers) so repeats of one issue match."""
    text = message
    for pattern, replacement in _NORMALISERS:
        text = pattern.sub(replacement, text)
    return _WHITESPACE.sub(" ", text).strip()


def parse_line(line: str, line_no: int = 0) -> dict[str, Any] | None:
    """Parse one log line into {timestamp, level, service, message}, or None if it doesn't match."""
    match = LINE_RE.match(line.rstrip("\r\n"))
    if not match:
        return None
    level = match["level"].upper()
    level = LEVEL_ALIASES.get(level, level)
    return {
        "timestamp": match["timestamp"],
        "level": level,
        "service": match["service"].strip(),
        "message": match["message"].strip(),
        "line_no": line_no,
        "raw": line.rstrip("\r\n"),
    }


def parse_log_file(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parse a log file. Returns (entries, number_of_unparsed_lines)."""
    entries: list[dict[str, Any]] = []
    unparsed = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            entry = parse_line(line, line_no)
            if entry is None:
                unparsed += 1
            else:
                entries.append(entry)
    return entries, unparsed


def cluster_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group WARNING+ entries by (service, normalised message).

    Deterministic: the same entries always give the same groups in the same order
    (worst level first, then most frequent, then service and signature).
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        if entry["level"] not in ISSUE_LEVELS:
            continue
        signature = normalise_message(entry["message"])
        key = (entry["service"], signature)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "service": entry["service"],
                "signature": signature,
                "level": entry["level"],
                "count": 0,
                "first_seen": entry["timestamp"],
                "last_seen": entry["timestamp"],
                "samples": [],
            }
        group["count"] += 1
        if LEVEL_RANK.get(entry["level"], 0) > LEVEL_RANK.get(group["level"], 0):
            group["level"] = entry["level"]
        group["first_seen"] = min(group["first_seen"], entry["timestamp"])
        group["last_seen"] = max(group["last_seen"], entry["timestamp"])
        if len(group["samples"]) < MAX_SAMPLES_PER_GROUP:
            group["samples"].append(entry["raw"])

    ordered = sorted(
        groups.values(),
        key=lambda g: (-LEVEL_RANK.get(g["level"], 0), -g["count"], g["service"], g["signature"]),
    )
    for index, group in enumerate(ordered, start=1):
        group["id"] = f"G{index}"
    return ordered


def _resolve_log_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and not candidate.exists():
        # Let "sample_logs/x.log" work no matter where `adk web` was started from.
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def read_logs(path: str, tool_context: ToolContext) -> dict:
    """Read and parse a server log file. Always call this first.

    Each line is parsed into timestamp, level, service and message, e.g.
    "2026-10-31 10:15:02 ERROR [payments] connection refused". The parsed
    entries are kept in session state for group_errors; this returns only a
    summary so the raw log is not sent to the model.

    Args:
        path: Path to the log file, absolute or relative to the project root
            (for example "sample_logs/payments_incident.log").

    Returns:
        A dict with status, line counts, counts per level, services seen and
        the time range covered, or status "error" and an error_message.
    """
    log_path = _resolve_log_path(path)
    if not log_path.is_file():
        return {"status": "error", "error_message": f"No such log file: {log_path}"}
    if log_path.stat().st_size > MAX_LOG_BYTES:
        return {"status": "error", "error_message": f"Log file is larger than {MAX_LOG_BYTES} bytes."}

    entries, unparsed = parse_log_file(log_path)
    tool_context.state[STATE_ENTRIES] = entries
    tool_context.state[STATE_SOURCE] = str(log_path)

    level_counts: dict[str, int] = {}
    services: dict[str, int] = {}
    for entry in entries:
        level_counts[entry["level"]] = level_counts.get(entry["level"], 0) + 1
        services[entry["service"]] = services.get(entry["service"], 0) + 1

    timestamps = [e["timestamp"] for e in entries]
    return {
        "status": "success",
        "path": str(log_path),
        "parsed_lines": len(entries),
        "unparsed_lines": unparsed,
        "level_counts": dict(sorted(level_counts.items(), key=lambda kv: -LEVEL_RANK.get(kv[0], 0))),
        "services": dict(sorted(services.items())),
        "first_timestamp": min(timestamps) if timestamps else None,
        "last_timestamp": max(timestamps) if timestamps else None,
    }


def group_errors(tool_context: ToolContext) -> dict:
    """Group the errors and warnings from the last read_logs call into distinct issues.

    Messages are normalised (numbers, UUIDs, IPs, hex and request ids removed)
    and clustered by service + normalised message, so repeats of the same
    underlying problem become one group. Call read_logs before this.

    Returns:
        A dict with status and a list of groups. Each group has id, service,
        level (worst seen), signature (normalised message), count, first_seen,
        last_seen and up to three raw sample lines.
    """
    entries = tool_context.state.get(STATE_ENTRIES)
    if entries is None:
        return {"status": "error", "error_message": "No log loaded. Call read_logs first."}

    groups = cluster_entries(entries)
    return {
        "status": "success",
        "source": tool_context.state.get(STATE_SOURCE),
        "issue_entries": sum(g["count"] for g in groups),
        "group_count": len(groups),
        "groups": groups[:MAX_GROUPS_RETURNED],
        "truncated": len(groups) > MAX_GROUPS_RETURNED,
    }


def write_report(report_markdown: str) -> dict:
    """Save the final incident report as markdown. Call this last, exactly once.

    The file name is chosen by vigil (reports/<timestamp>.md); this is the
    only thing vigil ever writes.

    Args:
        report_markdown: The complete incident report in markdown.

    Returns:
        A dict with status and the path the report was saved to.
    """
    if not report_markdown.strip():
        return {"status": "error", "error_message": "Report is empty."}

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    report_path = REPORTS_DIR / f"{stamp}.md"
    suffix = 1
    while report_path.exists():
        report_path = REPORTS_DIR / f"{stamp}_{suffix}.md"
        suffix += 1
    report_path.write_text(report_markdown.rstrip() + "\n", encoding="utf-8")
    return {"status": "success", "report_path": str(report_path)}
