"""Render an analysis result as a markdown incident report."""

from __future__ import annotations

from typing import Any


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(result: dict[str, Any]) -> str:
    summary = result["log_summary"]
    groups = {g["id"]: g for g in result["baseline"]}
    lines = [f"# Incident report: {result['source']}", ""]
    lines.append(
        f"Log covers {summary['first_timestamp']} to {summary['last_timestamp']}: "
        f"{summary['parsed_lines']} parsed lines, {summary['unparsed_lines']} unparsed, "
        f"{len(groups)} issue groups."
    )
    lines.append("")

    if result["status"] == "error":
        lines += ["## Analysis failed", "", f"**{result['error_kind']}**: {result['error']}", ""]
    if result["summary"]:
        lines += ["## Summary", "", result["summary"], ""]
    if root := result["root_cause_group_id"]:
        g = groups[root]
        lines += [f"**Root cause:** {root}, [{g['service']}] `{g['signature']}`", ""]

    if result["vigil_ranking"]:
        lines += ["## Ranking", "", "| Vigil | Baseline | Group | Service | Count | Moved because |", "|---|---|---|---|---|---|"]
        for row in result["vigil_ranking"]:
            g = groups[row["id"]]
            arrow = "" if row["delta"] == 0 else (f" ↑{row['delta']}" if row["delta"] > 0 else f" ↓{-row['delta']}")
            lines.append(
                f"| {row['position']}{arrow} | {row['old_position']} | {row['id']} | {_cell(g['service'])} "
                f"| {g['count']} | {_cell(row['reason'] or '')} |"
            )
        lines.append("")
    elif result["baseline"]:
        lines += ["## Baseline ranking", "", "| # | Group | Level | Service | Count | Score | Signature |", "|---|---|---|---|---|---|---|"]
        for g in result["baseline"]:
            lines.append(
                f"| {g['base_position']} | {g['id']} | {g['level']} | {_cell(g['service'])} | {g['count']} "
                f"| {g['score']} | `{_cell(g['signature'])}` |"
            )
        lines.append("")

    if result["findings"]:
        lines += ["## Findings", ""]
        for f in result["findings"]:
            g = groups.get(f["group_id"], {})
            flags = [f"**{f['severity'].upper()}**", f["verification"], f"confidence {f['confidence']:.2f}"]
            if f["needs_human_review"]:
                flags.append("**needs human review**")
            lines += [
                f"### {f['group_id']}: {f['title']}",
                "",
                f"{' · '.join(flags)}  ",
                f"Service `{g.get('service', '?')}`, {g.get('count', '?')} occurrences, "
                f"{g.get('first_seen', '?')} to {g.get('last_seen', '?')}",
                "",
                f"- **Why this severity:** {f['justification']}",
                f"- **Likely cause:** {f['likely_cause']}",
                f"- **Suggested fix:** {f['suggested_fix']}",
            ]
            if f["problems"]:
                lines.append(f"- **Evidence problems:** {'; '.join(f['problems'])}")
            lines += ["", "```"]
            for ev in f["evidence"]:
                lines.append(f"{ev['line_no']:>6}: {ev['text'] if ev['text'] is not None else '<no such line>'}")
            lines += ["```", ""]

    if result["warnings"]:
        lines += ["## Validator notes", ""] + [f"- {w}" for w in result["warnings"]] + [""]

    m = result["metrics"]
    lines += [
        "---",
        "",
        f"{'Model ' + m['model'] + ' · ' if m.get('model') else ''}"
        f"Latency {m['latency_ms']} ms · {m['llm_calls']} LLM calls · {m['tool_calls']} tool calls · "
        f"{m['attempts']} attempt(s) · tokens {m['tokens']['prompt']} in / {m['tokens']['output']} out "
        f"/ {m['tokens']['total']} total",
        "",
        "_vigil only reads logs and suggests fixes; nothing was executed or changed._",
    ]
    return "\n".join(lines) + "\n"
