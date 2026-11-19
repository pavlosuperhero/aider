"""Integration tests for the optional TDD workflow.

Real Coder, real edit parsers/writers, a real temporary Git repository and a real pytest
subprocess as the test command. Only the model reply is faked. No network, no sleeps.
"""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import git
import pytest

from aider.args import get_parser
from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model
from aider.tdd import Phase, RunKind, StopReason, TddConfig, TddController

TEST_CMD = f"{sys.executable} -m pytest -q -p no:cacheprovider"

CALC = "def add(a, b):\n    return a\n"
TEST_CALC = "from calc import add\n\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("webbrowser.open", Mock())
    monkeypatch.setattr("aider.repo.GitRepo.get_commit_message", lambda *a, **kw: "test commit")
    monkeypatch.setattr("aider.history.ChatSummary.too_big", lambda *a, **kw: False)
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "TDD test")
        config.set_value("user", "email", "tdd-test@example.invalid")
    (tmp_path / "tests").mkdir()
    (tmp_path / "calc.py").write_text(CALC)
    (tmp_path / "tests" / "test_calc.py").write_text(TEST_CALC)
    repo.index.add(["calc.py", "tests/test_calc.py"])
    repo.index.commit("Initial fixture")
    return tmp_path


def make_config(**overrides):
    return TddConfig(test_globs=("tests/*",), source_globs=("calc.py",), **overrides)


def make_coder(edit_format="whole", test_cmd=TEST_CMD, **overrides):
    controller = TddController(make_config(**overrides))
    coder = Coder.create(
        main_model=Model("gpt-3.5-turbo"),
        edit_format=edit_format,
        fnames=["calc.py", "tests/test_calc.py"],
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        auto_commits=False,
        auto_lint=False,
        auto_test=True,
        test_cmd=test_cmd,
        stream=False,
        map_tokens=0,
        detect_urls=False,
        **controller.hook_kwargs(),
    )
    return coder, controller


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


def whole(name, content):
    return f"{name}\n```python\n{content}```\n"


FAILING_TEST = TEST_CALC + "\n\ndef test_add_two():\n    assert add(1, 2) == 3\n"
PASSING_TEST = TEST_CALC + "\n\ndef test_add_identity():\n    assert add(5, 0) == 5\n"
BROKEN_IMPORT_TEST = "from calc import nothing_here\n\n\ndef test_x():\n    assert nothing_here\n"
FIXED_CALC = "def add(a, b):\n    return a + b\n"


def read(project, name):
    return (project / name).read_text()


def test_happy_path_editblock_red_then_green_then_report(project):
    coder, controller = make_coder("diff")
    old = "    assert add(0, 0) == 0\n"
    test_edit = (
        "tests/test_calc.py\n```python\n<<<<<<< SEARCH\n"
        + old
        + "=======\n"
        + old
        + "\n\ndef test_add_two():\n    assert add(1, 2) == 3\n>>>>>>> REPLACE\n```\n"
    )
    src_edit = (
        "calc.py\n```python\n<<<<<<< SEARCH\n    return a\n=======\n"
        "    return a + b\n>>>>>>> REPLACE\n```\n"
    )
    sends = fake_send(coder, [test_edit, src_edit, "Final report: add now sums."])
    coder.run("make add sum its arguments")

    assert len(sends) == 3
    # Message 2 is the literal red continuation, derived from the real pytest run.
    assert "exit status 1" in sends[1] and TEST_CMD in sends[1]
    assert "tests/test_calc.py::test_add_two" in sends[1]
    assert "assert 1 == 3" in sends[1]
    # Message 3 is the green/report continuation.
    assert "exit status 0" in sends[2] and "2 passed" in sends[2]
    assert read(project, "calc.py") == FIXED_CALC
    state = controller.state
    assert state.phase is Phase.DONE
    assert state.red.kind is RunKind.INTENDED_RED
    assert [f.node_id for f in state.red.failed] == ["tests/test_calc.py::test_add_two"]
    assert state.green.kind is RunKind.PASSED
    assert state.stop_reason is None


