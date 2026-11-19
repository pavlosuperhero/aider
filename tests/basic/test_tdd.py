"""Pure policy tests for the optional TDD controller (aider/tdd.py).

Only plain data and fake callbacks are used: no Coder, no files, no subprocesses.
"""

from types import SimpleNamespace

import pytest

from aider.hooks import EditProposal, ResponseHookResult, TestRunObservation
from aider.tdd import (
    DEFAULT_DOC_GLOBS,
    DEFAULT_SOURCE_GLOBS,
    DEFAULT_TEST_CMD,
    DEFAULT_TEST_GLOBS,
    Phase,
    RunKind,
    StopReason,
    TddConfig,
    TddController,
    classify_pytest_run,
    supports_pytest,
)

TestRunObservation.__test__ = False

CMD = "python -m pytest -q"
TEST_FILE = "tests/test_calc.py"
SRC_FILE = "calc.py"

RED = (
    "F.                                                         [100%]\n"
    "=================================== FAILURES ===================================\n"
    "________________________________ test_add ______________________________________\n"
    "E       assert 3 == 4\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_calc.py::test_add - assert 3 == 4\n"
    "1 failed, 1 passed in 0.03s\n"
)
GREEN = "..   [100%]\n2 passed in 0.02s\n"
COLLECTION_ERROR = (
    "==================================== ERRORS ====================================\n"
    "ERROR collecting tests/test_calc.py\n"
    "E   ModuleNotFoundError: No module named 'calcc'\n"
    "=========================== short test summary info ============================\n"
    "ERROR tests/test_calc.py\n"
    "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
    "1 error in 0.04s\n"
)


def obs(output, status, command=CMD):
    return TestRunObservation.create(command, "/work", status, output)


def make_controller(**overrides):
    config = TddConfig(
        test_globs=("tests/*",),
        source_globs=("calc.py", "pkg/*"),
        **overrides,
    )
    return TddController(config)


def red_stage(controller):
    assert controller.check_edits(EditProposal((TEST_FILE,))) is None
    result = controller.observe_test(obs(RED, 1))
    assert isinstance(result, ResponseHookResult)
    return result


def green_stage(controller):
    red_stage(controller)
    assert controller.check_edits(EditProposal((SRC_FILE,))) is None
    return controller.observe_test(obs(GREEN, 0))


# ---- classification -------------------------------------------------------------------------


def test_intended_red_records_failed_test_identity_and_reason():
    result = classify_pytest_run(obs(RED, 1), edited_test_paths=(TEST_FILE,))
    assert result.kind is RunKind.INTENDED_RED
    assert [(f.node_id, f.reason) for f in result.failed] == [
        ("tests/test_calc.py::test_add", "assert 3 == 4")
    ]
    assert result.exit_status == 1
    assert result.counts["failed"] == 1


def test_passing_run_is_classified_passed():
    assert classify_pytest_run(obs(GREEN, 0), (TEST_FILE,)).kind is RunKind.PASSED


@pytest.mark.parametrize(
    "output, status",
    [
        (COLLECTION_ERROR, 2),
        ("no tests ran in 0.01s\n", 5),
        ("ERROR: usage: pytest [options]\n", 4),
        ("INTERNALERROR> boom\n", 3),
        ("", 124),
        ("1 skipped in 0.01s\n", 0),
        ("1 xfailed in 0.01s\n", 0),
        ("1 failed, 1 error in 0.01s\nFAILED tests/test_calc.py::t - assert 1\n", 1),
        ("collected 1 item\n", 1),
    ],
)
def test_unexpected_results_are_never_valid_red(output, status):
    result = classify_pytest_run(obs(output, status), (TEST_FILE,))
    assert result.kind is RunKind.UNEXPECTED
    assert result.detail


def test_failure_in_unedited_test_is_not_the_intended_red():
    result = classify_pytest_run(obs(RED, 1), edited_test_paths=("tests/test_other.py",))
    assert result.kind is RunKind.UNEXPECTED
    assert "tests/test_calc.py::test_add" in result.detail


