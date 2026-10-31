from pathlib import Path

from vigil.ranking import baseline_score, rank_baseline, recency_factor
from vigil.tools import analyse_entries, parse_log_file

SAMPLE_LOGS = Path(__file__).resolve().parent.parent / "sample_logs"
START, END = "2026-10-31 10:00:00", "2026-10-31 11:00:00"


def group(service, level, count, last_seen=END, signature="x"):
    return {"service": service, "level": level, "count": count, "last_seen": last_seen, "signature": signature}


def test_score_is_level_weight_times_count_times_recency():
    assert baseline_score(group("a", "CRITICAL", 3), START, END) == 30
    assert baseline_score(group("a", "ERROR", 3), START, END) == 15
    assert baseline_score(group("a", "WARNING", 3), START, END) == 3
    # Last seen at the very start of the log: recency 0.5.
    assert baseline_score(group("a", "ERROR", 4, last_seen=START), START, END) == 10


def test_recency_handles_midpoint_and_bad_input():
    assert recency_factor("2026-10-31 10:30:00", START, END) == 0.75
    assert recency_factor("not a time", START, END) == 1.0
    assert recency_factor(END, None, None) == 1.0


def test_recent_issue_outranks_one_that_stopped():
    stopped = group("old", "ERROR", 10, last_seen=START)  # 5 * 10 * 0.5 = 25
    ongoing = group("new", "ERROR", 6)  # 5 * 6 * 1.0 = 30
    ranked = rank_baseline([stopped, ongoing], START, END)
    assert [g["service"] for g in ranked] == ["new", "old"]
    assert [(g["id"], g["base_position"]) for g in ranked] == [("G1", 1), ("G2", 2)]


def test_ties_break_deterministically():
    a = group("b-svc", "ERROR", 2)
    b = group("a-svc", "ERROR", 2)
    c = group("a-svc", "CRITICAL", 1)  # same score (10) as the ERROR x2 groups, higher level
    first = rank_baseline([a, b, c], START, END)
    second = rank_baseline([c, b, a], START, END)
    assert [g["service"] + g["level"] for g in first] == ["a-svcCRITICAL", "a-svcERROR", "b-svcERROR"]
    assert [g["id"] for g in first] == [g["id"] for g in second]


def test_payments_sample_baseline_puts_a_symptom_first():
    """The demo relies on this: counting alone ranks the gateway 500s above the pool exhaustion."""
    entries, _ = parse_log_file(SAMPLE_LOGS / "payments_incident.log")
    groups = analyse_entries(entries)
    top = groups[0]
    assert top["service"] == "api-gateway" and "checkout" in top["signature"]

    pool = next(g for g in groups if "Connection is not available" in g["signature"] and g["level"] == "CRITICAL")
    slow_query = next(g for g in groups if "slow query" in g["signature"])
    assert pool["base_position"] > 1
    assert slow_query["base_position"] > pool["base_position"]
