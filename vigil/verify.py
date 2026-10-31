"""Check the evidence the agent cites against the actual log, after the run.

A finding is "verified" only if its group exists and every cited line exists and
belongs to that group. Anything else is "unverified" with the reasons listed.
Findings are never dropped.
"""

from __future__ import annotations

from typing import Any

from .schema import Finding


def verify_findings(
    findings: list[Finding], groups: list[dict[str, Any]], log_lines: list[str]
) -> list[dict[str, Any]]:
    """Return each finding as a dict with evidence text, verification status and problems."""
    groups_by_id = {g["id"]: g for g in groups}
    results = []
    for finding in findings:
        problems: list[str] = []
        group = groups_by_id.get(finding.group_id)
        if group is None:
            problems.append(f"{finding.group_id} is not a known group")
        members = set(group["line_nos"]) if group else set()

        evidence = []
        for line_no in finding.evidence_lines:
            exists = 1 <= line_no <= len(log_lines)
            in_group = line_no in members
            if not exists:
                problems.append(f"line {line_no} does not exist (log has {len(log_lines)} lines)")
            elif group is not None and not in_group:
                problems.append(f"line {line_no} is not part of {finding.group_id}")
            evidence.append(
                {
                    "line_no": line_no,
                    "text": log_lines[line_no - 1] if exists else None,
                    "matches_group": exists and in_group,
                }
            )
        if not finding.evidence_lines:
            problems.append("no evidence lines cited")

        results.append(
            {
                **finding.model_dump(),
                "evidence": evidence,
                "verification": "unverified" if problems else "verified",
                "problems": problems,
            }
        )
    return results
