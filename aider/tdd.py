"""Optional TDD workflow controller, driven by generic hooks from aider/hooks.py.

This module is policy only. It sees plain data (EditProposal, TestRunObservation,
response text) and returns ResponseHookResult objects. It imports nothing from the
Coder, Commands or IO layers; the core never imports it except where main composes it.

Phases (TddState.phase)::

    TEST_ONLY_EDIT -> RED_OBSERVED -> IMPLEMENT -> REPORT -> DONE
                          (REPORT -> REFACTOR_RECHECK -> REPORT)      ABORTED from anywhere

- TEST_ONLY_EDIT: only test files may change. After a test edit the next test run must
  be an intended red.
- RED_OBSERVED: an intended red was observed; production edits are allowed.
- IMPLEMENT: a production edit was accepted; the next test run must pass.
- REPORT: a green run was observed after the last edit; the model is asked for a final
  report (or a refactor). DONE: a response arrived in REPORT without edits.
- REFACTOR_RECHECK: a production edit after green; green must be re-observed.
- ABORTED: a budget ran out or the runner is unsupported (state.stop_reason says why).

Contract and limits: the only supported runner is pytest. Red/green evidence comes from
the exit status and output of the one configured test command (focused and broader
checks are not distinguished). Edits are judged by explicit path globs, never by file
name guessing. Nothing here judges whether a test is meaningful or the code correct.
"""

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum

from aider.hooks import EditProposal, ResponseHookResult, TestRunObservation


class Phase(str, Enum):
    TEST_ONLY_EDIT = "test_only_edit"
    RED_OBSERVED = "red_observed"
    IMPLEMENT = "implement"
    REFACTOR_RECHECK = "refactor_recheck"
    REPORT = "report"
    DONE = "done"
    ABORTED = "aborted"


class StopReason(str, Enum):
    CORRECTION_BUDGET = "correction_budget"
    CYCLE_BUDGET = "cycle_budget"
    REFACTOR_BUDGET = "refactor_budget"
    UNSUPPORTED_RUNNER = "unsupported_runner"


class RunKind(str, Enum):
    INTENDED_RED = "intended_red"
    PASSED = "passed"
    UNEXPECTED = "unexpected"


def _globs(name, value, *, allow_empty=False):
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        raise ValueError(f"TDD {name} must be a list of glob patterns.")
    value = tuple(value)
    if (not value and not allow_empty) or any(
        not isinstance(g, str) or not g.strip() for g in value
    ):
        raise ValueError(f"TDD needs at least one nonempty {name} glob.")
    return value


@dataclass(frozen=True)
class TddConfig:
    """Explicit scope (fnmatch globs on project-relative POSIX paths) and budgets.

    fnmatch semantics: * also matches /, so "tests/*" covers nested test files.
    """

    test_globs: tuple[str, ...]
    source_globs: tuple[str, ...]
    doc_globs: tuple[str, ...] = ()
    max_cycles: int = 3  # red cycles, including the first
    max_corrections: int = 6  # rejected edits, invalid reds and failing runs after a change
    max_refactor_rechecks: int = 2

    def __post_init__(self):
        object.__setattr__(self, "test_globs", _globs("test", self.test_globs))
        object.__setattr__(self, "source_globs", _globs("source", self.source_globs))
        object.__setattr__(self, "doc_globs", _globs("doc", self.doc_globs, allow_empty=True))
        for name, minimum in (
            ("max_cycles", 1),
            ("max_corrections", 0),
            ("max_refactor_rechecks", 0),
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"TDD {name} must be an integer of at least {minimum}.")

    @classmethod
    def from_args(cls, args, edit_format=None):
        """Build the config from parsed CLI/YAML arguments, refusing unusable setups."""
        test_globs = tuple(getattr(args, "tdd_test_glob", None) or ())
        source_globs = tuple(getattr(args, "tdd_source_glob", None) or ())
        doc_globs = tuple(getattr(args, "tdd_doc_glob", None) or ())
        if not test_globs:
            raise ValueError("--tdd requires at least one --tdd-test-glob.")
        if not source_globs:
            raise ValueError("--tdd requires at least one --tdd-source-glob.")
        test_cmd = args.test_cmd
        if not test_cmd:
            raise ValueError("--tdd requires --test-cmd.")
        if not args.auto_test:
            raise ValueError("--tdd requires --auto-test so that every edit is followed by a run.")
        if not isinstance(test_cmd, str) or not supports_pytest(test_cmd):
            raise ValueError("--tdd supports only a pytest --test-cmd (for example 'pytest -q').")
        if edit_format == "architect":
            raise ValueError("--tdd does not support the architect edit format.")
        return cls(test_globs=test_globs, source_globs=source_globs, doc_globs=doc_globs)


