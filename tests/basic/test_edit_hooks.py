"""Tests for the generic pre-apply edit hook (Coder(edit_hooks=...)).

Real coders and real edit parsers run in a temporary Git repository; only the model
reply is faked. No network, no sleeps.
"""

import os
from unittest.mock import Mock

import git
import pytest

from aider.coders import Coder
from aider.hooks import EditProposal, ResponseHookResult
from aider.io import InputOutput
from aider.models import Model


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("webbrowser.open", Mock())
    # Auto-commit would otherwise ask the (network) weak model for a commit message.
    monkeypatch.setattr("aider.repo.GitRepo.get_commit_message", lambda *a, **kw: "test commit")
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Hook test")
        config.set_value("user", "email", "hook-test@example.invalid")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "greeting.py").write_text('print("before")\n')
    (tmp_path / "pkg" / "util.py").write_text("X = 1\n")
    repo.index.add(["greeting.py", "pkg/util.py"])
    repo.index.commit("Initial fixture")
    return tmp_path


def make_coder(edit_format, **kwargs):
    kwargs.setdefault("auto_commits", True)
    return Coder.create(
        main_model=Model("gpt-3.5-turbo"),
        edit_format=edit_format,
        fnames=["greeting.py", "pkg/util.py"],
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


EDITBLOCK = (
    'greeting.py\n```python\n<<<<<<< SEARCH\nprint("before")\n=======\n'
    'print("after")\n>>>>>>> REPLACE\n```\n'
)
EDITBLOCK_SHELL = EDITBLOCK + "\n```bash\ntouch shell-ran.txt\n```\n"
WHOLE = 'greeting.py\n```python\nprint("after")\n```'
WHOLE_TWO = WHOLE + "\n\npkg/util.py\n```python\nX = 2\n```"

FORMATS = pytest.mark.parametrize(
    "edit_format, reply",
    [("diff", EDITBLOCK), ("whole", WHOLE)],
    ids=["editblock", "wholefile"],
)


def read(project, name):
    return (project / name).read_text()


@FORMATS
def test_no_hook_applies_edits_as_before(project, edit_format, reply):
    coder = make_coder(edit_format)
    assert coder.edit_hooks == ()
    fake_send(coder, [reply])
    coder.run("change it")
    assert read(project, "greeting.py") == 'print("after")\n'


@FORMATS
def test_hook_sees_normalized_relative_paths_and_may_accept(project, edit_format, reply):
    hook = Mock(return_value=None)
    coder = make_coder(edit_format, edit_hooks=[hook])  # a list is accepted
    fake_send(coder, [reply])
    coder.run("change it")
    proposal = hook.call_args.args[0]
    assert proposal == EditProposal(("greeting.py",))
    assert hook.call_args.kwargs == {"io": coder.io}
    assert read(project, "greeting.py") == 'print("after")\n'


def test_paths_are_relative_normalized_and_unique(project):
    hook = Mock(return_value=None)
    coder = make_coder("whole", edit_hooks=(hook,))
    reply = WHOLE_TWO.replace("greeting.py\n", "./greeting.py\n", 1) + "\n" + WHOLE
    fake_send(coder, [reply])
    coder.run("change it")
    assert hook.call_args.args[0].paths == ("greeting.py", "pkg/util.py")


def test_proposal_includes_suggested_shell_commands(project):
    hook = Mock(return_value=None)
    coder = make_coder("diff", edit_hooks=(hook,))
    fake_send(coder, [EDITBLOCK_SHELL])
    coder.run("change it")
    assert hook.call_args.args[0] == EditProposal(("greeting.py",), ("touch shell-ran.txt\n",))


def test_hook_not_called_without_edits_or_shell_commands(project):
    hook = Mock(return_value=None)
    coder = make_coder("diff", edit_hooks=(hook,))
    fake_send(coder, ["Just talking."])
    coder.run("hello")
    hook.assert_not_called()


@FORMATS
def test_veto_prevents_write_commit_and_reports_not_applied(project, edit_format, reply):
    repo = git.Repo(project)
    head = repo.head.commit.hexsha
    hook = Mock(side_effect=[ResponseHookResult("Edit rejected: write a test first."), None])
    coder = make_coder(edit_format, edit_hooks=(hook,))
    sends = fake_send(coder, [reply, "Understood, no edit."])
    coder.run("change it")

    assert hook.call_count == 1
    assert read(project, "greeting.py") == 'print("before")\n'
    assert repo.head.commit.hexsha == head
    assert not repo.is_dirty()
    assert len(sends) == 2
    assert "Edit rejected: write a test first." in sends[1]
    assert coder.aider_edited_files == set()


def test_veto_prevents_shell_commands_and_does_not_leak_them_to_later_turns(project):
    hook = Mock(side_effect=[ResponseHookResult("Rejected."), None])
    coder = make_coder("diff", edit_hooks=(hook,))
    sends = fake_send(coder, [EDITBLOCK_SHELL, "Fine."])
    coder.run("change it")
    assert not (project / "shell-ran.txt").exists()
    assert coder.shell_commands == []
    assert len(sends) == 2


def test_veto_happens_before_new_file_creation_and_dirty_commit(project):
    (project / "greeting.py").write_text('print("dirty")\n')  # uncommitted user change
    repo = git.Repo(project)
    head = repo.head.commit.hexsha
    hook = Mock(side_effect=[ResponseHookResult("Rejected."), None])
    coder = make_coder("whole", edit_hooks=(hook,))
    reply = WHOLE + '\n\nbrand_new.py\n```python\nprint("new")\n```'
    fake_send(coder, [reply, "ok"])
    coder.run("create it")
    assert not (project / "brand_new.py").exists()
    assert read(project, "greeting.py") == 'print("dirty")\n'
    assert repo.head.commit.hexsha == head  # no "commit before edits" happened


def test_veto_with_no_continuation_stops_without_a_reflection(project):
    hook = Mock(return_value=ResponseHookResult())
    coder = make_coder("whole", edit_hooks=(hook,))
    sends = fake_send(coder, [WHOLE, "never sent"])
    coder.run("change it")
    assert len(sends) == 1
    assert read(project, "greeting.py") == 'print("before")\n'
    assert coder.reflected_message is None


def test_first_veto_wins_and_later_hooks_are_skipped(project):
    first = Mock(return_value=ResponseHookResult("first"))
    second = Mock(return_value=ResponseHookResult("second"))
    coder = make_coder("whole", edit_hooks=(first, second))
    sends = fake_send(coder, [WHOLE, "ok"])
    coder.run("change it")
    second.assert_not_called()
    assert "first" in sends[1]


def test_bad_hook_result_raises_before_any_write(project):
    coder = make_coder("whole", edit_hooks=(lambda proposal, io=None: "nope",))
    coder.io.tool_error = Mock()
    sends = fake_send(coder, [WHOLE])
    coder.run("change it")  # apply_updates reports hook errors and aborts before any write
    assert read(project, "greeting.py") == 'print("before")\n'
    assert len(sends) == 1
    assert any(
        "Edit hooks must return None or ResponseHookResult" in str(c.args[0])
        for c in coder.io.tool_error.call_args_list
    )


def test_hooks_survive_coder_clone(project):
    hook = Mock(return_value=None)
    coder = make_coder("whole", edit_hooks=[hook])
    clone = coder.clone()
    assert clone.edit_hooks == (hook,)
    assert os.path.exists(project / "greeting.py")
