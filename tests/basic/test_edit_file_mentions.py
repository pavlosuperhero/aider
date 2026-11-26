from pathlib import Path
from unittest.mock import Mock

import git
import pytest

from aider.coders import Coder
from aider.hooks import ResponseHookResult
from aider.io import InputOutput
from aider.models import Model

BEFORE = "value = 1\n"
AFTER = 'value = "payload.py"\n'
EDIT = (
    "a.py\n"
    "```python\n"
    "<<<<<<< SEARCH\n"
    f"{BEFORE}"
    "=======\n"
    f"{AFTER}"
    ">>>>>>> REPLACE\n"
    "```\n"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Integration test")
        config.set_value("user", "email", "integration-test@example.invalid")
    (tmp_path / "a.py").write_text(BEFORE, encoding="utf-8")
    (tmp_path / "b.py").write_text("CONTEXT = 2\n", encoding="utf-8")
    (tmp_path / "payload.py").write_text("PAYLOAD = 3\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text(
        "from a import value\n" + "# Full file must stay out of the prompt\n" * 4000,
        encoding="utf-8",
    )
    repo.index.add(["a.py", "b.py", "consumer.py", "payload.py"])
    repo.index.commit("Initial commit")
    return repo


def make_coder(replies, **kwargs):
    coder = Coder.create(
        main_model=Model("gpt-3.5-turbo"),
        edit_format="diff",
        fnames=["a.py"],
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        auto_commits=True,
        auto_lint=False,
        auto_test=False,
        stream=False,
        map_tokens=0,
        detect_urls=False,
        **kwargs,
    )
    coder.repo.get_commit_message = Mock(return_value="Update value")
    coder.io.confirm_ask = Mock(return_value=True)
    responses = iter(replies)

    def send(messages, functions=None):
        coder.partial_response_content = next(responses, "No further edits.")
        coder.partial_response_function_call = None
        return iter(())

    coder.send = Mock(side_effect=send)
    return coder


@pytest.mark.parametrize("accept_mention", [True, False])
def test_edit_and_file_mention_finish_in_one_model_response(repo, accept_mention):
    response = EDIT + "See b.py and consumer.py for context."
    coder = make_coder([response])
    excerpt = coder.read_file_range("consumer.py", 1, 1)
    coder.add_read_only_range(excerpt)

    def confirm_mention(*args, **kwargs):
        assert repo.head.commit.tree["a.py"].data_stream.read().decode("utf-8") == AFTER
        return accept_mention

    coder.io.confirm_ask.side_effect = confirm_mention
    coder.run_one("Update the value.", preproc=False)

    assert Path("a.py").read_text(encoding="utf-8") == AFTER
    assert not repo.is_dirty()
    assert len(list(repo.iter_commits())) == 2
    assert coder.get_inchat_relative_files() == (["a.py", "b.py"] if accept_mention else ["a.py"])
    assert coder.aider_edited_files == {"a.py"}
    assert coder.num_reflections == 0
    coder.send.assert_called_once()
    assert coder.io.confirm_ask.call_args.kwargs["subject"] == "b.py"
    coder.io.confirm_ask.assert_called_once()
    prompt = str(coder.send.call_args.args[0])
    assert "consumer.py:1-1" in prompt
    assert "Full file must stay out of the prompt" not in prompt


def test_mention_only_reply_still_reflects_with_added_file(repo):
    coder = make_coder(["Please add b.py.\n\n```sh\necho ignored\n```\n", "Thanks for adding it."])
    coder.handle_shell_commands = Mock(return_value=None)

    coder.run_one("Find the relevant file.", preproc=False)

    assert coder.get_inchat_relative_files() == ["a.py", "b.py"]
    assert coder.num_reflections == 1
    assert coder.send.call_count == 2
    assert "CONTEXT = 2" in str(coder.send.call_args.args[0])
    assert len(list(repo.iter_commits())) == 1
    assert not repo.is_dirty()
    coder.handle_shell_commands.assert_not_called()


def test_file_mention_opt_out_survives_coder_switch(repo):
    coder = make_coder([], auto_add_file_mentions=False).clone()
    coder.partial_response_content = "See b.py for context."
    coder.partial_response_function_call = None
    coder.send = Mock(return_value=iter(()))

    coder.run_one("See b.py for context.", preproc=True)

    assert coder.get_inchat_relative_files() == ["a.py"]
    assert coder.num_reflections == 0
    coder.send.assert_called_once()


def test_strict_audit_unknown_leaves_edit_uncommitted_and_mentions_unattached(repo):
    coder = make_coder(
        [EDIT + "See b.py for context."], local_callers=True, local_callers_audit=True
    )
    coder.run_shell_commands = Mock()

    coder.run_one("Update the value.", preproc=False)

    assert Path("a.py").read_text(encoding="utf-8") == AFTER
    assert repo.is_dirty()
    assert coder.get_inchat_relative_files() == ["a.py"]
    assert coder.reflected_message is None
    assert coder.aider_edited_files == {"a.py"}
    assert len(list(repo.iter_commits())) == 1
    coder.io.confirm_ask.assert_not_called()
    coder.run_shell_commands.assert_not_called()
    coder.send.assert_called_once()


@pytest.mark.parametrize("hook_type", ["response_hooks", "edit_hooks"])
def test_hook_veto_does_not_add_mentions_or_apply_edits(repo, hook_type):
    hook = Mock(return_value=ResponseHookResult())
    response = EDIT + "See b.py for context.\n\n```sh\necho ignored\n```\n"
    coder = make_coder([response], **{hook_type: (hook,)})
    coder.run_shell_commands = Mock()

    coder.run_one("Update the value.", preproc=False)

    assert Path("a.py").read_text(encoding="utf-8") == BEFORE
    assert coder.get_inchat_relative_files() == ["a.py"]
    assert coder.reflected_message is None
    assert coder.aider_edited_files == set()
    assert len(list(repo.iter_commits())) == 1
    assert not repo.is_dirty()
    coder.io.confirm_ask.assert_not_called()
    coder.run_shell_commands.assert_not_called()
    hook.assert_called_once()