@dataclass(frozen=True)
class FailedTest:
    node_id: str
    reason: str

    @property
    def assertion_like(self):
        reason = self.reason.strip()
        return reason.startswith(("assert", "AssertionError", "Failed: DID NOT RAISE"))


@dataclass(frozen=True)
class RunClassification:
    kind: RunKind
    detail: str
    command: str
    exit_status: int
    counts: dict = field(default_factory=dict)
    failed: tuple[FailedTest, ...] = ()
    excerpt: str = ""

    @property
    def summary_text(self):
        return ", ".join(f"{n} {name}" for name, n in self.counts.items()) or "no summary"


@dataclass
class TddState:
    phase: Phase = Phase.TEST_ONLY_EDIT
    cycles: int = 1
    corrections: int = 0
    refactor_rechecks: int = 0
    tests_edited: tuple[str, ...] = ()
    docs_edited: tuple[str, ...] = ()
    red: RunClassification | None = None
    green: RunClassification | None = None
    stop_reason: StopReason | None = None
    stop_detail: str = ""
    history: list = field(default_factory=list)  # (from_phase, to_phase, why)


# ---- pytest parsing -------------------------------------------------------------------------

_COUNT_WORDS = {"errors": "error", "warnings": "warning"}
_SUMMARY = re.compile(r"^(no tests ran|\d+ \w+(?:, \d+ \w+)*) in [\d.]+s(?: \(\d+:\d+:\d+\))?$")
_FAILED_LINE = re.compile(r"^FAILED (.+?)(?: - (.*))?$")


def supports_pytest(command):
    """True if the command is recognisably a pytest invocation (the only supported runner)."""
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return any(os.path.basename(t) in ("pytest", "py.test") for t in tokens)


def parse_pytest_summary(output):
    """Counts from pytest's final summary line, {} for 'no tests ran', None if absent."""
    for line in reversed(output.splitlines()[-40:]):
        match = _SUMMARY.match(line.strip().strip("=").strip())
        if not match:
            continue
        counts = {}
        for number, word in re.findall(r"(\d+) (\w+)", match.group(1)):
            counts[_COUNT_WORDS.get(word, word)] = int(number)
        return counts
    return None


def parse_failed_tests(output):
    failed = []
    for line in output.splitlines():
        match = _FAILED_LINE.match(line.strip())
        if match:
            failed.append(FailedTest(match.group(1).strip(), (match.group(2) or "").strip()))
    return tuple(failed)


def _excerpt(output, limit=6):
    lines = [
        line.strip()[:200]
        for line in output.splitlines()
        if line.startswith("E ") or "Error" in line or "ERROR" in line
    ]
    return "\n".join(lines[:limit])


