"""Unit and integration tests for aider.caller_audit and Coder wiring."""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import git
import pytest

from aider.args import get_parser
from aider.caller_audit import (
    AuditVerdict,
    CallerContractHook,
    IsolatedCallerAuditor,
    parse_audit_verdict,
)
from aider.coders import Coder
from aider.hooks import ResponseHookResult
from aider.io import InputOutput
from aider.models import Model


@dataclass(frozen=True)
class FakeExcerpt:
    path: str
    start_line: int
    symbol: str
    code: str

    def format(self) -> str:
        return f"{self.path}:{self.start_line}\n{self.code}"


SAMPLE_EXCERPT = FakeExcerpt(
    path="pkg/consumer.py",
    start_line=12,
    symbol="format_price",
    code="raw = format_price(1050)\nassert raw == '10.50'\n",
)

EDIT_BLOCK = (
    "pricing.py\n"
    "```python\n"
    "<<<<<<< SEARCH\n"
    "def format_price(cents):\n"
    "    return f'{cents / 100:.2f}'\n"
    "=======\n"
    "def format_price(cents):\n"
    "    return f'${cents / 100:.2f}'\n"
    ">>>>>>> REPLACE\n"
    "```\n"
)

FIX_EDIT_BLOCK = (
    "pricing.py\n"
    "```python\n"
    "<<<<<<< SEARCH\n"
    "def format_price(cents):\n"
    "    return f'${cents / 100:.2f}'\n"
    "=======\n"
    "def format_price(cents):\n"
    "    return f'{cents / 100:.2f}'\n"
    ">>>>>>> REPLACE\n"
    "```\n"
)


# --- parse_audit_verdict ---


def test_parse_audit_verdict_pass():
    verdict = parse_audit_verdict("Traced 2 inputs + edge case.\nVERDICT: PASS")
    assert verdict.passed is True
    assert verdict.reason == "PASS"
    assert "VERDICT: PASS" in verdict.raw_response


def test_parse_audit_verdict_fail_same_line():
    raw = "Analysis...\nVERDICT: FAIL: format_price returns '$10.50' but consumer expects '10.50'"
    verdict = parse_audit_verdict(raw)
    assert verdict.passed is False
    assert "format_price returns '$10.50'" in verdict.reason
    assert verdict.raw_response == raw


def test_parse_audit_verdict_fail_multiline_and_case_insensitive():
    raw = "verdict: fail\nCounterexample: format_price(0) returns '$0.00', breaking float()."
    verdict = parse_audit_verdict(raw)
    assert verdict.passed is False
    assert "format_price(0)" in verdict.reason


def test_parse_audit_verdict_fail_fallback_when_nothing_follows():
    raw = "The caller unpacks (a, b) but the diff returns (b, a).\nVERDICT: FAIL"
    verdict = parse_audit_verdict(raw)
    assert verdict.passed is False
    assert "caller unpacks (a, b)" in verdict.reason


def test_parse_audit_verdict_uses_last_occurrence():
    raw = "Initially looked like VERDICT: PASS.\nWait, edge case fails.\nVERDICT: FAIL: fails on 0"
    verdict = parse_audit_verdict(raw)
    assert verdict.passed is False
    assert verdict.reason == "fails on 0"

    raw2 = "Draft VERDICT: FAIL: false alarm.\nActually fine.\nVERDICT: PASS"
    verdict2 = parse_audit_verdict(raw2)
    assert verdict2.passed is True


@pytest.mark.parametrize("raw", [None, "", "   \n\t  "])
def test_parse_audit_verdict_empty_fails_open(raw):
    verdict = parse_audit_verdict(raw)
    assert verdict.passed is True
    assert verdict.reason == "empty audit response"
    assert verdict.raw_response == (raw or "")


def test_parse_audit_verdict_no_token_fails_open():
    verdict = parse_audit_verdict("Everything looks consistent across callers.")
    assert verdict.passed is True
    assert verdict.raw_response == "Everything looks consistent across callers."


# --- CallerContractHook ---


def test_contract_hook_rejects_missing_contract_or_trace():
    io = Mock()
    hook = CallerContractHook(enabled=True, max_rejections=2)
    hook.begin_turn([SAMPLE_EXCERPT])

    res = hook(EDIT_BLOCK, io=io)
    assert isinstance(res, ResponseHookResult)
    assert "CALLER_CONTRACT:" in res.continuation
    assert "TRACE:" in res.continuation
    assert hook.rejections == 1
    io.tool_output.assert_called_once_with("Local caller contract check: missing contract/trace")


