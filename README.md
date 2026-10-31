# vigil

A read-only log triage agent built on [Google ADK](https://adk.dev/). Point it at a
server log and it will:

1. parse every line into `{timestamp, level, service, message}`,
2. group errors and warnings that are the same underlying issue,
3. rank each group as **critical / high / low** with a one-line justification,
4. suggest a likely cause and fix for each (as text only),
5. save a short markdown incident report to `reports/<timestamp>.md`.

vigil never runs commands or changes anything. The only file it writes is the report.

## How it works

Parsing and grouping are deterministic Python ([vigil/tools.py](vigil/tools.py)); the
LLM only does the reasoning and writing.

| Tool | What it does |
| --- | --- |
| `read_logs(path)` | Parses the log, keeps the entries in session state, returns a summary (counts per level and service, time range, unparsed lines). The raw log never goes to the model. |
| `group_errors()` | Normalises WARNING+ messages (strips numbers, UUIDs, IPs, hex and request ids) and clusters them by service + normalised message. Returns count, first/last seen and up to 3 sample lines per group. |
| `write_report(report_markdown)` | Saves the report to `reports/<timestamp>.md`. vigil chooses the file name, not the model. |

Supported line format (a `T` separator, fractional seconds, a timezone suffix and
`WARN`/`FATAL` aliases also work):

```
2026-10-31 10:15:02 ERROR [payments] message
```

Lines that don't match (stack trace frames, for example) are counted as unparsed.

## Setup

Requires Python 3.10+ and a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # then put your key in GOOGLE_API_KEY
```

`.env` settings:

| Variable | Purpose |
| --- | --- |
| `GOOGLE_GENAI_USE_VERTEXAI` | Keep `FALSE` to use the Gemini API rather than Vertex AI. |
| `GOOGLE_API_KEY` | Your Gemini API key. |
| `VIGIL_MODEL` | Gemini model name (default `gemini-flash-latest`). |

## Run

From the project root (the folder that contains `vigil/`):

```bash
adk web            # open http://127.0.0.1:8000 and pick "vigil"
adk web --port 8765 .   # if port 8000 is already taken
adk run vigil      # terminal chat instead of the browser UI
```

Then ask, for example:

> Triage sample_logs/payments_incident.log

Relative paths resolve against the project root. When the agent finishes, it replies
with the path of the report it saved under `reports/`.

## Sample logs

[sample_logs/](sample_logs/) holds three fake logs (no real personal data; IPs are
private or documentation ranges):

- `payments_incident.log`: a slow reconciliation query exhausts the payments DB
  connection pool, causing cascading 503s/500s in orders and the API gateway (the
  critical issue), plus unrelated broker blips and noise.
- `auth_service.log`: a Redis failover window, SMTP relay throttling, failed logins,
  rate limiting and an expiring TLS certificate.
- `batch_worker.log`: a disk filling up until jobs fail with ENOSPC, slow uploads and
  a recurring reporting exception with a traceback.

Regenerate them (seeded, so the output is identical) with `python scripts/make_sample_logs.py`.

## Tests

```bash
pytest
```

The tests cover parsing, normalisation, grouping, determinism and report saving. They
don't need an API key.