def classify_pytest_run(observation, edited_test_paths=()):
    """Classify one pytest run as intended red, passed or unexpected (never a valid red)."""
    output = observation.output
    status = observation.exit_status
    summary = parse_pytest_summary(output)
    counts = summary or {}
    failed = parse_failed_tests(output)

    def result(kind, detail, failures=()):
        return RunClassification(
            kind,
            detail,
            observation.command,
            status,
            counts,
            tuple(failures),
            _excerpt(output) if kind is RunKind.UNEXPECTED else "",
        )

    def unexpected(detail):
        return result(RunKind.UNEXPECTED, detail, failed)

    if status == 0:
        if counts.get("passed", 0) > 0:
            return result(RunKind.PASSED, "pytest passed.")
        return unexpected("pytest exited 0 but no test passed (skipped or deselected only).")
    if status != 1:
        reasons = {
            2: "test execution was interrupted or a collection/import error occurred",
            3: "pytest hit an internal error",
            4: "pytest was invoked incorrectly (usage error)",
            5: "no tests were collected",
        }
        reason = reasons.get(status, "abnormal exit (crash, signal or timeout)")
        return unexpected(f"pytest exit status {status}: {reason}.")
    if summary is None:
        return unexpected("no pytest summary line was found, so failures cannot be told apart.")
    if counts.get("error"):
        return unexpected(
            f"pytest reported {counts['error']} error(s); errors are not test failures."
        )
    if not counts.get("failed"):
        return unexpected("exit status 1 without any failed test.")
    if not failed:
        return unexpected("no 'FAILED <test id> - <reason>' lines found; run pytest with -rf.")
    non_assertion = [f for f in failed if not f.assertion_like]
    if non_assertion:
        listing = "; ".join(f"{f.node_id} ({f.reason or 'no reason shown'})" for f in non_assertion)
        return unexpected(f"failures that are not assertion failures: {listing}.")
    edited = {os.path.normpath(p).replace(os.sep, "/") for p in edited_test_paths}
    if not any(f.node_id.split("::")[0] in edited for f in failed):
        ids = ", ".join(f.node_id for f in failed)
        return unexpected(
            f"failing tests ({ids}) are not in the test files you edited "
            f"({', '.join(sorted(edited)) or 'none'}); they may fail for another reason."
        )
    return result(RunKind.INTENDED_RED, "assertion failure(s) in edited test files.", failed)


# ---- continuation text ----------------------------------------------------------------------

NOT_APPLIED = (
    "Your previous response was rejected before execution. NOT applied: none of its edits or "
    "shell commands were applied, and the project files are unchanged by that response; use "
    "the supplied file contents, not the rejected patch, as the current state. "
)


_EARLY_COMPLETION_RE = re.compile(
    r"\b(?:i am done|i'm done|all done|everything already works|final report)\b",
    re.IGNORECASE,
)


def _failed_listing(failed):
    return "\n".join(f"- {f.node_id}: {f.reason or 'no reason shown'}" for f in failed)


def _run_line(run):
    return f"`{run.command}` finished with exit status {run.exit_status} ({run.summary_text})."


