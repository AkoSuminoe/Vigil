"""Deterministic baseline ranking of error groups.

score = level weight x count x recency

Recency is 0.5 for an issue last seen at the very start of the log and 1.0 for
one still happening at the end, so ongoing problems outrank ones that stopped.
This is the "base rank" the agent has to justify any change to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

LEVEL_WEIGHT = {"CRITICAL": 10, "ERROR": 5, "WARNING": 1}


def parse_timestamp(value: str) -> datetime | None:
    """Parse the timestamps read_logs accepts; timezone info is dropped for comparison."""
    try:
        return datetime.fromisoformat(value.replace(",", ".")).replace(tzinfo=None)
    except ValueError:
        return None


def recency_factor(last_seen: str, log_start: str | None, log_end: str | None) -> float:
    last = parse_timestamp(last_seen)
    start = parse_timestamp(log_start) if log_start else None
    end = parse_timestamp(log_end) if log_end else None
    if last is None or start is None or end is None or end <= start:
        return 1.0
    position = (last - start).total_seconds() / (end - start).total_seconds()
    return 0.5 + 0.5 * min(1.0, max(0.0, position))


def baseline_score(group: dict[str, Any], log_start: str | None, log_end: str | None) -> float:
    weight = LEVEL_WEIGHT.get(group["level"], 0)
    return round(weight * group["count"] * recency_factor(group["last_seen"], log_start, log_end), 2)


def rank_baseline(
    groups: list[dict[str, Any]], log_start: str | None, log_end: str | None
) -> list[dict[str, Any]]:
    """Return the groups sorted by baseline score, with score, base_position and id (G1 = base #1)."""
    scored = [{**group, "score": baseline_score(group, log_start, log_end)} for group in groups]
    scored.sort(
        key=lambda g: (
            -g["score"],
            -LEVEL_WEIGHT.get(g["level"], 0),
            -g["count"],
            g["service"],
            g["signature"],
        )
    )
    for position, group in enumerate(scored, start=1):
        group["base_position"] = position
        group["id"] = f"G{position}"
    return scored
