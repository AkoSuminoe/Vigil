# vigil

A read-only log triage agent built on [Google ADK](https://adk.dev/) and Gemini.
Give it a server log and it will:

1. parse every line and group errors and warnings that are the same underlying issue,
2. rank the groups with a deterministic **baseline** (level weight × count × recency),
3. have the agent **re-rank** them with context (service dependencies, cascades, root
   cause vs symptom), giving a one-line reason for every group it moved,
4. give each group a severity, likely cause, suggested fix (text only), confidence and
   **cited log line numbers**,
5. **verify** every cited line in Python, flag what needs a human, and produce a markdown report.

vigil never runs commands or changes anything. In the CLI and `adk web` the only thing it
writes is the report under `reports/`; the web app writes nothing at all.

## How it works

```
log text ─► parse + group + baseline rank (Python, deterministic)
         ─► agent (Gemini via ADK Runner): get_log_summary, get_groups ─► JSON answer
         ─► validate against a Pydantic schema (1 retry) ─► verify cited lines
         ─► review flags + metrics + markdown report
```

| Piece | File | Notes |
| --- | --- | --- |
| Parsing and grouping | [vigil/tools.py](vigil/tools.py) | See [Supported log formats](#supported-log-formats). Numbers, UUIDs, IPs, emails, hex and request ids are normalised away, then lines are clustered by service + message. Each group keeps every line number it covers. |
| Baseline ranker | [vigil/ranking.py](vigil/ranking.py) | `score = weight(CRITICAL 10, ERROR 5, WARNING 1) × count × recency`. Recency is 0.5 to 1.0 depending on how close to the end of the log the issue was last seen. |
| Agent | [vigil/agent.py](vigil/agent.py) | Two modes. `root_agent` (interactive) is for `adk web`/`adk run`. Pipeline mode gets the log pre-loaded into session state: no tool reads a path or writes anything, and the answer must match the `AgentOutput` schema. |
| Output contract | [vigil/schema.py](vigil/schema.py) | The ranking must be a permutation of the groups; one finding per group; a move with a reason for every group that changed position. Wrong position numbers are corrected and reported as warnings. |
| Verifier | [vigil/verify.py](vigil/verify.py) | A cited line must exist and belong to the finding's group, otherwise the finding is marked `unverified` with reasons. It is never dropped. |
| Guardrails | [vigil/pipeline.py](vigil/pipeline.py) | One retry on invalid output, then a clear error. `RunConfig(max_llm_calls=10)`, a tool-call cap (`before_tool_callback`, 6 per turn, plus a hard stop at 8) and an overall timeout (`VIGIL_TIMEOUT_S`, default 120 s). |
| Escalation | [vigil/schema.py](vigil/schema.py) | Computed in Python: `needs_human_review = severity == "critical" or confidence < 0.6`. |
| Metrics | [vigil/pipeline.py](vigil/pipeline.py) | Latency, attempts, LLM calls, tool calls and prompt/output/total tokens are included in every result, success or error. |

Only the top 15 baseline groups are sent to the model; any others keep their baseline
position.

## Supported log formats

| Format | Example |
| --- | --- |
| Text with service | `2026-10-31 10:15:02 ERROR [payments] connection refused` |
| Text without service (service becomes `app`) | `2026-10-31 10:15:02 - ERROR: connection refused` or `2026-10-31 10:15:02 WARN disk 91% full` |
| JSON lines (slog, pino, structlog...) | `{"time":"2026-10-31T10:15:02.123+03:00","level":"error","msg":"query failed","component":"db","err":"timeout"}` |

Parsing details:
- **Timestamps:** a `T` separator, fractional seconds and a timezone suffix all work.
- **Level aliases:** `WARN`, `ERR`, `SEVERE`, `FATAL`, `CRIT`, `PANIC`, `ALERT`, `EMERG`, `NOTICE`, `TRACE`,
  and pino's numeric levels (10–60) are mapped to DEBUG / INFO / WARNING / ERROR / CRITICAL.
- **JSON fields:**
  - timestamp: `time`, `timestamp`, `ts` or `@timestamp` (ISO string or epoch s/ms)
  - level: `level`, `lvl`, `severity` or `levelname`
  - message: `msg`, `message` or `event`
  - service: `service`, `component`, `logger` or `app`
- **Fields left out:** only an `error`/`err` string is appended to the message. Other fields (request ids,
  client addresses, user agents) are left out on purpose: they would split every request into its own group
  and send personal data to the model.
- **Unmatched lines:** lines that match no format (stack trace frames, for example) are counted as unparsed.
  If nothing in the file parses, vigil reports an unsupported format instead of an all-clear.

## Setup

Requires Python 3.11+ and a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # put your key in GOOGLE_API_KEY
```

Save `.env` as UTF-8. On Windows PowerShell 5.1, `echo ... > .env` writes UTF-16, which
python-dotenv can't read.

If the app reports that the Gemini API **rejected** the key (`API_KEY_SERVICE_BLOCKED`), the
key isn't allowed to call the Generative Language API. Create one in AI Studio, or in Google
Cloud Console → Credentials, add *Generative Language API* to that key's API restrictions.

## Run

**Web app**

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8100
```

Open http://127.0.0.1:8100, pick a sample (or upload a `.log`/`.txt` up to 2 MB) and click
**Analyze**. The page shows:
- the baseline and Vigil rankings side by side, with the moves and their reasons,
- finding cards with the cited lines and verified / needs-review badges,
- a metrics strip and a report download.

API:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/samples` | List the sample logs. |
| `POST /api/analyze` | Multipart form with `file` (upload) **or** `sample` (a sample name). Returns the full result as JSON: 200 on success, 413 too large, 400 bad input, 429 rate limited, 502/503/504 when the model step fails (the body still includes the baseline). |
| `GET /healthz` | Liveness check. |

Uploads are read with a hard 2 MB cap and processed in memory; nothing is written to disk.
The API key stays on the server.

**CLI**

```bash
python -m vigil sample_logs/payments_incident.log          # prints the markdown report
python -m vigil sample_logs/payments_incident.log --save   # also saves reports/<timestamp>.md
python -m vigil some.log --json                            # full result as JSON
```

**ADK dev UI**

```bash
adk web              # http://127.0.0.1:8000, pick "vigil"; use --port 8765 if 8000 is taken
adk run vigil
```

Ask, for example: *Triage sample_logs/payments_incident.log*.

## Evals

[evals/](evals/) has three synthetic incidents, each with a known root cause in
`evals/answers/<name>.json`:

| Case | Root cause | Baseline #1 (a symptom) |
| --- | --- | --- |
| `dns_outage` | CoreDNS has no healthy upstreams | orders → inventory timeouts |
| `expired_internal_cert` | cert-manager failed to renew the internal mTLS cert | checkout TLS handshake errors |
| `redis_maxmemory` | Redis hit maxmemory (noeviction) | auth session write failures |

The logs are built so symptoms outnumber the cause: the baseline ranks the real root cause
6th in every case, so a pass means the re-rank found it.

```bash
python -m evals.run                     # all cases
python -m evals.run --case dns_outage   # one case
```

Output ends with, for example:

```
Root cause found: 3/3
Baseline #1 was the root cause: 0/3
```

The evals call the real model, so they need `GOOGLE_API_KEY`.

## Sample logs

[sample_logs/](sample_logs/) holds the demo logs. All three are fake: IPs come from
private or documentation ranges and the ids are random.

- `payments_incident.log`: a slow reconciliation query exhausts the payments DB pool, and
  orders and the API gateway fail in a cascade. Counting alone puts the gateway's 500s first;
  the agent should move the pool exhaustion and the slow query above them.
- `auth_service.log`: a Redis failover window, SMTP throttling, failed logins and an expiring cert.
- `batch_worker.log`: a disk filling up until jobs fail with ENOSPC, plus a recurring traceback.

Regenerate the sample and eval logs (seeded, so the output is identical) with
`python scripts/make_sample_logs.py`.

## Tests

```bash
pytest
```

The tests cover parsing, grouping, the baseline ranker, the output validator, the evidence
verifier, the pipeline guardrails (using a scripted fake agent) and the web API. They don't
need an API key.

## Deploy (pm2 + Caddy + Cloudflare Tunnel)

Run the app bound to localhost and let Caddy and the tunnel be the only way in:

```bash
# on the server, in the project directory, with .env in place (TRUST_PROXY=1)
pm2 start .venv/bin/uvicorn --name vigil --interpreter none -- \
  app.main:app --host 127.0.0.1 --port 8100 --workers 1
pm2 save
```

```caddyfile
vigil.example.com {
    reverse_proxy 127.0.0.1:8100
}
```

Point the Cloudflare Tunnel at Caddy. Notes:
- **Real client IP:** with `TRUST_PROXY=1`, the rate limiter keys on `CF-Connecting-IP`, then the
  first `X-Forwarded-For` entry. Without it, every request would look like `127.0.0.1` and share
  one limit.
- **Header spoofing:** keep uvicorn on `127.0.0.1` so those headers can't be forged by bypassing
  Cloudflare.
- **Single worker:** the rate limiter and concurrency cap live in memory, so run a single worker.
- **Tuning:** adjust `RATE_LIMIT`, `RATE_WINDOW_S` and `MAX_CONCURRENT_ANALYSES` in `.env`.

## Acknowledgements

Some parsing ideas adapted from [silvermete0r/logs_analyzer_script_py](https://github.com/silvermete0r/logs_analyzer_script_py).