@pytest.mark.parametrize(
    "line",
    [
        "FAILED tests/test_calc.py::test_add - NameError: name 'x' is not defined",
        "FAILED tests/test_calc.py::test_add - AttributeError: no attribute 'add'",
        "FAILED tests/test_calc.py::test_add",
    ],
)
def test_non_assertion_or_unexplained_failures_are_not_intended(line):
    output = line + "\n1 failed in 0.01s\n"
    result = classify_pytest_run(obs(output, 1), (TEST_FILE,))
    assert result.kind is RunKind.UNEXPECTED


@pytest.mark.parametrize(
    "reason",
    [
        "AssertionError: boom",
        "Failed: DID NOT RAISE <class 'ValueError'>",
        "TypeError: TddConfig.__init__() got an unexpected keyword argument 'require_citation'",
    ],
)
def test_assertion_style_reasons_are_intended(reason):
    output = f"FAILED tests/test_calc.py::test_add - {reason}\n1 failed in 0.01s\n"
    assert classify_pytest_run(obs(output, 1), (TEST_FILE,)).kind is RunKind.INTENDED_RED


def test_truncated_80_col_short_summary_recovers_full_assertion_from_traceback():
    truncated_output = (
        "F....................................................................... [ 67%]\n"
        "..................................                                       [100%]\n"
        "=================================== FAILURES ===================================\n"
        "_____________________ test_timeout_error_specific_details ______________________\n"
        "\n"
        "monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x105b92ad0>\n"
        "\n"
        "    def test_timeout_error_specific_details(monkeypatch):\n"
        '        error_message = "Oracle retrieval timed out."\n'
        '>       assert "read" in error_message.lower() or "timeout" in error_message.lower()\n'
        "E       AssertionError: assert ('read' in 'oracle retrieval timed out.' or "
        "'timeout' in 'oracle retrieval timed out.')\n"
        "E        +  where 'oracle retrieval timed out.' = <built-in method lower of str>()\n"
        "\n"
        "tests/basic/test_oracle.py:177: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/basic/test_oracle.py::test_timeout_error_specific_details - Asse...\n"
        "1 failed, 105 passed in 6.91s\n"
    )
    result = classify_pytest_run(
        obs(truncated_output, 1), edited_test_paths=("tests/basic/test_oracle.py",)
    )
    assert result.kind is RunKind.INTENDED_RED
    assert len(result.failed) == 1
    assert (
        result.failed[0].node_id
        == "tests/basic/test_oracle.py::test_timeout_error_specific_details"
    )
    assert result.failed[0].reason.startswith("AssertionError: assert ('read' in")


def test_long_node_id_without_short_summary_reason_recovers_reason_from_traceback():
    long_name = "test_timeout_error_specific_details_with_a_very_long_descriptive_function_name"
    assertion_output = (
        "=================================== FAILURES ===================================\n"
        f"_ {long_name} _\n"
        f"    def {long_name}():\n"
        ">       assert 1 == 2\n"
        "E       AssertionError: assert 1 == 2\n"
        f"tests/basic/test_oracle.py:190: AssertionError\n"
        "=========================== short test summary info ============================\n"
        f"FAILED tests/basic/test_oracle.py::{long_name}\n"
        "1 failed in 0.12s\n"
    )
    result = classify_pytest_run(
        obs(assertion_output, 1), edited_test_paths=("tests/basic/test_oracle.py",)
    )
    assert result.kind is RunKind.INTENDED_RED
    assert result.failed[0].reason == "AssertionError: assert 1 == 2"

    non_assertion_output = (
        "=================================== FAILURES ===================================\n"
        f"_ {long_name} _\n"
        f"    def {long_name}():\n"
        ">       return unknown_symbol\n"
        "E       NameError: name 'unknown_symbol' is not defined\n"
        f"tests/basic/test_oracle.py:190: NameError\n"
        "=========================== short test summary info ============================\n"
        f"FAILED tests/basic/test_oracle.py::{long_name}\n"
        "1 failed in 0.12s\n"
    )
    bad = classify_pytest_run(
        obs(non_assertion_output, 1), edited_test_paths=("tests/basic/test_oracle.py",)
    )
    assert bad.kind is RunKind.UNEXPECTED
    assert "NameError: name 'unknown_symbol' is not defined" in bad.detail


