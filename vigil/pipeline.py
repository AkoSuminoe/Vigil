"""Run vigil end to end: deterministic prep -> agent -> validation -> verification.

Guardrails live here, outside the agent: JSON schema validation with one retry,
caps on LLM calls and tool calls, an overall timeout, and a human-review rule
computed in Python. Every result, success or error, carries metrics.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable, Protocol

from .report import render_markdown
from .schema import needs_human_review, validate_output
from .tools import (
    MAX_GROUPS_FOR_MODEL,
    MAX_TOOL_CALLS,
    STATE_GROUPS,
    STATE_SUMMARY,
    SUPPORTED_FORMATS,
    analyse_entries,
    parse_log_text,
    summarise_entries,
)
from .verify import verify_findings

APP_NAME = "vigil"
USER_ID = "vigil-pipeline"
DEFAULT_TIMEOUT_S = float(os.getenv("VIGIL_TIMEOUT_S", "120"))
MAX_LLM_CALLS = 10
# Backstop on top of the before_tool_callback cap, in case the model keeps calling anyway.
HARD_TOOL_CALL_LIMIT = MAX_TOOL_CALLS + 2

FIRST_MESSAGE = (
    "Analyse the loaded log '{source}'. Call get_log_summary and get_groups, "
    "then answer with the JSON object only."
)
RETRY_MESSAGE = (
    "Your previous answer failed validation:\n{errors}\n"
    "Return the corrected JSON object only, covering every group id you were given."
)


class StepLimitError(RuntimeError):
    pass


class ModelError(RuntimeError):
    pass


@dataclass
class AgentTurn:
    text: str | None = None
    tool_calls: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class AgentSession(Protocol):
    async def send(self, message: str) -> AgentTurn: ...


SessionFactory = Callable[[dict[str, Any]], AgentSession]


class AdkSession:
    """One ADK session for one analysis; send() runs a single user turn."""

    def __init__(self, state: dict[str, Any]) -> None:
        # Imported here so the deterministic parts (and tests) don't need the agent wiring.
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        from .agent import build_agent

        self._state = state
        self._sessions = InMemorySessionService()
        self._runner = Runner(agent=build_agent("pipeline"), app_name=APP_NAME, session_service=self._sessions)
        self._session_id: str | None = None

    async def send(self, message: str) -> AgentTurn:
        from google.adk.agents.run_config import RunConfig
        from google.genai import types

        if self._session_id is None:
            session = await self._sessions.create_session(app_name=APP_NAME, user_id=USER_ID, state=self._state)
            self._session_id = session.id

        turn = AgentTurn()
        async for event in self._runner.run_async(
            user_id=USER_ID,
            session_id=self._session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
            run_config=RunConfig(max_llm_calls=MAX_LLM_CALLS),
        ):
            if event.error_code:
                raise ModelError(f"{event.error_code}: {event.error_message or 'no details'}")
            turn.tool_calls += len(event.get_function_calls())
            if turn.tool_calls > HARD_TOOL_CALL_LIMIT:
                raise StepLimitError(f"agent made more than {HARD_TOOL_CALL_LIMIT} tool calls")
            if usage := event.usage_metadata:
                turn.llm_calls += 1
                turn.prompt_tokens += usage.prompt_token_count or 0
                turn.output_tokens += usage.candidates_token_count or 0
                turn.total_tokens += usage.total_token_count or 0
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(p.text for p in event.content.parts if p.text and not p.thought)
                if text.strip():
                    turn.text = text
        return turn


def _sanitise(message: str) -> str:
    key = os.getenv("GOOGLE_API_KEY")
    if key:
        message = message.replace(key, "***")
    return message[:400]


def _baseline_rows(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "base_position", "score", "service", "level", "signature", "count", "first_seen", "last_seen")
    return [{k: g[k] for k in keys} for g in groups]


async def analyze(
    text: str,
    source: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    session_factory: SessionFactory | None = None,
) -> dict[str, Any]:
    """Analyse log text in memory. Never raises for model/validation problems: those come
    back as status "error" with error_kind and a readable message, alongside the baseline."""
    started = perf_counter()
    log_lines = text.splitlines()
    entries, unparsed = parse_log_text(text)
    summary = {"source": source, "total_lines": len(log_lines), **summarise_entries(entries, unparsed)}
    groups = analyse_entries(entries)
    sent = groups[:MAX_GROUPS_FOR_MODEL]

    metrics = {
        "latency_ms": 0,
        "attempts": 0,
        "llm_calls": 0,
        "tool_calls": 0,
        "tokens": {"prompt": 0, "output": 0, "total": 0},
    }
    result: dict[str, Any] = {
        "status": "ok",
        "error": None,
        "error_kind": None,
        "source": source,
        "log_summary": summary,
        "baseline": _baseline_rows(groups),
        "groups_sent_to_model": len(sent),
        "summary": None,
        "root_cause_group_id": None,
        "vigil_ranking": [],
        "findings": [],
        "warnings": [],
        "metrics": metrics,
        "report_markdown": "",
    }

    def finish() -> dict[str, Any]:
        metrics["latency_ms"] = round((perf_counter() - started) * 1000)
        result["report_markdown"] = render_markdown(result)
        return result

    def fail(kind: str, message: str) -> dict[str, Any]:
        result.update(status="error", error_kind=kind, error=message)
        return finish()

    if not entries and any(line.strip() for line in log_lines):
        # Zero parsed lines means "format not recognised", not "all clear".
        return fail("unsupported_format", f"No lines matched a supported log format. Expected {SUPPORTED_FORMATS}.")
    if not groups:
        result["summary"] = "No warnings or errors found in this log."
        return finish()

    if session_factory is None:
        if not os.getenv("GOOGLE_API_KEY"):
            return fail("config", "GOOGLE_API_KEY is not set on the server.")
        session_factory = AdkSession

    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    session = session_factory({STATE_SUMMARY: summary, STATE_GROUPS: groups})
    message = FIRST_MESSAGE.format(source=source)
    validation = None
    try:
        async with asyncio.timeout(timeout):
            for attempt in (1, 2):
                metrics["attempts"] = attempt
                turn = await session.send(message)
                metrics["llm_calls"] += turn.llm_calls
                metrics["tool_calls"] += turn.tool_calls
                metrics["tokens"]["prompt"] += turn.prompt_tokens
                metrics["tokens"]["output"] += turn.output_tokens
                metrics["tokens"]["total"] += turn.total_tokens
                validation = validate_output(turn.text, sent)
                if validation.ok:
                    break
                message = RETRY_MESSAGE.format(errors="\n".join(f"- {e}" for e in validation.errors))
    except TimeoutError:
        return fail("timeout", f"Analysis timed out after {timeout:.0f}s.")
    except (LlmCallsLimitExceededError, StepLimitError) as exc:
        return fail("step_limit", f"Agent hit its step limit: {_sanitise(str(exc))}")
    except Exception as exc:  # model/API errors: report, don't crash the caller
        detail = str(exc)
        if "API_KEY_SERVICE_BLOCKED" in detail or "API_KEY_INVALID" in detail:
            return fail(
                "config",
                "The Gemini API rejected GOOGLE_API_KEY (not allowed to call the Generative Language API). "
                "Create a key at https://aistudio.google.com/apikey, or allow 'Generative Language API' "
                "in the key's API restrictions.",
            )
        return fail("model", f"Model call failed: {type(exc).__name__}: {_sanitise(detail)}")

    if validation is None or not validation.ok:
        errors = validation.errors if validation else ["no answer"]
        return fail("invalid_output", "Agent output failed validation after one retry: " + "; ".join(errors[:5]))

    output = validation.output
    result["warnings"] = validation.warnings
    result["summary"] = output.summary
    result["root_cause_group_id"] = output.root_cause_group_id

    reasons = {m.group_id: m.reason for m in output.moves}
    base_position = {g["id"]: g["base_position"] for g in groups}
    order = output.ranking + [g["id"] for g in groups[MAX_GROUPS_FOR_MODEL:]]
    result["vigil_ranking"] = [
        {
            "id": gid,
            "position": position,
            "old_position": base_position[gid],
            "delta": base_position[gid] - position,
            "reason": reasons.get(gid),
        }
        for position, gid in enumerate(order, start=1)
    ]

    findings = verify_findings(output.findings, groups, log_lines)
    rank_of = {gid: i for i, gid in enumerate(order)}
    findings.sort(key=lambda f: rank_of.get(f["group_id"], len(order)))
    for finding in findings:
        finding["needs_human_review"] = needs_human_review(finding["severity"], finding["confidence"])
    result["findings"] = findings
    return finish()
