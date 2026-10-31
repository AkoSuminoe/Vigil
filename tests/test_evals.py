from evals.run import LOGS_DIR, load_cases, matches_root_cause
from vigil.tools import analyse_entries, parse_log_file


def test_every_case_has_a_log_and_a_root_cause():
    cases = load_cases()
    assert len(cases) >= 3
    for case in cases:
        assert (LOGS_DIR / case["log"]).is_file()
        assert case["root_cause"]["service"]


def test_eval_logs_are_symptom_heavy():
    """The baseline alone must miss every root cause, otherwise the eval measures nothing."""
    for case in load_cases():
        groups = analyse_entries(parse_log_file(LOGS_DIR / case["log"])[0])
        assert not matches_root_cause(groups[0], case["root_cause"]), case["name"]
        assert any(matches_root_cause(g, case["root_cause"]) for g in groups), case["name"]


def test_matcher_checks_service_and_optional_signature():
    group = {"service": "redis", "signature": "OOM command not allowed"}
    assert matches_root_cause(group, {"service": "redis"})
    assert matches_root_cause(group, {"service": "redis", "signature_contains": ["oom"]})
    assert not matches_root_cause(group, {"service": "redis", "signature_contains": ["disk"]})
    assert not matches_root_cause(group, {"service": "auth"})
    assert not matches_root_cause(None, {"service": "redis"})