@pytest.mark.parametrize(
    "command, expected",
    [
        ("pytest -q", True),
        ("python -m pytest tests", True),
        (".venv/bin/python -m pytest -q tests/basic/test_x.py", True),
        ("/usr/bin/pytest", True),
        ("npm test", False),
        ("python -m unittest", False),
        ("make pytest-like", False),
        ("run_tests", False),
    ],
)
def test_supported_runner_contract_is_pytest_only(command, expected):
    assert supports_pytest(command) is expected


# ---- config ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(test_globs=(), source_globs=("calc.py",)),
        dict(test_globs=("tests/*",), source_globs=()),
        dict(test_globs=("",), source_globs=("calc.py",)),
        dict(test_globs="tests/*", source_globs=("calc.py",)),
        dict(test_globs=("tests/*",), source_globs=("calc.py",), max_cycles=0),
        dict(test_globs=("tests/*",), source_globs=("calc.py",), max_corrections=-1),
    ],
)
def test_config_rejects_unusable_scope_or_budgets(kwargs):
    with pytest.raises(ValueError):
        TddConfig(**kwargs)


def test_tdd_config_default_constants():
    config = TddConfig()
    assert config.test_globs == DEFAULT_TEST_GLOBS == ("tests/*",)
    assert (
        config.source_globs
        == DEFAULT_SOURCE_GLOBS
        == (
            "aider/*",
            "src/*",
            "lib/*",
            "app/*",
            "pkg/*",
        )
    )
    assert config.doc_globs == ()
    assert DEFAULT_DOC_GLOBS == ("docs/oracle.md",)
    assert DEFAULT_TEST_CMD == "pytest -q"


def test_scope_is_explicit_not_inferred_from_file_names():
    controller = make_controller()
    # Looks like a test by name but is not in a configured test glob.
    result = controller.check_edits(EditProposal(("test_helpers.py",)))
    assert isinstance(result, ResponseHookResult)
    assert "test_helpers.py" in result.continuation
    assert "NOT applied" in result.continuation
    assert controller.state.phase is Phase.TEST_ONLY_EDIT


def test_paths_outside_project_or_matching_both_scopes_are_vetoed():
    controller = TddController(TddConfig(test_globs=("tests/*",), source_globs=("tests/*", "a.py")))
    for path in ("tests/test_x.py", "../escape.py"):
        assert isinstance(controller.check_edits(EditProposal((path,))), ResponseHookResult)


# ---- edit vetoes ----------------------------------------------------------------------------


def test_initial_phase_and_test_only_edit_is_accepted():
    controller = make_controller()
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.check_edits(EditProposal((TEST_FILE,))) is None
    assert controller.state.tests_edited == (TEST_FILE,)


def test_production_edit_before_observed_red_is_vetoed_as_not_applied():
    controller = make_controller()
    result = controller.check_edits(EditProposal((SRC_FILE,)))
    assert "NOT applied" in result.continuation
    assert SRC_FILE in result.continuation
    assert "failing test" in result.continuation
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.state.corrections == 1


def test_combined_test_and_production_edit_is_vetoed_in_every_phase():
    controller = make_controller()
    proposal = EditProposal((TEST_FILE, SRC_FILE))
    result = controller.check_edits(proposal)
    assert "NOT applied" in result.continuation
    assert "separate" in result.continuation
    assert controller.state.tests_edited == ()
    red_stage(controller)
    assert isinstance(controller.check_edits(proposal), ResponseHookResult)
    assert controller.state.phase is Phase.RED_OBSERVED


def test_shell_commands_are_vetoed_before_red_even_without_edits():
    controller = make_controller()
    result = controller.check_edits(EditProposal((), ("sed -i s/a/b/ calc.py",)))
    assert "NOT applied" in result.continuation
    assert "sed -i" in result.continuation
    result = controller.check_edits(EditProposal((TEST_FILE,), ("touch calc.py",)))
    assert isinstance(result, ResponseHookResult)
    assert controller.state.tests_edited == ()