def test_contract_hook_rejects_missing_caller_reference_before_search():
    hook = CallerContractHook(enabled=True, max_rejections=2)
    hook.begin_turn([SAMPLE_EXCERPT])

    # Has CALLER_CONTRACT and TRACE, but never mentions pkg/consumer.py, consumer.py,
    # or format_price before <<<<<<< SEARCH.
    reply = (
        "CALLER_CONTRACT: returns a string\n"
        "TRACE: 100 -> '1.00'\n"
        + EDIT_BLOCK
    )
    res = hook(reply)
    assert isinstance(res, ResponseHookResult)
    assert hook.rejections == 1


def test_contract_hook_accepts_valid_contract_trace_and_caller_ref():
    hook = CallerContractHook(enabled=True, max_rejections=2)
    hook.begin_turn([SAMPLE_EXCERPT])

    for ref in ("pkg/consumer.py", "consumer.py", "format_price"):
        reply = (
            f"CALLER_CONTRACT: {ref} expects a plain decimal string without currency prefix.\n"
            "TRACE: input 1050 -> '10.50'; input 0 -> '0.00'.\n"
            + EDIT_BLOCK
        )
        assert hook(reply) is None
    assert hook.rejections == 0


def test_contract_hook_ignores_non_edit_replies():
    hook = CallerContractHook(enabled=True, max_rejections=2)
    hook.begin_turn([SAMPLE_EXCERPT])
    assert hook("I can explain how pricing works without making edits.") is None
    assert hook.rejections == 0


def test_contract_hook_respects_max_rejections_and_resets_on_begin_turn():
    hook = CallerContractHook(enabled=True, max_rejections=2)
    hook.begin_turn([SAMPLE_EXCERPT])

    assert isinstance(hook(EDIT_BLOCK), ResponseHookResult)
    assert isinstance(hook(EDIT_BLOCK), ResponseHookResult)
    assert hook.rejections == 2
    assert hook(EDIT_BLOCK) is None

    hook.begin_turn([SAMPLE_EXCERPT])
    assert hook.rejections == 0
    assert isinstance(hook(EDIT_BLOCK), ResponseHookResult)

    # Empty turn disables gating so stale excerpts do not leak.
    hook.begin_turn(())
    assert hook(EDIT_BLOCK) is None


def test_contract_hook_disabled_always_passes_through():
    hook = CallerContractHook(enabled=False)
    hook.begin_turn([SAMPLE_EXCERPT])
    assert hook(EDIT_BLOCK) is None


# --- IsolatedCallerAuditor ---


def test_auditor_builds_clean_two_message_prompt():
    auditor = IsolatedCallerAuditor(lambda msgs: "VERDICT: PASS")
    excerpt = SimpleNamespace(
        path="pkg/consumer.py",
        start_line=12,
        symbol="format_price",
        code="raw = format_price(1050)",
    )
    auditor.begin_turn([excerpt])

    msgs = auditor.build_messages("--- a/pricing.py\n+++ b/pricing.py\n+return '$10.50'")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "## Caller snippets" in msgs[1]["content"]
    assert "pkg/consumer.py:12" in msgs[1]["content"]
    assert "raw = format_price(1050)" in msgs[1]["content"]
    assert "## Proposed diff" in msgs[1]["content"]
    assert "+return '$10.50'" in msgs[1]["content"]


def test_auditor_returns_fail_verdict_and_continuation():
    captured = []

    def fake_complete(messages):
        captured.append(messages)
        return "Trace: 1050 -> '$10.50'\nVERDICT: FAIL: leading '$' breaks assert raw == '10.50'"

    io = Mock()
    auditor = IsolatedCallerAuditor(fake_complete, max_audits=1)
    auditor.begin_turn([SAMPLE_EXCERPT])

    verdict = auditor.audit("+    return f'${cents / 100:.2f}'", io=io)
    assert isinstance(verdict, AuditVerdict)
    assert verdict.passed is False
    assert "leading '$'" in verdict.reason
    assert len(captured) == 1
    assert len(captured[0]) == 2
    io.tool_output.assert_called_once()
    assert "Local caller audit: FAIL" in io.tool_output.call_args.args[0]

    cont = auditor.build_continuation(verdict)
    assert "leading '$'" in cont

    # Respects max_audits budget within the same turn
    assert auditor.audit("+    return 'x'", io=io) is None
    assert len(captured) == 1


