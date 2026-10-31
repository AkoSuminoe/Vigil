"""The JSON contract for the agent's answer, and the checks applied to it outside the model."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

Severity = Literal["critical", "high", "low"]

# Python, not the model, decides escalation.
REVIEW_CONFIDENCE_THRESHOLD = 0.6


class Finding(BaseModel):
    group_id: str = Field(description="Id of the group this finding is about, e.g. G3.")
    severity: Severity
    title: str = Field(description="Short name for the issue.")
    justification: str = Field(description="One line explaining the severity.")
    likely_cause: str
    suggested_fix: str = Field(description="Text only. vigil never applies fixes.")
    evidence_lines: list[int] = Field(
        min_length=1, description="Exact log line numbers, taken from the group's line_nos or samples."
    )
    confidence: float = Field(ge=0, le=1, description="0 to 1: how sure you are about the cause.")
    is_root_cause: bool


class RankMove(BaseModel):
    group_id: str
    old_position: int = Field(description="The group's base_position.")
    new_position: int = Field(description="1-based position in your ranking.")
    reason: str = Field(description="One line: why it moved.")


class AgentOutput(BaseModel):
    summary: str = Field(description="2-4 sentences: what broke, when, impact.")
    root_cause_group_id: str | None = Field(description="Group id of the root cause, or null if unclear.")
    ranking: list[str] = Field(description="Every group id you were given, most important first.")
    moves: list[RankMove] = Field(description="One entry for every group whose position differs from base_position.")
    findings: list[Finding] = Field(description="Exactly one finding per group.")


def needs_human_review(severity: str, confidence: float) -> bool:
    return severity == "critical" or confidence < REVIEW_CONFIDENCE_THRESHOLD


@dataclass
class Validation:
    output: AgentOutput | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.output is not None and not self.errors


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json(text: str) -> str:
    """Strip markdown fences and any chatter around the outermost JSON object."""
    stripped = _FENCE.sub("", text.strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start : end + 1] if start != -1 and end > start else stripped


def validate_output(text: str | None, groups: list[dict[str, Any]]) -> Validation:
    """Parse the agent's final text and check it against the groups it was given.

    Structural problems (bad JSON, schema errors, ranking not a permutation, missing
    findings or move reasons) are errors and trigger a retry. Wrong position numbers
    on an otherwise valid move are corrected here and reported as warnings.
    """
    if not text or not text.strip():
        return Validation(None, ["The answer was empty. Return the JSON object."])
    try:
        output = AgentOutput.model_validate(json.loads(extract_json(text)))
    except json.JSONDecodeError as exc:
        return Validation(None, [f"The answer is not valid JSON ({exc.msg} at char {exc.pos})."])
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:10]]
        return Validation(None, errors)

    errors: list[str] = []
    warnings: list[str] = []
    ids = [g["id"] for g in groups]
    base = {g["id"]: g["base_position"] for g in groups}

    missing = [i for i in ids if i not in output.ranking]
    unknown = [i for i in output.ranking if i not in base]
    duplicated = sorted({i for i in output.ranking if output.ranking.count(i) > 1})
    if missing:
        errors.append(f"ranking is missing groups: {', '.join(missing)}")
    if unknown:
        errors.append(f"ranking has unknown group ids: {', '.join(unknown)}")
    if duplicated:
        errors.append(f"ranking lists groups more than once: {', '.join(duplicated)}")

    finding_ids = [f.group_id for f in output.findings]
    if missing_findings := [i for i in ids if i not in finding_ids]:
        errors.append(f"no finding for groups: {', '.join(missing_findings)}")
    if unknown_findings := sorted({i for i in finding_ids if i not in base}):
        errors.append(f"findings for unknown group ids: {', '.join(unknown_findings)}")
    if duplicate_findings := sorted({i for i in finding_ids if finding_ids.count(i) > 1}):
        errors.append(f"more than one finding for: {', '.join(duplicate_findings)}")

    if output.root_cause_group_id is not None and output.root_cause_group_id not in base:
        errors.append(f"root_cause_group_id {output.root_cause_group_id} is not a known group id")

    if not errors:
        new_position = {gid: pos for pos, gid in enumerate(output.ranking, start=1)}
        moved = {gid for gid in ids if new_position[gid] != base[gid]}
        moves_by_id = {m.group_id: m for m in output.moves}
        missing_moves = sorted(moved - moves_by_id.keys(), key=new_position.get)
        # Groups the agent didn't explain are fine if they only shifted because explained
        # groups moved past them: their order relative to each other is unchanged.
        unexplained = [gid for gid in ids if gid not in moves_by_id]
        passive = [gid for gid in output.ranking if gid not in moves_by_id] == unexplained
        if missing_moves and not passive:
            errors.append(
                "moves is missing groups whose position changed: "
                + ", ".join(f"{g} ({base[g]} -> {new_position[g]})" for g in missing_moves)
            )
        elif missing_moves:
            for gid in missing_moves:
                direction = "down" if new_position[gid] > base[gid] else "up"
                moves_by_id[gid] = RankMove(
                    group_id=gid,
                    old_position=base[gid],
                    new_position=new_position[gid],
                    reason=f"(auto) Shifted {direction} by the moves above; relative order unchanged.",
                )
            output = output.model_copy(update={"moves": list(moves_by_id.values())})
            warnings.append(f"auto-filled reasons for passively shifted groups: {', '.join(missing_moves)}")
        kept_moves = []
        for move in output.moves:
            if move.group_id not in moved:
                warnings.append(f"ignored move for {move.group_id}: its position did not change")
                continue
            if not move.reason.strip():
                errors.append(f"move for {move.group_id} has no reason")
            expected = (base[move.group_id], new_position[move.group_id])
            if (move.old_position, move.new_position) != expected:
                warnings.append(
                    f"corrected positions for {move.group_id}: agent said "
                    f"{move.old_position} -> {move.new_position}, actual {expected[0]} -> {expected[1]}"
                )
                move = move.model_copy(update={"old_position": expected[0], "new_position": expected[1]})
            kept_moves.append(move)
        output = output.model_copy(update={"moves": sorted(kept_moves, key=lambda m: m.new_position)})

    return Validation(output if not errors else None, errors, warnings)