class TddController:
    """State machine over generic hooks; one instance serves one session.

    Registered as three hooks (see hook_kwargs): the instance itself as a response hook
    (only to notice the final report), check_edits as an edit hook and observe_test as a
    test-result hook. It deliberately has no prompt_reminder, so Aider's edit-format
    reminder is never replaced; guidance is carried by the literal continuations.
    """

    def __init__(self, config, *, citation_validator=None):
        self.config = config
        self.citation_validator = citation_validator
        self.state = TddState()
        self._serial = 0
        self._done_serial = None

    def hook_kwargs(self):
        return {
            "response_hooks": (self,),
            "edit_hooks": (self.check_edits,),
            "test_result_hooks": (self.observe_test,),
        }

    # -- plain-data view -----------------------------------------------------------------

    def snapshot(self):
        s = self.state

        def run(r):
            return r and {
                "kind": r.kind.value,
                "command": r.command,
                "exit_status": r.exit_status,
                "counts": dict(r.counts),
                "failed_tests": [f.node_id for f in r.failed],
            }

        return {
            "phase": s.phase.value,
            "stop_reason": s.stop_reason.value if s.stop_reason else None,
            "stop_detail": s.stop_detail,
            "cycles": s.cycles,
            "corrections": s.corrections,
            "refactor_rechecks": s.refactor_rechecks,
            "tests_edited": list(s.tests_edited),
            "docs_edited": list(s.docs_edited),
            "red": run(s.red),
            "green": run(s.green),
            "budgets": {
                "max_cycles": self.config.max_cycles,
                "max_corrections": self.config.max_corrections,
                "max_refactor_rechecks": self.config.max_refactor_rechecks,
            },
        }

    # -- transitions ---------------------------------------------------------------------

    def _move(self, phase, why):
        self.state.history.append((self.state.phase, phase, why))
        self.state.phase = phase

    def _abort(self, reason, detail, io=None):
        s = self.state
        s.stop_reason, s.stop_detail = reason, detail
        self._move(Phase.ABORTED, reason.value)
        self._report_stop(io)

    def _report_stop(self, io):
        if io is not None:
            io.tool_error(
                f"TDD workflow stopped ({self.state.stop_reason.value}): "
                f"{self.state.stop_detail}"
            )

    def _charge(self, io=None):
        """Count one correction; True if the budget is exhausted (workflow aborted)."""
        s = self.state
        s.corrections += 1
        if s.corrections > self.config.max_corrections:
            self._abort(
                StopReason.CORRECTION_BUDGET,
                f"more than {self.config.max_corrections} rejected edits or invalid runs.",
                io,
            )
            return True
        return False

    def _new_cycle(self, why, io=None):
        s = self.state
        if s.cycles + 1 > self.config.max_cycles:
            self._abort(
                StopReason.CYCLE_BUDGET,
                f"a new red cycle would exceed the limit of {self.config.max_cycles}.",
                io,
            )
            return False
        s.cycles += 1
        s.red = s.green = None
        s.tests_edited = ()
        self._move(Phase.TEST_ONLY_EDIT, why)
        return True

    def _veto(self, text, io=None):
        if self._charge(io):
            return ResponseHookResult()
        return ResponseHookResult(NOT_APPLIED + text)

    def _scope(self, path):
        if os.path.isabs(path) or path == ".." or path.startswith("../"):
            return "outside"
        is_test = any(fnmatch.fnmatchcase(path, g) for g in self.config.test_globs)
        is_source = any(fnmatch.fnmatchcase(path, g) for g in self.config.source_globs)
        is_doc = any(fnmatch.fnmatchcase(path, g) for g in self.config.doc_globs)
        if (is_test + is_source + is_doc) > 1:
            return "ambiguous"
        return "test" if is_test else "source" if is_source else "doc" if is_doc else "outside"

    def _unmet_requirements(self, content="", *, check_completion=False):
        s = self.state
        tdd = self.config
        unmet = []
        if s.red is None:
            unmet.append(
                f"no observed red test yet (edit only {', '.join(tdd.test_globs)} and observe an "
                "assertion failure first; regression coverage is not satisfied by an old suite passing)"
            )
        if s.green is None:
            unmet.append(
                "no current observed green test run after the latest edits (an old or stale "
                "passing run does not satisfy verification)"
            )
        if check_completion and tdd.doc_globs and not s.docs_edited:
            unmet.append(
                f"configured documentation scope ({', '.join(tdd.doc_globs)}) has not been updated "
                "(a changed docs file alone is not proof its claims are correct; verify every claim)"
            )
        if self.citation_validator is not None and content:
            unknown = tuple(self.citation_validator(content) or ())
            if unknown:
                unmet.append(
                    f"cited passage ID(s) {', '.join(unknown)} were never returned in this "
                    "session (cite only returned passage IDs and origins; valid passage identity "
                    "does not prove semantic support)"
                )
        return tuple(unmet)

    # -- response hook -------------------------------------------------------------------

    def __call__(self, content, *, token_count=None, io=None):
        self._serial += 1
        s = self.state
        text = (content or "").strip()
        if text.startswith("<oracle_search>"):
            return None
        if s.phase is Phase.REPORT:
            # If the response contains code fences (an edit attempt), let check_edits handle it
            # unless it's a pure report response.
            if "```" not in text:
                unmet = self._unmet_requirements(text, check_completion=True)
                if unmet:
                    if self._charge(io):
                        return ResponseHookResult()
                    items = "\n".join(f"- {item}" for item in unmet)
                    return ResponseHookResult(
                        "Workflow is NOT complete. Unmet requirements:\n"
                        f"{items}\n"
                        "Note: regression coverage, documentation and the final report are not "
                        "satisfied by an old suite passing; a changed docs file alone is not proof "
                        "its claims are correct; and valid passage identity does not prove semantic "
                        "support. Next action: satisfy the unmet requirements above before "
                        "submitting the final report."
                    )
            self._move(Phase.DONE, "report response")
            self._done_serial = self._serial
            return None
        if (
            s.phase
            in (Phase.TEST_ONLY_EDIT, Phase.RED_OBSERVED, Phase.IMPLEMENT, Phase.REFACTOR_RECHECK)
            and "```" not in text
            and _EARLY_COMPLETION_RE.search(text)
        ):
            unmet = self._unmet_requirements(text, check_completion=True)
            if unmet:
                if self._charge(io):
                    return ResponseHookResult()
                items = "\n".join(f"- {item}" for item in unmet)
                return ResponseHookResult(
                    "Workflow is NOT complete. Unmet requirements:\n"
                    f"{items}\n"
                    "Next action: complete the required TDD steps before finishing."
                )
        return None

    # -- edit hook -----------------------------------------------------------------------

    def check_edits(self, proposal, *, io=None):
        s = self.state
        if s.phase is Phase.ABORTED:
            self._report_stop(io)
            return ResponseHookResult()
        if s.phase is Phase.DONE:
            if self._serial == self._done_serial:  # edits came with the report: reopen
                self._move(Phase.REPORT, "edit in the report response")
            elif not self._new_cycle("edit after the completed workflow", io):
                return ResponseHookResult()

        tests, sources, docs, bad = [], [], [], []
        for path in proposal.paths:
            scope = self._scope(path)
            {"test": tests, "source": sources, "doc": docs}.get(scope, bad).append(path)
        tdd = self.config
        if bad:
            doc_info = f" Doc globs: {', '.join(tdd.doc_globs)}." if tdd.doc_globs else ""
            return self._veto(
                f"These paths are outside the configured TDD scope or match multiple scopes: "
                f"{', '.join(bad)}. Test globs: {', '.join(tdd.test_globs)}. Source globs: "
                f"{', '.join(tdd.source_globs)}.{doc_info} Next action: edit only files in those scopes.",
                io,
            )
        if tests and (sources or docs):
            other = sources + docs
            return self._veto(
                f"Combined test and production edits are not allowed (tests: "
                f"{', '.join(tests)}; production/docs: {', '.join(other)}). Next action: send "
                "the test-only edit in this response, and make production changes in a separate "
                "response after the failing test has been observed.",
                io,
            )
        in_test_phase = s.phase is Phase.TEST_ONLY_EDIT
        if proposal.shell_commands and in_test_phase:
            commands = "; ".join(c.strip() for c in proposal.shell_commands)
            return self._veto(
                f"Shell commands ({commands}) are not allowed before a failing test has been "
                "observed; the configured test command runs automatically after each edit. "
                "Next action: reply with the test-only edit and no shell commands.",
                io,
            )
        if not tests and not sources and not docs:
            return None

        if tests:
            if not in_test_phase and not self._new_cycle("test edit invalidates red/green", io):
                return ResponseHookResult()
            s.green = None
            s.tests_edited = tuple(dict.fromkeys(s.tests_edited + tuple(tests)))
            return None

        if in_test_phase:
            targets = sources + docs
            return self._veto(
                f"Production edit to {', '.join(targets)} is not allowed yet: the phase is "
                "test_only_edit and no failing test has been observed. Next action: edit only "
                f"test files ({', '.join(tdd.test_globs)}) to add a test that fails with an "
                "assertion against the current implementation; do not touch production files "
                "in that response.",
                io,
            )
        if docs:
            s.docs_edited = tuple(dict.fromkeys(s.docs_edited + tuple(docs)))
        if s.phase is Phase.REPORT:
            if sources:
                if s.refactor_rechecks + 1 > tdd.max_refactor_rechecks:
                    self._abort(
                        StopReason.REFACTOR_BUDGET,
                        f"more than {tdd.max_refactor_rechecks} refactor rechecks.",
                        io,
                    )
                    return ResponseHookResult()
                s.refactor_rechecks += 1
            self._move(Phase.REFACTOR_RECHECK, "production edit after green")
        elif s.phase is Phase.RED_OBSERVED:
            self._move(Phase.IMPLEMENT, "production edit after observed red")
        s.green = None
        return None

    # -- test-result hook ----------------------------------------------------------------

    def observe_test(self, observation, *, io=None):
        s = self.state
        if s.phase is Phase.TEST_ONLY_EDIT and s.tests_edited:
            expect_red = True
        elif s.phase in (Phase.IMPLEMENT, Phase.REFACTOR_RECHECK):
            expect_red = False
        else:
            return None  # a manual run outside any expectation
        if not supports_pytest(observation.command):
            self._abort(
                StopReason.UNSUPPORTED_RUNNER,
                f"only pytest test commands are supported, but the test command is "
                f"{observation.command!r}; red and green cannot be classified.",
                io,
            )
            return None
        run = classify_pytest_run(observation, s.tests_edited)
        tdd = self.config

        if expect_red:
            if run.kind is RunKind.INTENDED_RED:
                s.red = run
                self._move(Phase.RED_OBSERVED, "intended red observed")
                return ResponseHookResult(
                    f"Observed red: {_run_line(run)} Failed tests:\n{_failed_listing(run.failed)}\n"
                    "This is the expected red. Next action: make the smallest production change "
                    f"({', '.join(tdd.source_globs)}) that makes these tests pass. Do not edit "
                    "test files in this step; the test command runs automatically afterwards."
                )
            if self._charge(io):
                return None
            if run.kind is RunKind.PASSED:
                return ResponseHookResult(
                    f"Test did not fail: not valid red. {_run_line(run)} The new test must fail "
                    "against the current implementation. Next action: edit only test files "
                    f"({', '.join(tdd.test_globs)}) so a test fails with an assertion; do not "
                    "change production code."
                )
            return ResponseHookResult(
                f"Unexpected result, not a valid red: {_run_line(run)} {run.detail}\n"
                f"Relevant output:\n{run.excerpt or '(none)'}\n"
                "Next action: edit only test files so the test runs and fails with an assertion "
                "(no import, collection or syntax errors); do not change production code."
            )

        if run.kind is RunKind.PASSED:
            s.green = run
            self._move(Phase.REPORT, "green observed")
            before_line = _run_line(s.red) if s.red else "(none recorded)"
            earlier = _failed_listing(s.red.failed) if s.red else "(none recorded)"
            doc_req = (
                f"- Documentation scope ({', '.join(tdd.doc_globs)}): "
                f"{'updated (' + ', '.join(s.docs_edited) + ')' if s.docs_edited else 'UNMET (not yet updated)'}. "
                "A changed docs file alone is not proof its claims are correct.\n"
                if tdd.doc_globs
                else "- Documentation: update any in-scope docs if requested; a changed docs file "
                "alone is not proof its claims are correct.\n"
            )
            return ResponseHookResult(
                f"Observed green: {_run_line(run)}\n"
                f"Before (observed red): {before_line}\n"
                f"Previously failing tests:\n{earlier}\n"
                f"After (observed green): {_run_line(run)}\n"
                "Task completion checklist (regression coverage, documentation and final report "
                "are not satisfied by an old suite passing):\n"
                f"- Regression coverage: new/updated test observed red then green.\n"
                f"{doc_req}"
                "- Grounded citations & report: cite only passage IDs and origins actually "
                "returned in this session (valid passage identity does not prove semantic support).\n"
                "Next action: review the change for refactoring or remaining doc updates. If you "
                "refactor or update docs, edit production/doc files only (tests re-run "
                "automatically). Otherwise reply without edits with the final report: what "
                "changed, which test was red then green, and any skipped tests."
            )
        if self._charge(io):
            return None
        failures = _failed_listing(run.failed) if run.failed else f"Relevant output:\n{run.excerpt}"
        return ResponseHookResult(
            f"Tests are not passing after your change: {_run_line(run)} {run.detail}\n{failures}\n"
            "Next action: fix the production code so the test command passes; do not edit tests."
        )