def test_happy_path_wholefile(project):
    coder, controller = make_coder("whole")
    sends = fake_send(
        coder,
        [
            whole("tests/test_calc.py", FAILING_TEST),
            whole("calc.py", FIXED_CALC),
            "Report.",
        ],
    )
    coder.run("make add sum its arguments")
    assert controller.state.phase is Phase.DONE
    assert len(sends) == 3


def test_combined_patch_is_vetoed_and_nothing_runs(project):
    coder, controller = make_coder("diff")
    test_edit = (
        "tests/test_calc.py\n```python\n<<<<<<< SEARCH\n    assert add(0, 0) == 0\n=======\n"
        "    assert add(0, 0) == 0\n\n\ndef test_add_two():\n    assert add(1, 2) == 3\n"
        ">>>>>>> REPLACE\n```\n"
    )
    src_edit = (
        "calc.py\n```python\n<<<<<<< SEARCH\n    return a\n=======\n"
        "    return a + b\n>>>>>>> REPLACE\n```\n"
    )
    marker = "```bash\ntouch ran.txt\n```\n"
    sends = fake_send(coder, [test_edit + "\n" + src_edit + "\n" + marker, "ok"])
    coder.run("do both")

    assert read(project, "calc.py") == CALC
    assert read(project, "tests/test_calc.py") == TEST_CALC
    assert not (project / "ran.txt").exists()
    assert "NOT applied" in sends[1]
    assert "separate" in sends[1]
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.state.red is None


def test_production_edit_before_red_is_vetoed(project):
    coder, controller = make_coder("whole")
    sends = fake_send(coder, [whole("calc.py", FIXED_CALC), "ok"])
    coder.run("just fix it")
    assert read(project, "calc.py") == CALC
    assert "NOT applied" in sends[1]
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert not coder.aider_edited_files


def test_passing_new_test_is_not_a_valid_red(project):
    coder, controller = make_coder("whole")
    sends = fake_send(coder, [whole("tests/test_calc.py", PASSING_TEST), "stop"])
    coder.run("add a test")
    assert "did not fail" in sends[1] and "not valid red" in sends[1]
    assert "exit status 0" in sends[1]
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.state.red is None
    assert controller.state.corrections == 1


def test_collection_error_is_not_red(project):
    coder, controller = make_coder("whole")
    sends = fake_send(coder, [whole("tests/test_calc.py", BROKEN_IMPORT_TEST), "stop"])
    coder.run("add a test")
    assert "exit status 2" in sends[1]
    assert "not a valid red" in sends[1]
    assert "nothing_here" in sends[1] or "cannot import" in sends[1]
    assert controller.state.phase is Phase.TEST_ONLY_EDIT
    assert controller.state.red is None


def test_refactor_edit_after_green_requires_a_recheck(project):
    coder, controller = make_coder("whole")
    fake_send(
        coder,
        [
            whole("tests/test_calc.py", FAILING_TEST),
            whole("calc.py", FIXED_CALC),
            whole("calc.py", FIXED_CALC + "\n# refactor\n"),
            "Final report.",
        ],
    )
    coder.run("make add sum")
    # Green was reached, then the model's refactor edit re-opened verification
    # and the re-run restored green before the report.
    assert controller.state.refactor_rechecks == 1
    assert controller.state.phase is Phase.DONE


def test_test_edit_after_green_invalidates_red_and_green(project):
    coder, controller = make_coder("whole")
    fake_send(
        coder,
        [
            whole("tests/test_calc.py", FAILING_TEST),
            whole("calc.py", FIXED_CALC),
            whole("tests/test_calc.py", PASSING_TEST),
            "stop",
        ],
    )
    coder.run("make add sum")
    state = controller.state
    assert state.phase is Phase.TEST_ONLY_EDIT
    assert state.green is None and state.red is None
    assert state.cycles == 2