def test_empty_edit_proposal_is_vetoed_during_edit_phases_and_accepted_in_report():
    controller = make_controller()
    # In TEST_ONLY_EDIT: empty proposal is vetoed and charges 1 correction.
    result = controller.check_edits(EditProposal(()))
    assert isinstance(result, ResponseHookResult)
    assert "NOT applied" in result.continuation
    assert (
        "No file edits were found in your response during TDD phase test_only_edit"
        in result.continuation
    )
    assert controller.phase_guidance() in result.continuation
    assert controller.state.corrections == 1
    assert controller.state.phase is Phase.TEST_ONLY_EDIT

    # In RED_OBSERVED: empty proposal is also vetoed.
    red_stage(controller)
    result = controller.check_edits(EditProposal(()))
    assert isinstance(result, ResponseHookResult)
    assert (
        "No file edits were found in your response during TDD phase red_observed"
        in result.continuation
    )
    assert controller.state.corrections == 2
    assert controller.state.phase is Phase.RED_OBSERVED

    # Move to IMPLEMENT (failing test after source edit), empty proposal is vetoed.
    assert controller.check_edits(EditProposal((SRC_FILE,))) is None
    assert controller.observe_test(obs(RED, 1)) is not None
    assert controller.state.phase is Phase.IMPLEMENT
    result = controller.check_edits(EditProposal(()))
    assert isinstance(result, ResponseHookResult)
    assert (
        "No file edits were found in your response during TDD phase implement"
        in result.continuation
    )
    assert controller.state.phase is Phase.IMPLEMENT

    # In REPORT and DONE: empty proposal is accepted (final report has no file edits).
    assert controller.observe_test(obs(GREEN, 0)) is not None
    assert controller.state.phase is Phase.REPORT
    assert controller.check_edits(EditProposal(())) is None
    assert controller("final report") is None
    assert controller.state.phase is Phase.DONE
    assert controller.check_edits(EditProposal(())) is None


# ---- red / green flow -----------------------------------------------------------------------


def test_red_observation_continuation_is_literal_and_moves_to_red_observed():
    controller = make_controller()
    result = red_stage(controller)
    text = result.continuation
    assert CMD in text and "exit status 1" in text
    assert "tests/test_calc.py::test_add" in text and "assert 3 == 4" in text
    assert "production" in text
    state = controller.state
    assert state.phase is Phase.RED_OBSERVED
    assert state.red.kind is RunKind.INTENDED_RED
    assert state.green is None


def test_passing_test_during_red_expectation_is_not_valid_red():
    controller = make_controller()
    controller.check_edits(EditProposal((TEST_FILE,)))
    result = controller.observe_test(obs(GREEN, 0))
    assert "did not fail" in result.continuation
    assert "not valid red" in result.continuation
    assert CMD in result.continuation and "exit status 0" in result.continuation
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.state.red is None


def test_collection_error_is_reported_as_unexpected_not_red():
    controller = make_controller()
    controller.check_edits(EditProposal((TEST_FILE,)))
    result = controller.observe_test(obs(COLLECTION_ERROR, 2))
    assert "not a valid red" in result.continuation
    assert "exit status 2" in result.continuation
    assert "ModuleNotFoundError" in result.continuation
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.state.red is None


def test_production_edit_after_red_moves_to_implement_then_green_to_report():
    controller = make_controller()
    red_stage(controller)
    assert controller.check_edits(EditProposal((SRC_FILE,))) is None
    assert controller.state.phase is Phase.IMPLEMENT
    result = controller.observe_test(obs(GREEN, 0))
    assert controller.state.phase is Phase.REPORT
    assert controller.state.green.kind is RunKind.PASSED
    assert CMD in result.continuation and "exit status 0" in result.continuation
    assert "2 passed" in result.continuation
    assert "final report" in result.continuation


def test_failure_after_implementation_stays_in_implement_with_failed_ids():
    controller = make_controller()
    red_stage(controller)
    controller.check_edits(EditProposal((SRC_FILE,)))
    result = controller.observe_test(obs(RED, 1))
    assert controller.state.phase is Phase.IMPLEMENT
    assert "tests/test_calc.py::test_add" in result.continuation
    assert "exit status 1" in result.continuation


def test_report_response_completes_workflow():
    controller = make_controller()
    green_stage(controller)
    assert controller(" final report ", token_count=len, io=None) is None
    assert controller.state.phase is Phase.DONE