def test_auditor_returns_pass_verdict():
    io = Mock()
    auditor = IsolatedCallerAuditor(lambda msgs: "All cases match.\nVERDICT: PASS")
    auditor.begin_turn([SAMPLE_EXCERPT])

    verdict = auditor.audit("+    return f'{cents / 100:.2f}'", io=io)
    assert verdict is not None
    assert verdict.passed is True
    io.tool_output.assert_called_once_with("Local caller audit: PASS")


def test_auditor_fails_open_on_exception_or_none():
    io = Mock()
    auditor_err = IsolatedCallerAuditor(Mock(side_effect=RuntimeError("network down")))
    auditor_err.begin_turn([SAMPLE_EXCERPT])
    verdict_err = auditor_err.audit("+x = 1", io=io)
    assert verdict_err is not None
    assert verdict_err.passed is True
    io.tool_warning.assert_called_once()

    auditor_none = IsolatedCallerAuditor(lambda msgs: None)
    auditor_none.begin_turn([SAMPLE_EXCERPT])
    verdict_none = auditor_none.audit("+x = 1")
    assert verdict_none is not None
    assert verdict_none.passed is True


def test_auditor_skips_when_empty_excerpts_or_diff():
    called = Mock(return_value="VERDICT: FAIL: should not run")
    auditor = IsolatedCallerAuditor(called)
    assert auditor.audit("+x = 1") is None
    auditor.begin_turn([SAMPLE_EXCERPT])
    assert auditor.audit("   \n") is None
    called.assert_not_called()


# --- CLI args parsing ---


def test_cli_args_local_callers_defaults_and_flags():
    parser = get_parser([], None)

    default_args = parser.parse_args([])
    assert default_args.local_callers is False
    assert default_args.local_callers_mode == "passive"
    assert default_args.local_callers_audit is False

    enabled_args = parser.parse_args(
        ["--local-callers", "--local-callers-mode", "contract", "--local-callers-audit"]
    )
    assert enabled_args.local_callers is True
    assert enabled_args.local_callers_mode == "contract"
    assert enabled_args.local_callers_audit is True


# --- Coder integration ---


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("webbrowser.open", Mock())
    monkeypatch.setattr("aider.repo.GitRepo.get_commit_message", lambda *a, **kw: "test commit")
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Audit test")
        config.set_value("user", "email", "audit-test@example.invalid")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pricing.py").write_text(
        "def format_price(cents):\n    return f'{cents / 100:.2f}'\n"
    )
    (tmp_path / "pkg" / "consumer.py").write_text(
        "from pricing import format_price\nraw = format_price(1050)\nassert raw == '10.50'\n"
    )
    repo.index.add(["pricing.py", "pkg/consumer.py"])
    repo.index.commit("Initial commit")
    return tmp_path


def make_coder(**kwargs):
    kwargs.setdefault("auto_commits", True)
    return Coder.create(
        main_model=Model("gpt-3.5-turbo"),
        edit_format="diff",
        fnames=["pricing.py"],
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        auto_lint=False,
        stream=False,
        map_tokens=0,
        detect_urls=False,
        **kwargs,
    )


def fake_send(coder, replies):
    sends = []

    def send(messages, functions=None):
        sends.append(
            "\n".join(
                m["content"]
                for m in messages
                if m["role"] == "user" and isinstance(m["content"], str)
            )
        )
        coder.partial_response_content = replies[min(len(sends), len(replies)) - 1]
        coder.partial_response_function_call = None
        return iter(())

    coder.send = send
    return sends


def _patch_local_callers(monkeypatch, excerpts=(SAMPLE_EXCERPT,)):
    import aider.local_callers as lc

    monkeypatch.setattr(lc, "find_related_callers", lambda *a, **kw: tuple(excerpts), raising=False)
    monkeypatch.setattr(
        lc,
        "format_caller_context",
        lambda ex, *, hint=None, mode="passive": "\n".join(e.format() for e in ex),
        raising=False,
    )


def test_coder_rejects_invalid_local_callers_mode(project):
    with pytest.raises(ValueError, match="local_callers_mode"):
        make_coder(local_callers=True, local_callers_mode="invalid")


