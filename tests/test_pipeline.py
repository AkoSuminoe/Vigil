import asyncio
import json

import pytest
from helpers import LOG, answer

from vigil.pipeline import AgentTurn, ModelError, analyze


class FakeSession:
    """Plays back scripted agent answers; each send() is one agent turn."""

    def __init__(self, replies, delay=0.0):
        self.replies = list(replies)
        self.messages = []
        self.delay = delay
        self.state = None

    async def send(self, message):
        self.messages.append(message)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return AgentTurn(text=reply, tool_calls=2, llm_calls=3, prompt_tokens=100, output_tokens=50, total_tokens=150)


def run(replies, **kwargs):
    session = FakeSession(replies, delay=kwargs.pop("delay", 0.0))

    def factory(state):
        session.state = state
        return session

    return asyncio.run(analyze(LOG, "test.log", session_factory=factory, **kwargs)), session


def test_valid_answer_produces_verified_reranked_result():
    result, session = run([answer()])
    assert result["status"] == "ok"
    assert [b["id"] for b in result["baseline"]] == ["G1", "G2", "G3"]
    assert [(r["id"], r["old_position"], r["position"]) for r in result["vigil_ranking"]] == [
        ("G2", 2, 1),
        ("G1", 1, 2),
        ("G3", 3, 3),
    ]
    assert result["vigil_ranking"][0]["reason"] == "Root cause of the 500s."
    assert result["root_cause_group_id"] == "G2"
    assert all(f["verification"] == "verified" for f in result["findings"])
    # Session state is pre-seeded; the agent never gets a path.
    assert set(session.state) == {"vigil_summary", "vigil_groups"}


def test_review_flag_is_computed_in_python():
    result, _ = run([answer()])
    flags = {f["group_id"]: f["needs_human_review"] for f in result["findings"]}
    assert flags == {"G2": True, "G1": False, "G3": True}  # critical; 0.8 high; 0.5 low


def test_bad_evidence_is_marked_unverified_not_dropped():
    data = json.loads(answer())
    data["findings"][1]["evidence_lines"] = [2, 500]
    result, _ = run([json.dumps(data)])
    g1 = next(f for f in result["findings"] if f["group_id"] == "G1")
    assert len(result["findings"]) == 3
    assert g1["verification"] == "unverified"
    assert len(g1["problems"]) == 2


def test_invalid_then_valid_retries_once_with_the_errors():
    result, session = run(["not json at all", answer()])
    assert result["status"] == "ok"
    assert result["metrics"]["attempts"] == 2
    assert "failed validation" in session.messages[1]


def test_invalid_twice_returns_clear_error_with_baseline():
    result, session = run(["nope", answer(ranking=["G1"])])
    assert result["status"] == "error"
    assert result["error_kind"] == "invalid_output"
    assert "ranking is missing groups" in result["error"]
    assert len(session.messages) == 2
    assert len(result["baseline"]) == 3
    assert "Analysis failed" in result["report_markdown"]


def test_timeout_is_reported():
    result, _ = run([answer()], delay=1.0, timeout=0.05)
    assert result["status"] == "error"
    assert result["error_kind"] == "timeout"


def test_model_errors_are_reported_not_raised():
    result, _ = run([ModelError("RESOURCE_EXHAUSTED: quota")])
    assert result["error_kind"] == "model"
    assert "RESOURCE_EXHAUSTED" in result["error"]


def test_rejected_api_key_is_a_config_error_with_a_hint():
    result, _ = run([ModelError("PERMISSION_DENIED: 403 ... 'reason': 'API_KEY_SERVICE_BLOCKED' ...")])
    assert result["error_kind"] == "config"
    assert "aistudio.google.com" in result["error"]
    assert result["metrics"]["attempts"] == 1


def test_metrics_are_summed_over_attempts():
    result, _ = run(["bad", answer()])
    metrics = result["metrics"]
    assert metrics["llm_calls"] == 6
    assert metrics["tool_calls"] == 4
    assert metrics["tokens"] == {"prompt": 200, "output": 100, "total": 300}
    assert metrics["latency_ms"] >= 0
    assert "tokens 200 in / 100 out / 300 total" in result["report_markdown"]


def test_missing_api_key_is_a_config_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = asyncio.run(analyze(LOG, "test.log"))
    assert result["error_kind"] == "config"
    assert "GOOGLE_API_KEY" in result["error"]


def test_unrecognised_format_is_an_error_not_an_all_clear():
    result = asyncio.run(analyze("<14>Oct 31 10:00:00 host sshd[42]: Failed password\nanother line\n", "syslog.log"))
    assert result["status"] == "error"
    assert result["error_kind"] == "unsupported_format"
    assert "JSON lines" in result["error"]
    assert result["metrics"]["attempts"] == 0


def test_read_logs_rejects_unrecognised_format(tmp_path):
    from test_tools import FakeToolContext

    path = tmp_path / "nginx.log"
    path.write_text('203.0.113.9 - - [31/Oct/2026:10:00:00 +0000] "GET / HTTP/1.1" 200 512\n', encoding="utf-8")
    from vigil.tools import read_logs

    result = read_logs(str(path), FakeToolContext())
    assert result["status"] == "error" and "supported log format" in result["error_message"]


def test_log_without_issues_skips_the_model():
    result = asyncio.run(analyze("2026-10-31 10:00:00 INFO [api] ok\n", "quiet.log"))
    assert result["status"] == "ok"
    assert result["findings"] == [] and result["metrics"]["attempts"] == 0


@pytest.mark.parametrize("kind", ["ok", "error"])
def test_report_renders_for_both_outcomes(kind):
    replies = [answer()] if kind == "ok" else ["x", "y"]
    result, _ = run(replies)
    assert result["report_markdown"].startswith("# Incident report: test.log")
