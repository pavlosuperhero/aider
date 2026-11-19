"""Tests for the optional test-result hook contract (aider/hooks.py).

Hooks are fakes and test commands are tiny local shell snippets: no network, no sleeps.
"""

import sys
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import git
import pytest

from aider.coders import Coder
from aider.hooks import MAX_TEST_OUTPUT_CHARS, ResponseHookResult, TestRunObservation
from aider.io import InputOutput
from aider.models import Model

# Not a test class, despite the name.
TestRunObservation.__test__ = False

PY = sys.executable


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("webbrowser.open", Mock())
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Hook test")
        config.set_value("user", "email", "hook-test@example.invalid")
    (tmp_path / "greeting.py").write_text('print("before")\n')
    repo.index.add(["greeting.py"])
    repo.index.commit("Initial fixture")
    return tmp_path


def make_coder(test_cmd=None, *, stream=False, **kwargs):
    return Coder.create(
        main_model=Model("gpt-3.5-turbo"),
        edit_format="whole",
        fnames=["greeting.py"],
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        auto_commits=False,
        auto_lint=False,
        auto_test=bool(test_cmd),
        test_cmd=test_cmd,
        stream=stream,
        map_tokens=0,
        detect_urls=False,
        **kwargs,
    )


def script_cmd(code):
    return f'{PY} -c "{code}"'


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


EDIT = 'greeting.py\n```python\nprint("after")\n```'


def test_failure_observation_is_delivered(project):
    hook = Mock(return_value=None)
    coder = make_coder(
        script_cmd("import sys; print('boom'); sys.exit(3)"), test_result_hooks=(hook,)
    )
    result = coder.commands.cmd_test(coder.test_cmd)

    obs = hook.call_args.args[0]
    assert isinstance(obs, TestRunObservation)
    assert obs.exit_status == 3
    assert "boom" in obs.output
    assert obs.command == coder.test_cmd
    assert obs.cwd == coder.root
    assert not obs.truncated
    assert hook.call_args.kwargs == {"io": coder.io}
    assert result and "boom" in result
    with pytest.raises(FrozenInstanceError):
        obs.exit_status = 0


def test_success_observation_is_delivered_with_output(project):
    hook = Mock(return_value=None)
    coder = make_coder(script_cmd("print('all green')"), test_result_hooks=[hook])
    assert coder.commands.cmd_test(coder.test_cmd) is None

    obs = hook.call_args.args[0]
    assert obs.exit_status == 0
    assert "all green" in obs.output


def test_no_hook_keeps_return_values_and_messages(project):
    coder = make_coder(script_cmd("print('green')"))
    assert coder.test_result_hooks == ()
    before = list(coder.cur_messages)
    assert coder.commands.cmd_test(coder.test_cmd) is None
    assert coder.cur_messages == before

    coder = make_coder(script_cmd("import sys; print('red'); sys.exit(1)"))
    errors = coder.commands.cmd_test(coder.test_cmd)
    assert "red" in errors
    assert coder.cur_messages[-1] == {"role": "assistant", "content": "Ok."}


def test_hook_does_not_change_cmd_test_return_value(project):
    hook = Mock(return_value=ResponseHookResult("keep going"))
    coder = make_coder(script_cmd("print('green')"), test_result_hooks=(hook,))
    assert coder.commands.cmd_test(coder.test_cmd) is None
    coder = make_coder(
        script_cmd("import sys; print('red'); sys.exit(1)"), test_result_hooks=(hook,)
    )
    assert "red" in coder.commands.cmd_test(coder.test_cmd)


def test_passing_continuation_reaches_model_as_reflected_message(project):
    hook = Mock(return_value=ResponseHookResult("Report the passing result."))
    coder = make_coder(script_cmd("print('green')"), test_result_hooks=(hook,))
    sends = fake_send(coder, [EDIT, "done"])
    coder.run("Update greeting.py")

    assert len(sends) == 2
    assert "Report the passing result." in sends[1]
    assert hook.call_count == 1  # the reply "done" made no edit, so no re-run
    # Re-run only happens naturally when the continuation edits again.
    assert hook.call_args_list[0].args[0].exit_status == 0


def test_continuation_turn_with_edits_reruns_tests(project):
    hook = Mock(side_effect=[ResponseHookResult("Report."), None])
    coder = make_coder(script_cmd("print('green')"), test_result_hooks=(hook,))
    sends = fake_send(coder, [EDIT, EDIT.replace("after", "again")])
    coder.run("Update greeting.py")
    assert len(sends) == 2
    assert hook.call_count == 2


def test_failure_without_continuation_keeps_confirm_flow(project):
    hook = Mock(return_value=None)
    coder = make_coder(
        script_cmd("import sys; print('red'); sys.exit(1)"), test_result_hooks=(hook,)
    )
    coder.io.confirm_ask = Mock(return_value=False)
    sends = fake_send(coder, [EDIT])
    coder.run("Update greeting.py")
    coder.io.confirm_ask.assert_called_once_with("Attempt to fix test errors?")
    assert len(sends) == 1
    assert coder.test_outcome is False


def test_first_continuation_wins(project):
    first = Mock(return_value=None)
    second = Mock(return_value=ResponseHookResult("second"))
    third = Mock(return_value=ResponseHookResult("third"))
    coder = make_coder(script_cmd("print('g')"), test_result_hooks=(first, second, third))
    sends = fake_send(coder, [EDIT, "done"])
    coder.run("Update greeting.py")
    assert "second" in sends[1] and "third" not in sends[1]
    third.assert_not_called()


def test_continuation_is_bounded_by_max_reflections(project):
    hook = Mock(return_value=ResponseHookResult("again"))
    coder = make_coder(script_cmd("print('g')"), test_result_hooks=(hook,))
    coder.max_reflections = 2
    sends = fake_send(coder, [EDIT])
    coder.run("Update greeting.py")
    assert len(sends) == 3  # initial turn + max_reflections continuations
    assert hook.call_count == 3


def test_streaming_path_delivers_observation(project):
    hook = Mock(return_value=None)
    coder = make_coder(script_cmd("print('g')"), stream=True, test_result_hooks=(hook,))
    fake_send(coder, [EDIT])
    coder.run("Update greeting.py")
    assert hook.call_count == 1


def test_create_and_clone_keep_hooks(project):
    hook = Mock(return_value=None)
    coder = make_coder(test_result_hooks=iter((hook,)))
    assert coder.test_result_hooks == (hook,)
    assert coder.clone().test_result_hooks == (hook,)
    assert coder.clone(edit_format="diff").test_result_hooks == (hook,)


def test_long_output_is_truncated_keeping_tail(project):
    hook = Mock(return_value=None)
    code = f"print('HEAD' + 'x' * {MAX_TEST_OUTPUT_CHARS * 2}); print('TAIL')"
    coder = make_coder(script_cmd(code), test_result_hooks=(hook,))
    coder.commands.cmd_test(coder.test_cmd)
    obs = hook.call_args.args[0]
    assert obs.truncated
    assert len(obs.output) <= MAX_TEST_OUTPUT_CHARS
    assert "TAIL" in obs.output
    assert "HEAD" not in obs.output


def test_observation_helper_marks_short_output_untruncated():
    obs = TestRunObservation.create("cmd", "/tmp", 0, "short")
    assert (obs.output, obs.truncated) == ("short", False)
