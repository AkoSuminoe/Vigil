import json

from helpers import answer, finding, groups

from vigil.schema import needs_human_review, validate_output


def test_fixture_baseline_is_symptom_first():
    ranked = groups()
    assert [(g["id"], g["service"], g["level"]) for g in ranked] == [
        ("G1", "api", "ERROR"),
        ("G2", "db", "CRITICAL"),
        ("G3", "api", "WARNING"),
    ]


def test_valid_answer_passes():
    result = validate_output(answer(), groups())
    assert result.ok, result.errors
    assert [m.group_id for m in result.output.moves] == ["G2", "G1"]
    assert result.warnings == []


def test_fenced_json_with_chatter_is_accepted():
    result = validate_output("Here you go:\n```json\n" + answer() + "\n```", groups())
    assert result.ok, result.errors


def test_not_json_and_empty_are_errors():
    assert validate_output("the db is broken", groups()).errors
    assert validate_output("", groups()).errors
    assert validate_output(None, groups()).errors


def test_schema_violations_are_reported():
    bad = json.loads(answer())
    bad["findings"][0]["confidence"] = 1.5
    bad["findings"][1]["severity"] = "urgent"
    result = validate_output(json.dumps(bad), groups())
    assert not result.ok
    assert any("confidence" in e for e in result.errors)
    assert any("severity" in e for e in result.errors)


def test_ranking_must_be_a_permutation():
    result = validate_output(answer(ranking=["G2", "G2", "G9"]), groups())
    joined = " ".join(result.errors)
    assert "missing groups: G1, G3" in joined
    assert "unknown group ids: G9" in joined
    assert "more than once: G2" in joined


def test_every_group_needs_one_finding():
    result = validate_output(answer(findings=[finding("G2", "critical", [2], 0.9, True)]), groups())
    assert any("no finding for groups: G1, G3" in e for e in result.errors)


def test_every_moved_group_needs_a_move_with_reason():
    result = validate_output(answer(moves=[]), groups())
    assert any("G2 (2 -> 1)" in e and "G1 (1 -> 2)" in e for e in result.errors)


def test_wrong_positions_are_corrected_with_a_warning():
    data = json.loads(answer())
    data["moves"][0]["old_position"] = 5
    data["moves"].append({"group_id": "G3", "old_position": 3, "new_position": 3, "reason": "no change"})
    result = validate_output(json.dumps(data), groups())
    assert result.ok
    g2 = next(m for m in result.output.moves if m.group_id == "G2")
    assert (g2.old_position, g2.new_position) == (2, 1)
    assert any("corrected positions for G2" in w for w in result.warnings)
    assert any("ignored move for G3" in w for w in result.warnings)


def test_human_review_rule():
    assert needs_human_review("critical", 0.95)
    assert needs_human_review("low", 0.59)
    assert not needs_human_review("high", 0.6)
