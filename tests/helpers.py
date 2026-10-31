"""Shared fixtures: a tiny symptom-heavy log and a well-formed agent answer for it."""

import json

from vigil.tools import analyse_entries, parse_log_text

# Line numbers matter: the tests cite them as evidence.
LOG = "\n".join(
    [
        "2026-10-31 10:00:00 INFO [api] started",  # 1
        "2026-10-31 10:00:01 CRITICAL [db] connection pool exhausted (active=20, waiting=40)",  # 2
        "2026-10-31 10:00:02 ERROR [api] POST /checkout -> 500 req=req-0000aa01",  # 3
        "2026-10-31 10:00:03 ERROR [api] POST /checkout -> 500 req=req-0000aa02",  # 4
        "2026-10-31 10:00:04 ERROR [api] POST /checkout -> 500 req=req-0000aa03",  # 5
        "    at Handler.checkout(Handler.java:12)",  # 6 (unparsed)
        "2026-10-31 10:00:05 WARNING [api] cache miss ratio 41%",  # 7
        "2026-10-31 10:00:06 ERROR [api] POST /checkout -> 500 req=req-0000aa04",  # 8
    ]
)
LOG_LINES = LOG.splitlines()


def groups():
    entries, _ = parse_log_text(LOG)
    # Baseline: G1 api 500s (5*4=20), G2 db pool (10*1*recency), G3 cache warning.
    return analyse_entries(entries)


def answer(**overrides):
    """A valid answer that moves the db root cause (G2) above the api symptom (G1)."""
    data = {
        "summary": "DB pool exhausted, checkout returned 500s.",
        "root_cause_group_id": "G2",
        "ranking": ["G2", "G1", "G3"],
        "moves": [
            {"group_id": "G2", "old_position": 2, "new_position": 1, "reason": "Root cause of the 500s."},
            {"group_id": "G1", "old_position": 1, "new_position": 2, "reason": "Symptom of the pool exhaustion."},
        ],
        "findings": [
            finding("G2", "critical", [2], 0.9, True),
            finding("G1", "high", [3, 4], 0.8),
            finding("G3", "low", [7], 0.5),
        ],
    }
    data.update(overrides)
    return json.dumps(data)


def finding(group_id, severity, lines, confidence, root=False):
    return {
        "group_id": group_id,
        "severity": severity,
        "title": f"{group_id} issue",
        "justification": "Because.",
        "likely_cause": "Something.",
        "suggested_fix": "Do something.",
        "evidence_lines": lines,
        "confidence": confidence,
        "is_root_cause": root,
    }
