from pathlib import Path

from vigil.tools import (
    MAX_TOOL_CALLS,
    analyse_entries,
    enforce_tool_cap,
    get_groups,
    get_log_summary,
    normalise_message,
    parse_line,
    parse_log_text,
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


SLOG_LINE = (
    '{"time":"2024-06-07T22:23:18.5130148+05:00","level":"ERROR","msg":"request failed",'
    '"component":"middleware/logger","request_id":"LAPTOP-TN2607IT/oFXwmM7Ri6-000001",'
    '"remote_addr":"127.0.0.1:64609","user_agent":"Mozilla/5.0","err":"dial tcp 10.0.0.5:5432: connection refused"}'
)


def test_parse_json_line_slog_style():
    entry = parse_line(SLOG_LINE, 7)
    assert entry["timestamp"] == "2024-06-07T22:23:18.5130148+05:00"
    assert entry["level"] == "ERROR"
    assert entry["service"] == "middleware/logger"
    assert entry["message"] == "request failed | error: dial tcp 10.0.0.5:5432: connection refused"
    # Request ids, client address and user agent stay out of the message (grouping + privacy).
    assert "Mozilla" not in entry["message"] and "64609" not in entry["message"]
    assert entry["raw"] == SLOG_LINE and entry["line_no"] == 7


def test_parse_json_line_pino_numeric_level_and_epoch_ms():
    entry = parse_line('{"level":50,"time":1793442902000,"msg":"db down","app":"api"}')
    assert entry["level"] == "ERROR"
    assert entry["timestamp"] == "2026-10-31 10:35:02"
    assert entry["service"] == "api"
    assert parse_line('{"level":60,"time":1793442902,"msg":"x"}')["level"] == "CRITICAL"


def test_parse_json_line_rejects_incomplete_or_non_object_records():
    assert parse_line('{"time":"2024-06-07T22:23:18","level":"INFO"}') is None  # no msg
    assert parse_line('{"time":"2024-06-07T22:23:18","msg":"x"}') is None  # no level
    assert parse_line("{not json") is None
    assert parse_line('{"time":"2024-06-07T22:23:18","level":true,"msg":"x"}') is None


def test_parse_plain_line_without_service():
    entry = parse_line("2024-06-08 10:00:00 - ERROR: Database connection failed")
    assert (entry["level"], entry["service"], entry["message"]) == ("ERROR", "app", "Database connection failed")
    assert parse_line("2024-06-08 10:00:00 WARN disk almost full")["level"] == "WARNING"
    assert parse_line("2024-06-08 10:00:00 - BANANA: x") is None
    assert parse_line("2024-06-08 10:00:00 Starting up") is None


def test_more_level_aliases():
    expected = {"PANIC": "CRITICAL", "alert": "CRITICAL", "EMERG": "CRITICAL", "Severe": "ERROR", "NOTICE": "INFO", "TRACE": "DEBUG"}
    for alias, level in expected.items():
        assert parse_line(f"2026-10-31 10:00:00 {alias} [svc] x")["level"] == level


def test_parse_log_text_keeps_real_line_numbers():
    entries, unparsed = parse_log_text(
        "2026-10-31 10:00:00 INFO [api] started\n"
        "\n"
        "    at some.stack.Frame(File.java:1)\n"
        "2026-10-31 10:00:05 ERROR [payments] timeout\n"
    )
    assert [e["line_no"] for e in entries] == [1, 4]
    assert unparsed == 1


def test_read_logs_summarises_and_stores_groups(tmp_path):
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
    assert result["source"] == "app.log"
    assert result["parsed_lines"] == 3
    assert result["unparsed_lines"] == 1
    assert result["level_counts"] == {"ERROR": 1, "WARNING": 1, "INFO": 1}
    assert result["services"] == {"api": 2, "payments": 1}
    assert result["first_timestamp"] == "2026-10-31 10:00:00"
    assert result["last_timestamp"] == "2026-10-31 10:01:00"
    assert len(ctx.state["vigil_groups"]) == 2
    assert get_log_summary(ctx)["parsed_lines"] == 3


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


def test_normalise_emails_and_random_base62_ids():
    assert normalise_message("password reset failed for alice.smith+test@example.co.uk") == "password reset failed for <EMAIL>"
    assert normalise_message("req LAPTOP-TN2607IT/oFXwmM7Ri6-000001 done") == "req <ID>/<ID>-<N> done"
    # Ordinary words with digits are not ids.
    assert normalise_message("sha256sum utf8mb4 HikariPool-1 x86_64") == "sha<N>sum utf<N>mb<N> HikariPool-<N> x<N>_<N>"


def test_json_lines_group_across_request_ids():
    lines = [SLOG_LINE.replace("oFXwmM7Ri6-000001", rid).replace("64609", port) for rid, port in [
        ("oFXwmM7Ri6-000001", "64609"),
        ("Qz8pLk2NvB-000002", "51123"),
        ("aB3dE5fG7h-000143", "60001"),
    ]]
    entries, unparsed = parse_log_text("\n".join(lines))
    groups = analyse_entries(entries)
    assert unparsed == 0
    assert [(g["service"], g["count"]) for g in groups] == [("middleware/logger", 3)]


def test_groups_cluster_by_service_and_normalised_message():
    entries, _ = parse_log_text(
        "2026-10-31 10:00:00 INFO [api] all good\n"
        "2026-10-31 10:00:01 ERROR [db] pool exhausted (active=20, waiting=31)\n"
        "2026-10-31 10:00:09 ERROR [db] pool exhausted (active=20, waiting=48)\n"
        "2026-10-31 10:00:04 CRITICAL [db] pool exhausted (active=20, waiting=90)\n"
        "2026-10-31 10:00:05 ERROR [api] pool exhausted (active=20, waiting=12)\n"
        "2026-10-31 10:00:06 WARN [api] cache miss ratio 41%\n"
    )
    groups = analyse_entries(entries)

    # INFO is ignored; db and api pool messages stay separate groups.
    assert [(g["id"], g["service"], g["count"]) for g in groups] == [("G1", "db", 3), ("G2", "api", 1), ("G3", "api", 1)]

    db = groups[0]
    assert db["level"] == "CRITICAL"  # worst level seen wins
    assert db["signature"] == "pool exhausted (active=<N>, waiting=<N>)"
    assert db["first_seen"] == "2026-10-31 10:00:01"
    assert db["last_seen"] == "2026-10-31 10:00:09"
    assert db["line_nos"] == [2, 3, 4]
    assert db["samples"][0] == {"line_no": 2, "text": "2026-10-31 10:00:01 ERROR [db] pool exhausted (active=20, waiting=31)"}


def test_get_groups_trims_for_model_and_needs_a_loaded_log():
    assert get_groups(FakeToolContext())["status"] == "error"

    ctx = FakeToolContext()
    read_logs(str(SAMPLE_LOGS / "payments_incident.log"), ctx)
    result = get_groups(ctx)
    assert result["status"] == "success"
    first = result["groups"][0]
    assert first["base_position"] == 1 and first["id"] == "G1"
    assert len(first["line_nos"]) <= 15
    assert first["line_nos_total"] == first["count"]


def test_grouping_is_deterministic():
    first, second = FakeToolContext(), FakeToolContext()
    read_logs(str(SAMPLE_LOGS / "payments_incident.log"), first)
    read_logs(str(SAMPLE_LOGS / "payments_incident.log"), second)
    assert get_groups(first) == get_groups(second)


def test_tool_cap_refuses_calls_past_the_limit():
    ctx = FakeToolContext()
    results = [enforce_tool_cap(None, {}, ctx) for _ in range(MAX_TOOL_CALLS + 1)]
    assert results[:-1] == [None] * MAX_TOOL_CALLS
    assert results[-1]["status"] == "error"


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