def test_response_outside_report_phase_never_changes_phase():
    controller = make_controller()
    assert controller("hello") is None
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    red_stage(controller)
    assert controller("still working") is None
    assert controller.state.phase is Phase.RED_OBSERVED


def test_manual_test_runs_outside_expectation_are_ignored():
    controller = make_controller()
    assert controller.observe_test(obs(GREEN, 0)) is None  # no test edit yet
    red_stage(controller)
    assert controller.observe_test(obs(GREEN, 0)) is None  # no implementation edit yet
    assert controller.state.phase is Phase.RED_OBSERVED


# ---- stale evidence -------------------------------------------------------------------------


def test_test_edit_after_red_invalidates_red_and_requires_new_cycle():
    controller = make_controller()
    red_stage(controller)
    assert controller.check_edits(EditProposal((TEST_FILE,))) is None
    state = controller.state
    assert state.phase is Phase.TEST_ONLY_EDIT
    assert state.red is None
    assert state.cycles == 2
    # Production edit is now blocked again until a fresh red is observed.
    assert isinstance(controller.check_edits(EditProposal((SRC_FILE,))), ResponseHookResult)


def test_test_edit_after_green_invalidates_green():
    controller = make_controller()
    green_stage(controller)
    controller.check_edits(EditProposal((TEST_FILE,)))
    assert controller.state.green is None
    assert controller.state.phase is Phase.TEST_ONLY_EDIT


def test_production_edit_after_green_invalidates_green_and_requires_recheck():
    controller = make_controller()
    green_stage(controller)
    assert controller.check_edits(EditProposal((SRC_FILE,))) is None
    assert controller.state.phase is Phase.REFACTOR_RECHECK
    assert controller.state.green is None
    assert controller.state.refactor_rechecks == 1
    result = controller.observe_test(obs(GREEN, 0))
    assert controller.state.phase is Phase.REPORT
    assert controller.state.green is not None
    assert "final report" in result.continuation


def test_edit_in_the_report_response_reopens_the_workflow_instead_of_done():
    controller = make_controller()
    green_stage(controller)
    controller("refactor and report")
    assert controller.state.phase is Phase.DONE
    assert controller.check_edits(EditProposal((SRC_FILE,))) is None
    assert controller.state.phase is Phase.REFACTOR_RECHECK


def test_edit_after_done_in_a_later_response_starts_a_new_cycle():
    controller = make_controller()
    green_stage(controller)
    controller("report")
    controller("next user task")
    assert isinstance(controller.check_edits(EditProposal((SRC_FILE,))), ResponseHookResult)
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.state.cycles == 2
    assert controller.check_edits(EditProposal((TEST_FILE,))) is None


# ---- budgets and stop reasons ---------------------------------------------------------------


def test_correction_budget_aborts_with_stop_reason_then_everything_is_vetoed():
    controller = make_controller(max_corrections=2)
    assert controller.check_edits(EditProposal((SRC_FILE,))).continuation
    assert controller.check_edits(EditProposal((SRC_FILE,))).continuation
    stop = controller.check_edits(EditProposal((SRC_FILE,)))
    assert stop == ResponseHookResult()  # stop, no further continuation
    state = controller.state
    assert state.phase is Phase.ABORTED
    assert state.stop_reason is StopReason.CORRECTION_BUDGET
    assert state.stop_detail
    assert controller.check_edits(EditProposal((TEST_FILE,))) == ResponseHookResult()
    assert controller.observe_test(obs(RED, 1)) is None


def test_cycle_budget_aborts_when_tests_keep_being_rewritten_after_red():
    controller = make_controller(max_cycles=2)
    red_stage(controller)
    controller.check_edits(EditProposal((TEST_FILE,)))  # cycle 2
    controller.observe_test(obs(RED, 1))
    stop = controller.check_edits(EditProposal((TEST_FILE,)))  # would be cycle 3
    assert stop == ResponseHookResult()
    assert controller.state.stop_reason is StopReason.CYCLE_BUDGET


