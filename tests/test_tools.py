from pathlib import Path

from vigil.tools import (
    group_errors,
    normalise_message,
    parse_line,
    read_logs,
    write_report,
)

SAMPLE_LOGS = Path(__file__).resolve().parent.parent / "sample_logs"


class FakeToolContext:
    """Stand-in for ADK's ToolContext; the tools only use .state."""

    def __init__(self) -> None:
        self.state: dict = {}


def write_log(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "app.log"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_line_common_format():
    entry = parse_line("2026-10-31 10:15:02 ERROR [payments] card declined for order 123")
    assert entry["timestamp"] == "2026-10-31 10:15:02"
    assert entry["level"] == "ERROR"
    assert entry["service"] == "payments"
    assert entry["message"] == "card declined for order 123"


def test_parse_line_normalises_level_aliases_and_rejects_garbage():
    assert parse_line("2026-10-31 10:15:02 WARN [api] slow")["level"] == "WARNING"
    assert parse_line("2026-10-31T10:15:02.123Z FATAL [db] down")["level"] == "CRITICAL"
    assert parse_line("    at com.example.Foo.bar(Foo.java:12)") is None


def test_read_logs_summarises_and_stores_entries(tmp_path):
    path = write_log(
        tmp_path,
        "2026-10-31 10:00:00 INFO [api] started\n"
        "2026-10-31 10:00:05 ERROR [payments] timeout after 5000ms\n"
        "    at some.stack.Frame(File.java:1)\n"
        "\n"
        "2026-10-31 10:01:00 WARN [api] slow request\n",
    )
    ctx = FakeToolContext()
    result = read_logs(str(path), ctx)

    assert result["status"] == "success"
    assert result["parsed_lines"] == 3
    assert result["unparsed_lines"] == 1
    assert result["level_counts"] == {"ERROR": 1, "WARNING": 1, "INFO": 1}
    assert result["services"] == {"api": 2, "payments": 1}
    assert result["first_timestamp"] == "2026-10-31 10:00:00"
    assert result["last_timestamp"] == "2026-10-31 10:01:00"
    assert len(ctx.state["vigil_entries"]) == 3


def test_read_logs_missing_file(tmp_path):
    result = read_logs(str(tmp_path / "nope.log"), FakeToolContext())
    assert result["status"] == "error"


def test_normalise_message_strips_volatile_tokens():
    a = normalise_message(
        "POST /checkout -> 500 (5012ms) client=203.0.113.9 req=req-8f3a2b1c "
        "session 3f2b8c1e-1d2a-4c3b-9e8f-0a1b2c3d4e5f"
    )
    b = normalise_message(
        "POST /checkout -> 500 (30001ms) client=198.51.100.77 req=req-00ffaa11 "
        "session 9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
    )
    assert a == b
    assert "<IP>" in a and "<UUID>" in a and "<ID>" in a


def test_group_errors_clusters_by_service_and_normalised_message(tmp_path):
    path = write_log(
        tmp_path,
        "2026-10-31 10:00:00 INFO [api] all good\n"
        "2026-10-31 10:00:01 ERROR [db] pool exhausted (active=20, waiting=31)\n"
        "2026-10-31 10:00:09 ERROR [db] pool exhausted (active=20, waiting=48)\n"
        "2026-10-31 10:00:04 CRITICAL [db] pool exhausted (active=20, waiting=90)\n"
        "2026-10-31 10:00:05 ERROR [api] pool exhausted (active=20, waiting=12)\n"
        "2026-10-31 10:00:06 WARN [api] cache miss ratio 41%\n",
    )
    ctx = FakeToolContext()
    read_logs(str(path), ctx)
    result = group_errors(ctx)

    assert result["status"] == "success"
    groups = result["groups"]
    # INFO is ignored; db and api pool messages stay separate groups.
    assert [(g["service"], g["count"]) for g in groups] == [("db", 3), ("api", 1), ("api", 1)]

    db = groups[0]
    assert db["id"] == "G1"
    assert db["level"] == "CRITICAL"  # worst level seen wins
    assert db["signature"] == "pool exhausted (active=<N>, waiting=<N>)"
    assert db["first_seen"] == "2026-10-31 10:00:01"
    assert db["last_seen"] == "2026-10-31 10:00:09"
    assert len(db["samples"]) == 3


def test_group_errors_is_deterministic_and_needs_read_logs_first():
    assert group_errors(FakeToolContext())["status"] == "error"

    first, second = FakeToolContext(), FakeToolContext()
    read_logs(str(SAMPLE_LOGS / "payments_incident.log"), first)
    read_logs(str(SAMPLE_LOGS / "payments_incident.log"), second)
    assert group_errors(first) == group_errors(second)


def test_sample_incident_log_surfaces_pool_exhaustion():
    ctx = FakeToolContext()
    read_logs(str(SAMPLE_LOGS / "payments_incident.log"), ctx)
    groups = group_errors(ctx)["groups"]
    top = groups[0]
    assert top["service"] == "payments"
    assert top["level"] == "CRITICAL"
    assert "Connection is not available" in top["signature"]


def test_write_report_saves_markdown(tmp_path, monkeypatch):
    import vigil.tools as tools

    monkeypatch.setattr(tools, "REPORTS_DIR", tmp_path / "reports")
    result = write_report("# Incident report\n\nAll quiet.")
    saved = Path(result["report_path"])
    assert result["status"] == "success"
    assert saved.parent == tmp_path / "reports"
    assert saved.suffix == ".md"
    assert saved.read_text(encoding="utf-8").startswith("# Incident report")
    assert write_report("   ")["status"] == "error"