def test_unsupported_test_runner_aborts_before_any_production_edit(project):
    coder, controller = make_coder("whole", test_cmd=f'{sys.executable} -c "print(1)"')
    sends = fake_send(coder, [whole("tests/test_calc.py", FAILING_TEST), "x"])
    coder.run("add a test")
    assert controller.state.stop_reason is StopReason.UNSUPPORTED_RUNNER
    assert len(sends) == 1


# ---- CLI / YAML ----------------------------------------------------------------------------


def clean_env(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("AIDER_TDD"):
            monkeypatch.delenv(name)


def tdd_args(parser, *extra):
    return parser.parse_args(
        [
            "--tdd-test-glob",
            "tests/*",
            "--tdd-source-glob",
            "calc.py",
            "--tdd-source-glob",
            "pkg/*",
            "--auto-test",
            "--test-cmd",
            "python -m pytest -q",
            *extra,
        ]
    )


def test_parser_defaults_to_off_and_supports_disable_override(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    parser = get_parser([], None)
    args = parser.parse_args([])
    assert args.tdd is False
    assert not args.tdd_test_glob and not args.tdd_source_glob
    assert parser.parse_args(["--tdd"]).tdd is True
    assert parser.parse_args(["--tdd", "--no-tdd"]).tdd is False

    config = tmp_path / ".aider.conf.yml"
    config.write_text(
        "tdd: true\ntdd-test-glob:\n  - tests/*\ntdd-source-glob:\n  - calc.py\n  - pkg/*\n"
    )
    parser = get_parser([str(config)], None)
    args = parser.parse_args([])
    assert args.tdd is True
    assert args.tdd_test_glob == ["tests/*"]
    assert args.tdd_source_glob == ["calc.py", "pkg/*"]
    assert parser.parse_args(["--no-tdd"]).tdd is False
    monkeypatch.setenv("AIDER_TDD", "false")
    assert parser.parse_args([]).tdd is False


def test_config_from_args_and_refusals(monkeypatch):
    clean_env(monkeypatch)
    parser = get_parser([], None)
    config = TddConfig.from_args(tdd_args(parser, "--tdd"))
    assert config.test_globs == ("tests/*",)
    assert config.source_globs == ("calc.py", "pkg/*")

    def refused(*argv, edit_format=None):
        args = parser.parse_args(list(argv))
        with pytest.raises(ValueError) as err:
            TddConfig.from_args(args, edit_format=edit_format)
        return str(err.value)

    base = ["--tdd", "--tdd-test-glob", "tests/*", "--tdd-source-glob", "calc.py"]
    assert "--tdd-test-glob" in refused("--tdd", "--tdd-source-glob", "calc.py")
    assert "--tdd-source-glob" in refused("--tdd", "--tdd-test-glob", "tests/*")
    assert "--test-cmd" in refused(*base, "--auto-test")
    assert "--auto-test" in refused(*base, "--test-cmd", "python -m pytest")
    assert "pytest" in refused(*base, "--auto-test", "--test-cmd", "npm test")
    assert "architect" in refused(
        *base, "--auto-test", "--test-cmd", "pytest", edit_format="architect"
    )


def test_main_wires_hooks_only_when_enabled_and_refuses_bad_setup(project, monkeypatch):
    from aider import main as main_module

    clean_env(monkeypatch)
    monkeypatch.setattr(main_module, "check_version", lambda *a, **kw: None)
    monkeypatch.setenv("AIDER_ANALYTICS", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "deadbeef")
    home = project / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    base = [
        "--model",
        "gpt-3.5-turbo",
        "--yes",
        "--no-check-update",
        "--no-show-release-notes",
        "--no-analytics",
        "--no-stream",
        "--map-tokens",
        "0",
        "calc.py",
    ]
    coder = main_module.main(base, return_coder=True)
    assert (coder.response_hooks, coder.edit_hooks, coder.test_result_hooks) == ((), (), ())

    tdd = [
        "--tdd",
        "--tdd-test-glob",
        "tests/*",
        "--tdd-source-glob",
        "calc.py",
        "--auto-test",
        "--test-cmd",
        "python -m pytest -q",
    ]
    coder = main_module.main(base + tdd, return_coder=True)
    assert len(coder.response_hooks) == len(coder.edit_hooks) == len(coder.test_result_hooks) == 1
    assert isinstance(coder.response_hooks[0], TddController)
    assert coder.response_hooks[0].citation_validator is None

    coder_with_oracle = main_module.main(
        base + tdd + ["--tdd-doc-glob", "docs/*", "--oracle"], return_coder=True
    )
    tdd_hook = coder_with_oracle.response_hooks[0]
    assert isinstance(tdd_hook, TddController)
    assert tdd_hook.config.doc_globs == ("docs/*",)
    assert tdd_hook.citation_validator == coder_with_oracle.oracle_provider.unknown_cited_ids

    coder = main_module.main(base + tdd + ["--no-tdd"], return_coder=True)
    assert coder.edit_hooks == ()

    assert main_module.main(base + ["--tdd"], return_coder=True) == 1


def test_completion_requires_doc_glob_and_rejects_unknown_citations_end_to_end(project):
    (project / "docs").mkdir()
    (project / "docs" / "guide.md").write_text("# Guide\n")
    repo = git.Repo(project)
    repo.index.add(["docs/guide.md"])
    repo.index.commit("Add guide")
    known_id = "4d4d418905639aeba3a03277"
    unknown_id = "deadbeefdeadbeefdeadbeef"
    controller = TddController(
        TddConfig(
            test_globs=("tests/*",),
            source_globs=("calc.py",),
            doc_globs=("docs/*",),
        ),
        citation_validator=lambda text: (unknown_id,) if unknown_id in text else (),
    )
    coder = Coder.create(
        main_model=Model("gpt-3.5-turbo"),
        edit_format="diff",
        fnames=["calc.py", "tests/test_calc.py", "docs/guide.md"],
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        auto_commits=False,
        auto_lint=False,
        auto_test=True,
        test_cmd=TEST_CMD,
        stream=False,
        map_tokens=0,
        detect_urls=False,
        **controller.hook_kwargs(),
    )
    coder.max_reflections = 6
    test_edit = (
        "tests/test_calc.py\n```python\n<<<<<<< SEARCH\n    assert add(0, 0) == 0\n=======\n"
        "    assert add(0, 0) == 0\n\n\ndef test_add_two():\n    assert add(1, 2) == 3\n"
        ">>>>>>> REPLACE\n```\n"
    )
    src_edit = (
        "calc.py\n```python\n<<<<<<< SEARCH\n    return a\n=======\n"
        "    return a + b\n>>>>>>> REPLACE\n```\n"
    )
    doc_edit = (
        "docs/guide.md\n```markdown\n<<<<<<< SEARCH\n# Guide\n=======\n"
        "# Guide\nUpdated add.\n>>>>>>> REPLACE\n```\n"
    )
    sends = fake_send(
        coder,
        [
            test_edit,
            src_edit,
            f"Final report citing {unknown_id} before updating docs.",
            doc_edit,
            f"Final report citing {known_id}.",
        ],
    )
    coder.run("make add sum and document it")
    assert len(sends) == 5
    # Green continuation (sends[2]) includes before/after commands and outcomes
    assert "Before (observed red):" in sends[2]
    assert "After (observed green):" in sends[2]
    # Unmet requirements continuation (sends[3]) flags both untouched docs and fabricated ID
    assert "Unmet requirements" in sends[3]
    assert "docs/*" in sends[3]
    assert unknown_id in sends[3]
    assert controller.state.phase is Phase.DONE
    assert read(project, "docs/guide.md") == "# Guide\nUpdated add.\n"