def test_coder_does_not_mutate_caller_response_hooks_list_and_raises_max_reflections(project):
    user_hooks = [Mock(return_value=None)]
    coder = make_coder(
        local_callers=True,
        local_callers_mode="contract",
        local_callers_audit=True,
        response_hooks=user_hooks,
    )
    assert len(user_hooks) == 1
    assert coder.local_caller_contract_hook in coder.response_hooks
    assert coder.local_caller_auditor is not None
    assert coder.max_reflections >= 4


def test_coder_audit_fail_sets_reflected_message_with_counterexample(project, monkeypatch):
    _patch_local_callers(monkeypatch)
    coder = make_coder(local_callers=True, local_callers_audit=True)
    audit_calls = []

    def fake_audit(messages):
        audit_calls.append(messages)
        return (
            "VERDICT: FAIL: format_price(1050) returns '$10.50' "
            "but pkg/consumer.py expects '10.50'"
        )

    monkeypatch.setattr(coder.main_model, "simple_send_with_retries", fake_audit)
    sends = fake_send(coder, [EDIT_BLOCK, FIX_EDIT_BLOCK])

    coder.run("update format_price")

    assert len(audit_calls) == 1
    assert len(audit_calls[0]) == 2
    assert "pkg/consumer.py:12" in audit_calls[0][1]["content"]
    assert "format_price" in audit_calls[0][1]["content"]
    assert len(sends) == 2
    assert "format_price(1050) returns '$10.50'" in sends[1]
    assert (project / "pricing.py").read_text() == (
        "def format_price(cents):\n    return f'{cents / 100:.2f}'\n"
    )


def test_coder_audit_pass_does_not_reflect(project, monkeypatch):
    _patch_local_callers(monkeypatch)
    coder = make_coder(local_callers=True, local_callers_audit=True)
    audit_calls = []

    def fake_audit(messages):
        audit_calls.append(messages)
        return "VERDICT: PASS"

    monkeypatch.setattr(coder.main_model, "simple_send_with_retries", fake_audit)
    sends = fake_send(coder, [EDIT_BLOCK])

    coder.run("update format_price")

    assert len(audit_calls) == 1
    assert len(sends) == 1
    assert coder.reflected_message is None


def test_coder_contract_mode_gates_before_applying_edits(project, monkeypatch):
    _patch_local_callers(monkeypatch)
    coder = make_coder(local_callers=True, local_callers_mode="contract")
    valid_reply = (
        "CALLER_CONTRACT: pkg/consumer.py calls format_price(1050) expecting '10.50'.\n"
        "TRACE: format_price(1050) -> '$10.50'; format_price(0) -> '$0.00'.\n"
        + EDIT_BLOCK
    )
    sends = fake_send(coder, [EDIT_BLOCK, valid_reply])

    coder.run("update format_price")

    assert len(sends) == 2
    assert "CALLER_CONTRACT:" in sends[1]
    assert "TRACE:" in sends[1]
    assert coder.local_caller_contract_hook.rejections == 1
    assert coder.get_inchat_relative_files() == ["pricing.py"]


def test_contract_hook_does_not_reject_followup_reflection_once_satisfied():
    hook = CallerContractHook(enabled=True, max_rejections=2)
    hook.begin_turn([SAMPLE_EXCERPT])
    valid = (
        "CALLER_CONTRACT: pkg/consumer.py expects string without $.\n"
        "TRACE: format_price(1050) -> '10.50'.\n"
        + EDIT_BLOCK
    )
    assert hook(valid) is None
    # Follow-up edit after an audit/test reflection should not be rejected again.
    assert hook(FIX_EDIT_BLOCK) is None
    # New turn resets satisfaction.
    hook.begin_turn([SAMPLE_EXCERPT])
    assert hook(FIX_EDIT_BLOCK) is not None


def test_auditor_uses_task_and_updated_files_instead_of_diff():
    captured = []

    def complete(messages):
        captured.append(messages)
        return "VERDICT: PASS"

    auditor = IsolatedCallerAuditor(complete)
    auditor.begin_turn([SAMPLE_EXCERPT])
    verdict = auditor.audit(
        "- old_bug()\n+ new_fix()",
        task="Update format_price to return dollars.",
        updated_files={"pricing.py": "def format_price(cents):\n    return f'{cents/100:.2f}'\n"},
    )
    assert verdict is not None and verdict.passed
    user_msg = captured[0][1]["content"]
    assert "## Task" in user_msg
    assert "Update format_price to return dollars." in user_msg
    assert "## Updated implementation" in user_msg
    assert "### pricing.py" in user_msg
    assert "old_bug" not in user_msg
