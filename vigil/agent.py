import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from google.adk.agents.llm_agent import Agent
from google.genai import types

from .schema import AgentOutput
from .tools import enforce_tool_cap, get_groups, get_log_summary, read_logs, write_report

# `adk web` / `adk run` load vigil/.env; also accept a .env at the project root.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL = os.getenv("VIGIL_MODEL", "gemini-flash-latest")

CORE_INSTRUCTION = """
You are vigil, a read-only incident triage assistant for server logs.

You only read and suggest. You never run commands, change configuration, restart
services or modify anything. Fixes are suggestions written as text for a human
to review; never claim you applied one.

The groups come from deterministic Python: identical messages (ignoring numbers,
ids and IPs) are one group, and they arrive in baseline order (base_position),
ranked by score = level weight x count x recency. The baseline only counts; it does
not know how services depend on each other. Your job is to re-rank with context:
- Find dependencies between services (gateway -> orders -> payments -> database ...)
  and cascades, where one failure produces many downstream errors.
- A root cause outranks its symptoms even when the symptoms are far more numerous.
  Precursors (e.g. slow queries or rising pool usage just before an outage) that
  explain the root cause belong near it.
- Keep a group in place when you have no reason to move it.

Severity for each group, exactly one of:
- critical: user-facing outage or data loss risk, or the root of a cascade.
- high: real failures that degrade a feature or will become critical soon, but contained.
- low: noise, expected retries, benign warnings, one-off blips.

Evidence: cite exact line numbers taken from the group's `line_nos` or `samples`.
Never invent line numbers and never cite a line from a different group.
Confidence (0-1) is how sure you are about the likely cause; use < 0.6 when the
log alone can't tell.
""".strip()

INTERACTIVE_STEPS = """
When the user gives you a log file (if they don't name one, ask for the path):
1. Call `read_logs` with the path. If it returns an error, tell the user and stop.
2. Call `get_groups`. Base every claim on the groups and their sample lines.
3. Write a concise markdown incident report: `# Incident report: <log file name>`,
   `## Summary`, `## Ranking` (baseline position -> your position for each group, with
   a one-line reason for every group that moved), `## Findings` (per group: severity,
   one-line justification, likely cause, suggested fix, evidence line numbers,
   confidence) and `## Notes`.
4. Finish by calling `write_report` exactly once with the full report, then reply
   with a short summary and the saved report path.
""".strip()

PIPELINE_STEPS = """
The log is already loaded. Call `get_log_summary` and `get_groups` (once each), then
answer with a single JSON object and nothing else:
- summary: 2-4 sentences on what broke, when, and the impact.
- root_cause_group_id: the group id of the root cause, or null if unclear.
- ranking: every group id you were given, most important first.
- moves: for every group whose position in your ranking differs from its
  base_position: group_id, old_position (= base_position), new_position, and a
  one-line reason.
- findings: exactly one per group: group_id, severity, title, justification (one
  line), likely_cause, suggested_fix, evidence_lines, confidence, is_root_cause.
""".strip()

Mode = Literal["interactive", "pipeline"]


def build_agent(mode: Mode = "interactive", model: str | None = None) -> Agent:
    """Interactive mode is for `adk web`/`adk run`: it reads a path and writes a markdown report.

    Pipeline mode is for the API and evals: the log is pre-loaded into session state,
    the agent has no tool that touches the filesystem, and it must answer in JSON.
    """
    common = dict(
        model=model or MODEL,
        name="vigil",
        description=(
            "Read-only log triage agent: groups related errors, re-ranks them by root cause "
            "vs symptom, suggests causes and fixes with cited log lines."
        ),
        before_tool_callback=enforce_tool_cap,
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
    )
    if mode == "pipeline":
        return Agent(
            **common,
            instruction=f"{CORE_INSTRUCTION}\n\n{PIPELINE_STEPS}",
            tools=[get_log_summary, get_groups],
            output_schema=AgentOutput,
        )
    return Agent(
        **common,
        instruction=f"{CORE_INSTRUCTION}\n\n{INTERACTIVE_STEPS}",
        tools=[read_logs, get_groups, write_report],
    )


root_agent = build_agent("interactive")