def test_refactor_budget_aborts():
    controller = make_controller(max_refactor_rechecks=1)
    green_stage(controller)
    controller.check_edits(EditProposal((SRC_FILE,)))
    controller.observe_test(obs(GREEN, 0))
    stop = controller.check_edits(EditProposal((SRC_FILE,)))
    assert stop == ResponseHookResult()
    assert controller.state.stop_reason is StopReason.REFACTOR_BUDGET


def test_unsupported_runner_stops_with_clear_reason():
    controller = make_controller()
    controller.check_edits(EditProposal((TEST_FILE,)))
    io = SimpleNamespace(errors=[], tool_error=lambda text: io.errors.append(text))
    assert controller.observe_test(obs("ok", 0, command="npm test"), io=io) is None
    state = controller.state
    assert state.phase is Phase.ABORTED
    assert state.stop_reason is StopReason.UNSUPPORTED_RUNNER
    assert "pytest" in state.stop_detail and "npm test" in state.stop_detail
    assert io.errors and "pytest" in io.errors[0]


def test_snapshot_exposes_plain_data():
    controller = make_controller()
    red_stage(controller)
    snapshot = controller.snapshot()
    assert snapshot["phase"] == "red_observed"
    assert snapshot["stop_reason"] is None
    assert snapshot["red"]["failed_tests"] == ["tests/test_calc.py::test_add"]
    assert snapshot["budgets"]["max_cycles"] == controller.config.max_cycles


def test_hook_kwargs_register_all_three_callbacks_on_one_controller():
    controller = make_controller()
    hooks = controller.hook_kwargs()
    assert set(hooks) == {"response_hooks", "edit_hooks", "test_result_hooks"}
    assert hooks["response_hooks"] == (controller,)
    assert hooks["edit_hooks"] == (controller.check_edits,)
    assert hooks["test_result_hooks"] == (controller.observe_test,)
    assert not hasattr(controller, "prompt_reminder")


def test_phase_guidance_tracks_tdd_phase_and_doc_scope():
    controller = TddController(
        TddConfig(
            test_globs=("tests/*",),
            source_globs=("calc.py",),
            doc_globs=("docs/*",),
        )
    )
    assert "test_only_edit" in controller.phase_guidance()
    assert "tests/*" in controller.phase_guidance()
    assert "calc.py" in controller.phase_guidance()
    assert "docs/*" in controller.phase_guidance()

    red_stage(controller)
    assert (
        "red_observed" in controller.phase_guidance() or "implement" in controller.phase_guidance()
    )
    assert "calc.py" in controller.phase_guidance()
    assert "docs/*" in controller.phase_guidance()

    green_stage(controller)
    assert "report" in controller.phase_guidance()
    assert "docs/*" in controller.phase_guidance()


# ---- completion and grounding (Milestone 3) -------------------------------------------------


def test_green_continuation_shows_before_and_after_commands_outcomes_and_visible_checklist():
    controller = TddController(
        TddConfig(
            test_globs=("tests/*",),
            source_globs=("calc.py",),
            doc_globs=("docs/*",),
        )
    )
    red_stage(controller)
    assert controller.check_edits(EditProposal((SRC_FILE,))) is None
    result = controller.observe_test(obs(GREEN, 0))
    text = result.continuation
    assert "Before (observed red):" in text
    assert f"`{CMD}` finished with exit status 1 (1 failed, 1 passed)" in text
    assert "tests/test_calc.py::test_add: assert 3 == 4" in text
    assert "After (observed green):" in text
    assert f"`{CMD}` finished with exit status 0 (2 passed)" in text
    assert "regression coverage" in text.lower()
    assert "not satisfied by an old suite passing" in text
    assert "docs/*" in text
    assert "changed docs file alone is not proof its claims are correct" in text
    assert "Next action: required documentation (docs/*) is still UNMET. Edit docs/* now" in text

    assert controller.check_edits(EditProposal(("docs/guide.md",))) is None
    after_docs = controller.observe_test(obs(GREEN, 0)).continuation
    assert "Otherwise reply without edits with the final report" in after_docs


