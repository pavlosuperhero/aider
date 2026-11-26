"""Offline unit tests for scripts/local_context_trial/auditor_calibration.py."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest import mock

import pytest

TRIAL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "local_context_trial"
if str(TRIAL_DIR) not in sys.path:
    sys.path.insert(0, str(TRIAL_DIR))

import auditor_calibration as cal  # noqa: E402
from aider.caller_audit import _AUDITOR_SYSTEM_PROMPT  # noqa: E402

EXPECTED_CASE_IDS = (
    "neg_opaque_digest_consumer",
    "neg_coarse_status_check",
    "neg_declaration_prototype_only",
    "neg_unrelated_homonym_method",
    "neg_incomplete_truncated_evidence",
    "neg_no_evidence_empty",
    "pos_exact_unit_contract_pass",
    "pos_exact_tuple_order_pass",
    "fail_wrong_return_unit",
    "fail_swapped_tuple_unpack",
)

PROMPT_EXAMPLE_SYMBOLS = (
    "format_price",
    "yajl_parse_integer",
    "get_timeout",
    "Error::fmt",
    "Version::fmt",
    "parse_int",
    "deadline_ms",
)


def _match_case_for_messages(messages: list[dict]) -> cal.CalibrationCase:
    """Identify which CalibrationCase produced the given auditor prompt messages."""
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == _AUDITOR_SYSTEM_PROMPT
    user_content = messages[1]["content"]
    matches = [case for case in cal.CALIBRATION_CASES if case.task in user_content]
    assert len(matches) == 1, f"Expected unique task match, got {len(matches)}"
    return matches[0]


def _perfect_oracle_complete(messages: list[dict]) -> str:
    """Return a perfectly calibrated 4-step auditor response matching case ground truth."""
    case = _match_case_for_messages(messages)
    if case.expected_verdict == "PASS":
        verdict_line = "VERDICT: PASS"
    elif case.expected_verdict == "FAIL":
        verdict_line = f"VERDICT: FAIL: concrete mismatch in {case.case_id}"
    else:
        verdict_line = f"VERDICT: UNKNOWN: insufficient contract evidence in {case.case_id}"
    return (
        "1. CALLER_EXPECTS: Checked caller snippet.\n"
        "2. TRACE_REALISTIC: Traced realistic and edge inputs.\n"
        "3. MISMATCH_CHECK: Verified contract alignment.\n"
        f"{verdict_line}"
    )


def test_calibration_suite_structure_and_disjoint_from_prompt_examples():
    assert len(cal.CALIBRATION_CASES) == 10
    case_ids = tuple(case.case_id for case in cal.CALIBRATION_CASES)
    assert case_ids == EXPECTED_CASE_IDS
    assert len(set(case_ids)) == 10

    cases_by_id = {case.case_id: case for case in cal.CALIBRATION_CASES}
    incomplete_case = cases_by_id["neg_incomplete_truncated_evidence"]
    assert incomplete_case.deterministic_gate is True
    assert incomplete_case.expected_verdict == "UNKNOWN"
    assert incomplete_case.excerpts
    assert any(excerpt.incomplete for excerpt in incomplete_case.excerpts)

    empty_case = cases_by_id["neg_no_evidence_empty"]
    assert empty_case.deterministic_gate is True
    assert empty_case.expected_verdict == "UNKNOWN"
    assert empty_case.excerpts == ()

    # Ensure zero literal example symbols from _AUDITOR_SYSTEM_PROMPT leak into held-out cases.
    for case in cal.CALIBRATION_CASES:
        corpus_parts = [case.case_id, case.task, case.diff_text, case.description]
        for excerpt in case.excerpts:
            corpus_parts.extend([excerpt.path, excerpt.symbol, excerpt.code])
        corpus = "\n".join(corpus_parts)
        for forbidden in PROMPT_EXAMPLE_SYMBOLS:
            assert forbidden not in corpus, (
                f"Held-out case {case.case_id!r} reused prompt symbol {forbidden!r}"
            )


def test_cli_plan_executes_with_zero_llm_calls(capsys):
    with mock.patch.object(
        cal,
        "_build_model_adapter",
        side_effect=AssertionError("model adapter must not be called for --plan"),
    ):
        rc = cal.main(["--plan"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "plan"
    assert payload["total_cases"] == 10
    assert len(payload["cases"]) == 10
    assert payload["by_expected"] == {"UNKNOWN": 6, "PASS": 2, "FAIL": 2}
    assert payload["deterministic_gates"] == [
        "neg_incomplete_truncated_evidence",
        "neg_no_evidence_empty",
    ]
    assert len(payload["llm_cases"]) == 8


def test_cli_dry_run_verifies_deterministic_gates_with_zero_llm_calls(capsys, tmp_path):
    out_file = tmp_path / "reports" / "dry_run.json"
    with mock.patch.object(
        cal,
        "_build_model_adapter",
        side_effect=AssertionError("model adapter must not be called for --dry-run"),
    ):
        rc = cal.main(["--dry-run", "--out", str(out_file)])

    assert rc == 0
    stdout_payload = json.loads(capsys.readouterr().out)
    file_payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert stdout_payload == file_payload
    assert stdout_payload["mode"] == "dry_run"

    summary = stdout_payload["summary"]
    assert summary["total_cases"] == 2
    assert summary["total_audit_calls"] == 0
    assert summary["deterministic_gate_violations"] == 0
    assert summary["false_approvals"] == 0
    assert summary["safety_gate_passed"] is True

    rows = stdout_payload["rows"]
    assert [row["case_id"] for row in rows] == [
        "neg_incomplete_truncated_evidence",
        "neg_no_evidence_empty",
    ]
    assert all(row["actual_verdict"] == "UNKNOWN" for row in rows)
    assert all(row["audit_calls"] == 0 for row in rows)


def test_perfect_oracle_passes_safety_gate_with_zero_calls_on_deterministic_gates():
    rows = cal.run_calibration(
        cal.CALIBRATION_CASES,
        complete_fn=_perfect_oracle_complete,
        model_label="fake-oracle",
    )
    summary = cal.evaluate_calibration_results(rows)

    assert len(rows) == 10
    assert summary["safety_gate_passed"] is True
    assert summary["false_approvals"] == 0
    assert summary["false_abstentions"] == 0
    assert summary["deterministic_gate_violations"] == 0
    assert summary["pass_retention"] == 1.0
    assert summary["fail_recall"] == 1.0
    assert summary["accuracy"] == 1.0
    assert summary["total_audit_calls"] == 8

    for row in rows:
        if row.deterministic_gate:
            assert row.audit_calls == 0
            assert row.actual_verdict == "UNKNOWN"
        else:
            assert row.audit_calls == 1
            assert row.exact_match is True


@pytest.mark.parametrize(
    "corrupted_case_id",
    [
        case.case_id
        for case in cal.CALIBRATION_CASES
        if not case.deterministic_gate and case.expected_verdict in ("UNKNOWN", "FAIL")
    ],
)
def test_single_false_pass_fails_safety_gate(corrupted_case_id):
    def single_false_pass_complete(messages: list[dict]) -> str:
        case = _match_case_for_messages(messages)
        if case.case_id == corrupted_case_id:
            return "Traced.\nVERDICT: PASS"
        return _perfect_oracle_complete(messages)

    rows = cal.run_calibration(
        cal.CALIBRATION_CASES,
        complete_fn=single_false_pass_complete,
        model_label="fake-false-pass",
    )
    summary = cal.evaluate_calibration_results(rows)

    assert summary["false_approvals"] == 1
    assert summary["false_approval_ids"] == [corrupted_case_id]
    assert summary["deterministic_gate_violations"] == 0
    assert summary["safety_gate_passed"] is False


def test_over_abstaining_fake_fails_safety_gate():
    def always_unknown(_messages: list[dict]) -> str:
        return "Unsure.\nVERDICT: UNKNOWN: overly cautious"

    rows = cal.run_calibration(
        cal.CALIBRATION_CASES,
        complete_fn=always_unknown,
        model_label="fake-cautious",
    )
    summary = cal.evaluate_calibration_results(rows)

    assert summary["false_approvals"] == 0
    assert summary["false_abstentions"] == 4
    assert summary["pass_retention"] == 0.0
    assert summary["fail_recall"] == 0.0
    assert summary["deterministic_gate_violations"] == 0
    assert summary["safety_gate_passed"] is False


def test_evaluate_flags_deterministic_gate_violations_and_validates_thresholds():
    violating_row = cal.CalibrationRow(
        case_id="neg_no_evidence_empty",
        category="deterministic_no_evidence",
        expected_verdict="UNKNOWN",
        actual_verdict="UNKNOWN",
        reason="NO_EVIDENCE",
        audit_calls=1,
        response_tail="unexpected llm call",
        deterministic_gate=True,
        exact_match=True,
        false_approval=False,
        false_abstention=False,
    )
    summary = cal.evaluate_calibration_results([violating_row])
    assert summary["deterministic_gate_violations"] == 1
    assert summary["deterministic_gate_violation_ids"] == ["neg_no_evidence_empty"]
    assert summary["safety_gate_passed"] is False

    empty_summary = cal.evaluate_calibration_results([])
    assert empty_summary["total_cases"] == 0
    assert empty_summary["safety_gate_passed"] is False

    with pytest.raises(ValueError):
        cal.evaluate_calibration_results([], min_pass_retention=1.5)
    with pytest.raises(ValueError):
        cal.evaluate_calibration_results([], min_fail_recall=-0.1)


def test_cli_model_mode_records_effective_params_and_exit_codes(capsys, tmp_path):
    fake_params = {
        "model": "fake/model-v1",
        "temperature": 0.0,
        "max_tokens": 2048,
        "provider": "fake",
        "dropped_params": [],
    }
    out_pass = tmp_path / "live_pass.json"
    with mock.patch.object(
        cal,
        "_build_model_adapter",
        return_value=(_perfect_oracle_complete, fake_params),
    ):
        rc_pass = cal.main(["--model", "fake/model-v1", "--out", str(out_pass)])

    assert rc_pass == 0
    payload_pass = json.loads(capsys.readouterr().out)
    assert payload_pass == json.loads(out_pass.read_text(encoding="utf-8"))
    assert payload_pass["mode"] == "live"
    assert payload_pass["model"] == "fake/model-v1"
    assert payload_pass["effective_completion_params"] == fake_params
    assert payload_pass["summary"]["safety_gate_passed"] is True

    with mock.patch.object(
        cal,
        "_build_model_adapter",
        return_value=(lambda _msgs: "VERDICT: UNKNOWN: always abstain", fake_params),
    ):
        rc_fail = cal.main(["--model", "fake/model-v1"])

    assert rc_fail == 1
    payload_fail = json.loads(capsys.readouterr().out)
    assert payload_fail["summary"]["safety_gate_passed"] is False
