from helpers import LOG_LINES, groups

from vigil.schema import Finding
from vigil.verify import verify_findings


def make(group_id, lines):
    return Finding(
        group_id=group_id,
        severity="high",
        title="t",
        justification="j",
        likely_cause="c",
        suggested_fix="f",
        evidence_lines=lines,
        confidence=0.8,
        is_root_cause=False,
    )


def test_matching_lines_are_verified_with_their_text():
    [result] = verify_findings([make("G1", [3, 8])], groups(), LOG_LINES)
    assert result["verification"] == "verified"
    assert result["problems"] == []
    assert result["evidence"][0] == {"line_no": 3, "text": LOG_LINES[2], "matches_group": True}


def test_nonexistent_line_is_unverified():
    [result] = verify_findings([make("G1", [3, 99])], groups(), LOG_LINES)
    assert result["verification"] == "unverified"
    assert "line 99 does not exist" in result["problems"][0]
    assert result["evidence"][1]["text"] is None


def test_line_from_another_group_is_unverified():
    # Line 2 is the db pool line, not one of the api 500s.
    [result] = verify_findings([make("G1", [2])], groups(), LOG_LINES)
    assert result["verification"] == "unverified"
    assert result["problems"] == ["line 2 is not part of G1"]


def test_unparsed_line_exists_but_does_not_match():
    [result] = verify_findings([make("G1", [6])], groups(), LOG_LINES)
    assert result["verification"] == "unverified"
    assert result["evidence"][0]["text"].strip().startswith("at Handler")


def test_unknown_group_is_unverified():
    [result] = verify_findings([make("G42", [3])], groups(), LOG_LINES)
    assert result["verification"] == "unverified"
    assert "G42 is not a known group" in result["problems"]


def test_unverified_findings_are_kept_in_order():
    findings = [make("G1", [3]), make("G2", [3]), make("G3", [7])]
    results = verify_findings(findings, groups(), LOG_LINES)
    assert [(r["group_id"], r["verification"]) for r in results] == [
        ("G1", "verified"),
        ("G2", "unverified"),
        ("G3", "verified"),
    ]