def test_early_completion_before_red_or_green_reports_unmet_requirements():
    controller = make_controller()
    early = controller("I am done; everything already works.")
    assert isinstance(early, ResponseHookResult)
    assert "NOT complete" in early.continuation
    assert "Unmet requirements" in early.continuation
    assert "observed red" in early.continuation.lower()
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.state.corrections == 1

    red_stage(controller)
    early_after_red = controller("All done now.")
    assert isinstance(early_after_red, ResponseHookResult)
    assert "Unmet requirements" in early_after_red.continuation
    assert "observed green" in early_after_red.continuation.lower()
    assert controller.state.phase is Phase.RED_OBSERVED


def test_oracle_search_envelope_is_not_treated_as_early_completion():
    controller = make_controller()
    envelope = '<oracle_search>{"query":"q","source":"s"}</oracle_search>'
    assert controller(envelope) is None
    assert controller.state.corrections == 0
    assert controller.state.phase is Phase.TEST_ONLY_EDIT


def test_optional_doc_glob_allows_doc_edit_and_rejects_report_when_docs_untouched():
    controller = TddController(
        TddConfig(
            test_globs=("tests/*",),
            source_globs=("calc.py",),
            doc_globs=("docs/*",),
        )
    )
    green_stage(controller)
    # Report while docs/* has never been touched -> unmet documentation requirement.
    unmet = controller("Final report without touching docs.")
    assert isinstance(unmet, ResponseHookResult)
    assert "Unmet requirements" in unmet.continuation
    assert "docs/*" in unmet.continuation
    assert "changed docs file alone is not proof its claims are correct" in unmet.continuation
    assert controller.state.phase is Phase.REPORT

    # Now edit docs/oracle.md in REPORT phase; allowed and marks docs_edited.
    assert controller.check_edits(EditProposal(("docs/oracle.md",))) is None
    assert controller.state.docs_edited == ("docs/oracle.md",)
    assert controller.observe_test(obs(GREEN, 0)) is not None
    assert controller.state.phase is Phase.REPORT
    assert controller("Final report after doc update.") is None
    assert controller.state.phase is Phase.DONE


def test_report_rejects_unknown_cited_passage_ids_and_notes_identity_vs_semantic_support():
    known_id = "4d4d418905639aeba3a03277"
    unknown_id = "deadbeefdeadbeefdeadbeef"
    validator = lambda text: ("deadbeefdeadbeefdeadbeef",) if unknown_id in text else ()
    controller = TddController(
        TddConfig(test_globs=("tests/*",), source_globs=("calc.py",)),
        citation_validator=validator,
    )
    green_stage(controller)
    rejected = controller(f"Final report citing {known_id} and fabricated {unknown_id}.")
    assert isinstance(rejected, ResponseHookResult)
    assert unknown_id in rejected.continuation
    assert "never returned" in rejected.continuation
    assert "valid passage identity does not prove semantic support" in rejected.continuation
    assert controller.state.phase is Phase.REPORT

    accepted = controller(f"Final report citing only {known_id}.")
    assert accepted is None
    assert controller.state.phase is Phase.DONE


def test_require_citation_flag_requires_final_report_to_contain_at_least_one_24hex_id():
    controller = TddController(
        TddConfig(
            test_globs=("tests/*",),
            source_globs=("calc.py",),
            require_citation=True,
        ),
        citation_validator=lambda text: (),
    )

    green_stage(controller)

    unmet = controller("Final report with no citation.")
    assert isinstance(unmet, ResponseHookResult)
    assert (
        "final report must cite at least one returned 24-hex Oracle passage ID"
        in unmet.continuation
    )
    assert controller.state.phase is Phase.REPORT

    accepted = controller("Final report citing passage id 4d4d418905639aeba3a03277.")
    assert accepted is None
    assert controller.state.phase is Phase.DONE


def test_fenced_edit_in_report_phase_does_not_move_to_done_prematurely():
    controller = TddController(
        TddConfig(
            test_globs=("tests/*",),
            source_globs=("calc.py",),
            doc_globs=("docs/*",),
        )
    )
    green_stage(controller)
    assert controller.state.phase is Phase.REPORT

    # Model responds with a fenced edit block in REPORT phase
    fenced_edit = "docs/oracle.md\n```markdown\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n```"
    assert controller(fenced_edit) is None
    assert controller.state.phase is Phase.REPORT
