"""Integration tests for local callers backends, read-range, and project facts wiring."""

import importlib
import json
from pathlib import Path
import runpy
import sys
from unittest.mock import Mock

import git
import pytest

from aider.args import get_parser
from aider.caller_audit import (
    CallerContractHook,
    IsolatedCallerAuditor,
    ProvenanceEvidence,
    normalize_supplementary_evidence,
)
from aider.coders import Coder
from aider.commands import SwitchCoder
from aider.hooks import ResponseHookResult
from aider.io import InputOutput
from aider.local_callers import CallerExcerpt
from aider.main import (
    LocalReadRangeReader,
    compose_local_context_kwargs,
    create_local_caller_finder,
    create_read_range_reader,
)
from aider.models import Model


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


class FakeCymbalRunner:
    """Deterministic runner aligned with aider.cymbal_callers.CymbalCommandRunner."""

    def __init__(
        self,
        *,
        version="cymbal v0.18.0\n",
        refs=None,
        impact=None,
        changed=None,
        show=None,
        error=None,
    ):
        self.version = version
        self.refs = refs if refs is not None else {}
        self.impact = impact if impact is not None else {}
        self.changed = changed if changed is not None else {"changed": [], "stale": False}
        self.show = show if show is not None else {}
        self.error = error
        self.calls = []

    def run(self, argv, *, cwd, timeout_seconds, max_output_bytes):
        from aider.cymbal_callers import CommandResult

        argv_tuple = tuple(argv)
        self.calls.append((argv_tuple, cwd, timeout_seconds, max_output_bytes))
        if self.error is not None:
            raise self.error
        if len(argv_tuple) >= 2 and argv_tuple[1] == "--version":
            return CommandResult(
                argv=argv_tuple,
                returncode=0,
                stdout=self.version.encode("utf-8"),
                stderr=b"",
            )
        subcmd = argv_tuple[1] if len(argv_tuple) >= 2 else ""
        if subcmd == "changed":
            payload = self.changed() if callable(self.changed) else self.changed
        else:
            target = argv_tuple[-1]
            if subcmd == "refs":
                payload = (
                    self.refs(target)
                    if callable(self.refs)
                    else self.refs.get(target, {"references": []})
                )
            elif subcmd == "impact":
                payload = (
                    self.impact(target)
                    if callable(self.impact)
                    else self.impact.get(target, {"direct_callers": []})
                )
            elif subcmd == "show":
                payload = self.show(target) if callable(self.show) else self.show.get(target, {})
            else:
                return CommandResult(
                    argv=argv_tuple,
                    returncode=2,
                    stdout=b"",
                    stderr=f"unknown subcommand: {subcmd}".encode("utf-8"),
                )
        return CommandResult(
            argv=argv_tuple,
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )


class EmptyCallerFinder:
    """Minimal CallerFinder test double returning no caller excerpts."""

    def supports(self, path: str) -> bool:
        return True

    def find_callers(
        self,
        root,
        message,
        editable,
        candidates,
        *,
        max_files=3,
        max_chars=700,
        context_lines=3,
        report=None,
    ):
        return ()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("webbrowser.open", Mock())
    monkeypatch.setattr("aider.repo.GitRepo.get_commit_message", lambda *a, **kw: "test commit")
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Integration test")
        config.set_value("user", "email", "integration-test@example.invalid")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pricing.py").write_text(
        "def format_price(cents):\n    return f'{cents / 100:.2f}'\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "consumer.py").write_text(
        "from pricing import format_price\n"
        "raw = format_price(1050)\n"
        "assert raw == '10.50'\n",
        encoding="utf-8",
    )
    repo.index.add(["pricing.py", "pkg/consumer.py"])
    repo.index.commit("Initial commit")
    return tmp_path


def make_coder(**kwargs):
    kwargs.setdefault("auto_commits", True)
    kwargs.setdefault("main_model", Model("gpt-3.5-turbo"))
    return Coder.create(
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


# --- CLI flags & .gitignore ---


def test_cli_flags_defaults_and_opt_in_values():
    parser = get_parser([], None)

    defaults = parser.parse_args([])
    assert defaults.local_callers_backend == "builtin"
    assert defaults.local_callers_read_range is False
    assert defaults.facts is False
    assert defaults.facts_file is None
    assert defaults.facts_db is None

    configured = parser.parse_args(
        [
            "--local-callers",
            "--local-callers-backend",
            "auto",
            "--local-callers-read-range",
            "--facts",
            "--facts-file",
            "custom/facts.jsonl",
            "--facts-db",
            "custom/facts.sqlite3",
        ]
    )
    assert configured.local_callers is True
    assert configured.local_callers_backend == "auto"
    assert configured.local_callers_read_range is True
    assert configured.facts is True
    assert configured.facts_file == "custom/facts.jsonl"
    assert configured.facts_db == "custom/facts.sqlite3"

    url_args = parser.parse_args(["--facts", "--facts-db", "sqlite:///tmp/facts.sqlite3"])
    assert url_args.facts is True
    assert url_args.facts_db == "sqlite:///tmp/facts.sqlite3"

    cymbal_args = parser.parse_args(["--local-callers-backend", "cymbal", "--no-facts"])
    assert cymbal_args.local_callers_backend == "cymbal"
    assert cymbal_args.facts is False

    with pytest.raises(SystemExit):
        parser.parse_args(["--local-callers-backend", "unknown"])


def test_gitignore_covers_cymbal_and_facts_db():
    gitignore = Path(__file__).resolve().parents[2] / ".gitignore"
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    assert ".cymbal/" in lines
    assert ".aider.facts.jsonl" in lines
    assert ".aider.facts.sqlite3" in lines
    assert ".aider.facts.sqlite3-journal" in lines
    assert ".aider.facts.sqlite3-wal" in lines
    assert ".aider.facts.sqlite3-shm" in lines


# --- Lazy imports & zero I/O when disabled ---


def test_disabled_features_perform_no_io_and_keep_optional_imports_lazy(project, monkeypatch):
    parser = get_parser([], str(project))
    db_path = project / "should_not_be_created.sqlite3"
    jsonl_path = project / "should_not_be_created.jsonl"
    args = parser.parse_args(
        ["--no-facts", "--facts-db", str(db_path), "--facts-file", str(jsonl_path)]
    )

    optional_mods = ("aider.project_facts", "aider.read_range", "aider.cymbal_callers")
    for mod_name in optional_mods:
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    kwargs = compose_local_context_kwargs(args, root=project)
    assert kwargs["local_callers_backend"] == "builtin"
    assert kwargs["local_callers_read_range"] is False
    assert kwargs["facts"] is False
    assert "facts_store" not in kwargs
    assert "read_range_reader" not in kwargs
    assert "local_caller_finder" not in kwargs
    assert not db_path.exists()
    assert not jsonl_path.exists()
    assert not (project / ".aider.facts.jsonl").exists()
    assert not (project / ".aider.facts.sqlite3").exists()

    coder = make_coder(**kwargs)
    assert coder.facts_enabled is False
    assert coder.facts_store is None
    assert coder.read_range_reader is None
    assert coder.local_caller_finder is None
    assert not db_path.exists()
    assert not jsonl_path.exists()
    assert not (project / ".aider.facts.jsonl").exists()
    assert not (project / ".aider.facts.sqlite3").exists()
    for mod_name in optional_mods:
        assert mod_name not in sys.modules


# --- Real factory composition & backend modes ---


def test_composition_wires_real_cymbal_read_range_and_facts_factories(project):
    from aider.cymbal_callers import FallbackCallerFinder
    from aider.project_facts import (
        DEFAULT_FACTS_FILENAME,
        DEFAULT_FACTS_JSONL_FILENAME,
        ProjectFactStore,
        chat_scope_id,
    )

    parser = get_parser([], str(project))
    default_args = parser.parse_args(
        [
            "--local-callers",
            "--local-callers-backend",
            "cymbal",
            "--local-callers-read-range",
            "--facts",
        ]
    )
    kwargs = compose_local_context_kwargs(default_args, root=project)
    try:
        finder = kwargs["local_caller_finder"]
        reader = kwargs["read_range_reader"]
        store = kwargs["facts_store"]

        assert isinstance(finder, FallbackCallerFinder)
        assert finder.backend == "cymbal"
        assert isinstance(reader, LocalReadRangeReader)
        assert reader.backend == "cymbal"
        assert reader.cymbal_client is finder.cymbal_finder.client
        assert isinstance(store, ProjectFactStore)
        assert store.active is True
        assert store.backend == "jsonl"
        assert store.read_only is False
        expected_jsonl = (project / DEFAULT_FACTS_JSONL_FILENAME).resolve()
        assert store.storage_path == expected_jsonl
        assert store.db_path == expected_jsonl
        assert store.chat_id == chat_scope_id(project, default_args.chat_history_file)
        assert Path(kwargs["facts_file"]) == expected_jsonl
        assert kwargs["facts_db"] is None
        assert expected_jsonl.exists()
        assert not (project / DEFAULT_FACTS_FILENAME).exists()
    finally:
        kwargs["facts_store"].close()

    custom_jsonl_args = parser.parse_args(["--facts", "--facts-file", "state/custom.jsonl"])
    custom_jsonl_kwargs = compose_local_context_kwargs(custom_jsonl_args, root=project)
    try:
        expected_custom_jsonl = (project / "state/custom.jsonl").resolve()
        assert custom_jsonl_kwargs["facts_store"].backend == "jsonl"
        assert custom_jsonl_kwargs["facts_store"].storage_path == expected_custom_jsonl
        assert Path(custom_jsonl_kwargs["facts_file"]) == expected_custom_jsonl
        assert expected_custom_jsonl.exists()
    finally:
        custom_jsonl_kwargs["facts_store"].close()

    custom_args = parser.parse_args(["--facts", "--facts-db", "state/custom.sqlite3"])
    custom_kwargs = compose_local_context_kwargs(custom_args, root=project)
    try:
        expected_custom = (project / "state/custom.sqlite3").resolve()
        assert custom_kwargs["facts_store"].backend == "sqlite"
        assert custom_kwargs["facts_store"].db_path == expected_custom
        assert Path(custom_kwargs["facts_db"]) == expected_custom
        assert expected_custom.exists()
    finally:
        custom_kwargs["facts_store"].close()

    url_target = (project / "state" / "url_facts.sqlite3").resolve()
    url_str = f"sqlite:///{url_target.as_posix().lstrip('/')}"
    url_args = parser.parse_args(["--facts", "--facts-db", url_str])
    url_kwargs = compose_local_context_kwargs(url_args, root=project)
    try:
        assert url_kwargs["facts_store"].backend == "sqlite"
        assert url_kwargs["facts_store"].storage_path == url_target
        assert Path(url_kwargs["facts_db"]) == url_target
        assert url_target.exists()
    finally:
        url_kwargs["facts_store"].close()


def test_auto_backend_falls_back_with_diagnostics_and_cymbal_mode_errors_explicitly(
    project, monkeypatch
):
    from aider.cymbal_callers import CymbalNotFoundError

    failing_runner = FakeCymbalRunner(error=CymbalNotFoundError("cymbal binary not found"))

    auto_finder = create_local_caller_finder("auto", runner=failing_runner)
    res = auto_finder.lookup(project, "fix format_price", ["pricing.py"], ["pkg/consumer.py"])
    assert res.source == "builtin"
    assert res.fallback_used is True
    assert res.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in res.excerpts] == ["pkg/consumer.py"]
    assert any("cymbal fallback (not_found)" in d for d in res.diagnostics)

    # Through Coder.run in auto mode: emits diagnostic warning and still completes audit.
    auto_coder = make_coder(
        local_callers=True,
        local_callers_backend="auto",
        local_callers_audit=True,
        local_caller_finder=create_local_caller_finder("auto", runner=failing_runner),
    )
    warnings = []
    auto_coder.io.tool_warning = lambda *a, **kw: warnings.append(" ".join(str(x) for x in a))
    monkeypatch.setattr(
        auto_coder.local_caller_auditor, "_complete_fn", lambda msgs: "VERDICT: PASS"
    )
    fake_send(auto_coder, [EDIT_BLOCK])
    repo = git.Repo(project)
    head = repo.head.commit
    auto_coder.run("update format_price")
    assert any("cymbal fallback" in w for w in warnings)
    assert repo.head.commit != head

    # Explicit cymbal mode raises directly in finder and never falls back in Coder.run.
    cymbal_finder = create_local_caller_finder("cymbal", runner=failing_runner)
    with pytest.raises(CymbalNotFoundError):
        cymbal_finder.lookup(project, "fix format_price", ["pricing.py"], ["pkg/consumer.py"])

    cymbal_coder = make_coder(
        local_callers=True,
        local_callers_backend="cymbal",
        local_callers_audit=True,
        local_callers_policy="strict",
        local_caller_finder=cymbal_finder,
    )
    errors = []
    cymbal_coder.io.tool_error = lambda *a, **kw: errors.append(" ".join(str(x) for x in a))
    audit_backend = Mock(return_value="VERDICT: PASS")
    monkeypatch.setattr(cymbal_coder.local_caller_auditor, "_complete_fn", audit_backend)
    fake_send(cymbal_coder, [EDIT_BLOCK.replace("f'${", "f'EUR {")])
    head_before = repo.head.commit
    cymbal_coder.run("update format_price")
    assert any("cymbal" in err.lower() for err in errors)
    assert repo.head.commit == head_before
    audit_backend.assert_not_called()


# --- Propagation through coder switches ---


def test_adapters_and_flags_propagate_through_coder_switches(project):
    from aider.cymbal_callers import build_caller_finder
    from aider.project_facts import FakeFactRepository, open_project_fact_store

    finder = build_caller_finder(backend="cymbal")
    reader = create_read_range_reader(
        project, backend="cymbal", cymbal_client=finder.cymbal_finder.client
    )
    store = open_project_fact_store(project, enabled=True, repository=FakeFactRepository())
    store.record_fact("prices omit currency symbols", source_paths=("pricing.py",))

    coder = make_coder(
        local_callers=True,
        local_callers_backend="cymbal",
        local_callers_read_range=True,
        local_caller_finder=finder,
        read_range_reader=reader,
        facts=True,
        facts_db=str(project / ".aider.facts.sqlite3"),
        facts_file=str(project / ".aider.facts.jsonl"),
        chat_history_file=str(project / "custom.chat.history.md"),
        facts_store=store,
    )
    coder.commands.cmd_read_range("pkg/consumer.py:1-3")
    assert len(coder.read_only_ranges) == 1

    switched = Coder.create(from_coder=coder, edit_format="ask")
    assert switched.local_callers is True
    assert switched.local_callers_backend == "cymbal"
    assert switched.local_callers_read_range is True
    assert switched.local_caller_finder is finder
    assert switched.read_range_reader is reader
    assert switched.facts_enabled is True
    assert switched.facts_db == str(project / ".aider.facts.sqlite3")
    assert switched.facts_file == str(project / ".aider.facts.jsonl")
    assert switched.chat_history_file == str(project / "custom.chat.history.md")
    assert switched.facts_store is store
    assert len(switched.read_only_ranges) == 1

    with pytest.raises(SwitchCoder) as exc_info:
        switched.commands.cmd_chat_mode("diff")
    restored = Coder.create(from_coder=switched, **exc_info.value.kwargs)
    assert restored.local_caller_finder is finder
    assert restored.read_range_reader is reader
    assert restored.facts_store is store
    assert restored.facts_file == str(project / ".aider.facts.jsonl")
    assert restored.chat_history_file == str(project / "custom.chat.history.md")
    assert restored.facts_enabled is True
    assert restored.local_callers_backend == "cymbal"


# --- Real /read-range and /fact commands ---


def test_cmd_read_range_uses_real_read_range_and_is_read_only(project):
    reader = create_read_range_reader(project)
    coder = make_coder(local_callers_read_range=True, read_range_reader=reader)
    outputs = []
    errors = []
    coder.io.tool_output = lambda *a, **kw: outputs.append(" ".join(str(x) for x in a))
    coder.io.tool_error = lambda *a, **kw: errors.append(" ".join(str(x) for x in a))

    assert "/read-range" in coder.commands.get_commands()
    coder.commands.cmd_read_range("pkg/consumer.py:2-3")
    coder.commands.cmd_read_range("pkg/consumer.py 1-2")
    coder.commands.cmd_read_range("pkg/consumer.py 1 3")

    assert not errors
    # File must never be promoted to editable or full read-only files.
    assert coder.get_inchat_relative_files() == ["pricing.py"]
    assert coder.abs_read_only_fnames == set()
    assert any(
        "provenance=read-range" in line
        and "pkg/consumer.py:2-3" in line
        and "2: raw = format_price(1050)" in line
        for line in outputs
    )

    ro_msgs = coder.get_readonly_files_messages()
    assert any(
        "provenance=read-range" in m["content"]
        for m in ro_msgs
        if isinstance(m["content"], str)
    )

    coder.commands.cmd_read_range("pkg/consumer.py 5-2")
    coder.commands.cmd_read_range("../outside.py:1-2")
    assert len(errors) == 2


def test_cmd_fact_disabled_unavailable_and_read_write_lifecycle(project, monkeypatch):
    # 1. Disabled by default: /fact errors and creates neither JSONL nor SQLite file.
    disabled_coder = make_coder(facts=False)
    disabled_errors = []
    disabled_coder.io.tool_error = lambda *a, **kw: disabled_errors.append(
        " ".join(str(x) for x in a)
    )
    assert "/fact" in disabled_coder.commands.get_commands()
    disabled_coder.commands.cmd_fact("list")
    disabled_coder.commands.cmd_fact("-f pricing.py format_price returns plain decimal")
    assert len(disabled_errors) == 2
    assert all("disabled" in msg.lower() for msg in disabled_errors)
    assert not (project / ".aider.facts.jsonl").exists()
    assert not (project / ".aider.facts.sqlite3").exists()

    # 2. Explicit --facts-db degrades when sqlite3 is unavailable or URL scheme is unsupported,
    # creating no DB and no fallback JSONL; meanwhile default --facts (JSONL) still works!
    import aider.project_facts as pf

    with monkeypatch.context() as mp:
        mp.setattr(pf, "sqlite3", None)
        io_warnings = []
        io = InputOutput(yes=True, pretty=False, fancy_input=False)
        io.tool_warning = lambda *a, **kw: io_warnings.append(" ".join(str(x) for x in a))
        parser = get_parser([], str(project))
        args = parser.parse_args(["--facts", "--facts-db", "custom.sqlite3"])
        unavail_kwargs = compose_local_context_kwargs(args, root=project, io=io)
        assert unavail_kwargs["facts_store"].active is False
        assert "sqlite3" in unavail_kwargs["facts_store"].diagnostic.lower()
        assert any("sqlite3" in w.lower() for w in io_warnings)
        assert not (project / "custom.sqlite3").exists()
        assert not (project / ".aider.facts.jsonl").exists()

        unavail_coder = make_coder(**unavail_kwargs)
        unavail_errors = []
        unavail_coder.io.tool_error = lambda *a, **kw: unavail_errors.append(
            " ".join(str(x) for x in a)
        )
        unavail_coder.commands.cmd_fact("format_price returns plain decimal")
        assert any("sqlite3" in e.lower() for e in unavail_errors)

        bad_url_warnings = []
        bad_io = InputOutput(yes=True, pretty=False, fancy_input=False)
        bad_io.tool_warning = lambda *a, **kw: bad_url_warnings.append(
            " ".join(str(x) for x in a)
        )
        bad_url_args = parser.parse_args(["--facts", "--facts-db", "postgresql://localhost/aider"])
        bad_url_kwargs = compose_local_context_kwargs(bad_url_args, root=project, io=bad_io)
        assert bad_url_kwargs["facts_store"].active is False
        assert any("scheme" in w.lower() for w in bad_url_warnings)
        assert not (project / ".aider.facts.jsonl").exists()

        # Default --facts uses JSONL and succeeds even when sqlite3 is None!
        jsonl_no_sqlite_args = parser.parse_args(["--facts"])
        jsonl_no_sqlite_kwargs = compose_local_context_kwargs(
            jsonl_no_sqlite_args, root=project
        )
        try:
            assert jsonl_no_sqlite_kwargs["facts_store"].active is True
            assert jsonl_no_sqlite_kwargs["facts_store"].backend == "jsonl"
            assert (project / ".aider.facts.jsonl").exists()
        finally:
            jsonl_no_sqlite_kwargs["facts_store"].close()

    # 3. Enabled default JSONL store: read/write /fact, chat_id, candidate isolation, staleness.
    parser = get_parser([], str(project))
    args = parser.parse_args(["--facts"])
    kwargs = compose_local_context_kwargs(args, root=project)
    coder = make_coder(**kwargs)
    try:
        assert coder.facts_store.backend == "jsonl"
        outputs = []
        errors = []
        coder.io.tool_output = lambda *a, **kw: outputs.append(" ".join(str(x) for x in a))
        coder.io.tool_error = lambda *a, **kw: errors.append(" ".join(str(x) for x in a))

        coder.commands.cmd_fact(
            "-f pricing.py -t pricing format_price must never prepend a currency symbol"
        )
        coder.commands.cmd_fact("add -f pricing.py format_price rounds to 2 decimal places")
        assert not errors
        assert sum("Recorded confirmed project fact" in line for line in outputs) == 2

        outputs.clear()
        coder.commands.cmd_fact("")
        assert any("never prepend a currency symbol" in line for line in outputs)
        assert any("rounds to 2 decimal places" in line for line in outputs)

        outputs.clear()
        coder.commands.cmd_fact("search rounds")
        assert any("rounds to 2 decimal places" in line for line in outputs)

        recorded = coder.facts_store.search("currency")
        assert len(recorded) == 1
        fact_id = recorded[0].fact_id
        assert recorded[0].provenance.chat_id == coder.facts_store.chat_id

        outputs.clear()
        coder.commands.cmd_fact(f"show {fact_id}")
        assert any(
            fact_id in line and "never prepend a currency symbol" in line for line in outputs
        )

        # Unverified model candidates stay excluded from prompt context until explicitly confirmed.
        cand = coder.facts_store.record_candidate(
            "unverified model speculation about pricing",
            source_paths=("pricing.py",),
        )
        prompt_ctx = coder.facts_store.format_prompt_context(
            query="pricing", paths=("pricing.py",)
        )
        assert "never prepend a currency symbol" in prompt_ctx
        assert f"chat={coder.facts_store.chat_id}" in prompt_ctx
        assert "unverified model speculation" not in prompt_ctx

        coder.commands.cmd_fact(f"confirm {cand.fact_id}")
        confirmed_ctx = coder.facts_store.format_prompt_context(
            query="speculation", paths=("pricing.py",)
        )
        assert "unverified model speculation" in confirmed_ctx

        # Source file modification marks dependent facts stale and excludes them from
        # prompt context.
        (project / "pricing.py").write_text(
            "def format_price(cents):\n    return f'{cents / 100:.3f}'\n",
            encoding="utf-8",
        )
        stale_ctx = coder.facts_store.format_prompt_context(
            query="currency", paths=("pricing.py",)
        )
        assert stale_ctx == ""
    finally:
        coder.facts_store.close()


# --- Automatic call-site ranges (native + validated Cymbal show) ---


def test_automatic_call_site_ranges_native_and_validated_cymbal_show(project, monkeypatch):
    from aider.cymbal_callers import build_caller_finder

    repo = git.Repo(project)
    long_consumer = (
        "from pricing import format_price\n"
        + "".join(f"# padding line {i}\n" for i in range(2, 40))
        + "raw = format_price(1050)\n"
        + "assert raw == '10.50'\n"
    )
    (project / "pkg" / "consumer.py").write_text(long_consumer, encoding="utf-8")
    repo.index.add(["pkg/consumer.py"])
    repo.index.commit("Expand consumer")

    # 1. Native builtin caller discovery + automatic bounded call-site range.
    parser = get_parser([], str(project))
    args = parser.parse_args(["--local-callers", "--local-callers-read-range"])
    kwargs = compose_local_context_kwargs(args, root=project)
    coder = make_coder(
        local_callers=True,
        local_callers_audit=True,
        **kwargs,
    )
    audit_prompts = []
    monkeypatch.setattr(
        coder.local_caller_auditor,
        "_complete_fn",
        lambda msgs: (audit_prompts.append(msgs[1]["content"]), "VERDICT: PASS")[1],
    )
    sends = fake_send(coder, [EDIT_BLOCK])
    coder.run("update format_price")

    assert len(sends) == 1
    assert "Read-only call-site range excerpts" in sends[0]
    assert "40: raw = format_price(1050)" in sends[0]
    assert coder.get_inchat_relative_files() == ["pricing.py"]
    assert coder.abs_read_only_fnames == set()
    assert len(audit_prompts) == 1
    assert "provenance=read-range" in audit_prompts[0]
    assert "40: raw = format_price(1050)" in audit_prompts[0]

    # Reset pricing.py back for Cymbal show test.
    (project / "pricing.py").write_text(
        "def format_price(cents):\n    return f'{cents / 100:.2f}'\n",
        encoding="utf-8",
    )
    repo.index.add(["pricing.py"])
    repo.index.commit("Reset pricing")

    # 2. Cymbal backend: validated Cymbal show
    # (fresh body -> [cymbal]; stale body -> disk + cymbal_stale).
    fresh_runner = FakeCymbalRunner(
        refs={
            "format_price": {
                "definitions": [{"file": "pricing.py", "line": 1, "name": "format_price"}],
                "references": [
                    {
                        "file": "pkg/consumer.py",
                        "line": 40,
                        "kind": "call",
                        "snippet": "raw = format_price(1050)",
                    }
                ],
            }
        },
        impact={"format_price": {"direct_callers": []}},
        show={
            "pkg/consumer.py:40": {
                "file": "pkg/consumer.py",
                "start_line": 39,
                "end_line": 41,
                "symbol": "format_price",
                "code": "# padding line 39\nraw = format_price(1050)\nassert raw == '10.50'",
            }
        },
    )
    cymbal_finder = build_caller_finder(backend="cymbal", runner=fresh_runner)
    cymbal_coder = make_coder(
        local_callers=True,
        local_callers_backend="cymbal",
        local_callers_read_range=True,
        local_caller_finder=cymbal_finder,
    )
    cymbal_sends = fake_send(cymbal_coder, ["No edits needed."])
    cymbal_coder.run("inspect format_price")
    assert any(argv[1] == "show" for argv, *_ in fresh_runner.calls)
    assert "[cymbal]" in cymbal_sends[0]
    assert "40: raw = format_price(1050)" in cymbal_sends[0]

    stale_runner = FakeCymbalRunner(
        refs=fresh_runner.refs,
        impact=fresh_runner.impact,
        show={
            "pkg/consumer.py:40": {
                "file": "pkg/consumer.py",
                "start_line": 39,
                "end_line": 41,
                "symbol": "format_price",
                "code": "# STALE INDEX BODY\nraw = old_call()\nassert True",
            }
        },
    )
    stale_coder = make_coder(
        local_callers=True,
        local_callers_backend="cymbal",
        local_callers_read_range=True,
        local_caller_finder=build_caller_finder(backend="cymbal", runner=stale_runner),
    )
    stale_sends = fake_send(stale_coder, ["No edits needed."])
    stale_coder.run("inspect format_price")
    assert "cymbal_stale" in stale_sends[0]
    assert "40: raw = format_price(1050)" in stale_sends[0]


# --- Normal entry-point real factories end-to-end & strict verifier incompleteness ---


def test_normal_entry_point_real_factories_end_to_end_and_incompleteness_preservation(
    project, monkeypatch
):
    from aider.cymbal_callers import build_caller_finder

    parser = get_parser([], str(project))
    args = parser.parse_args(
        ["--local-callers", "--local-callers-read-range", "--facts"]
    )
    kwargs = compose_local_context_kwargs(args, root=project)
    coder = make_coder(
        local_callers=True,
        local_callers_mode="passive",
        local_callers_audit=True,
        local_callers_policy="strict",
        **kwargs,
    )
    try:
        coder.commands.cmd_fact("-f pricing.py format_price returns plain decimal strings")
        coder.commands.cmd_read_range("pkg/consumer.py:1-3")

        captured_audit_prompts = []

        def fake_audit(messages):
            captured_audit_prompts.append(messages)
            return "VERDICT: PASS"

        monkeypatch.setattr(coder.local_caller_auditor, "_complete_fn", fake_audit)
        sends = fake_send(coder, [EDIT_BLOCK])

        repo = git.Repo(project)
        head = repo.head.commit
        coder.run("update format_price")

        # Validated facts from format_prompt_context appeared in editor prompt.
        assert len(sends) == 1
        assert "Read-only project facts for this worktree:" in sends[0]
        assert "format_price returns plain decimal strings" in sends[0]
        assert "pricing.py@" in sends[0]

        # Auditor received caller snippets + provenance-labeled facts & read-ranges + diff.
        assert len(captured_audit_prompts) == 1
        audit_user_msg = captured_audit_prompts[0][1]["content"]
        assert "## Caller snippets" in audit_user_msg
        assert "## Supplementary evidence" in audit_user_msg
        assert "provenance=project-fact" in audit_user_msg
        assert "format_price returns plain decimal strings" in audit_user_msg
        assert "provenance=read-range" in audit_user_msg
        assert "pkg/consumer.py:1-3" in audit_user_msg
        assert "## Proposed diff" in audit_user_msg

        # Complete caller evidence + PASS verdict -> committed.
        assert repo.head.commit != head
        assert not repo.is_dirty()

        # Now verify Cymbal incompleteness (e.g. stale out-of-range hit alongside a valid hit)
        # is preserved onto excerpts so strict verifier returns UNKNOWN and blocks commit.
        (project / "pricing.py").write_text(
            "def format_price(cents):\n    return f'{cents / 100:.2f}'\n",
            encoding="utf-8",
        )
        (project / "pkg" / "other.py").write_text("x = 1\n", encoding="utf-8")
        repo.index.add(["pricing.py", "pkg/other.py"])
        head_inc = repo.index.commit("Prepare incompleteness test")

        incomplete_runner = FakeCymbalRunner(
            refs={
                "format_price": {
                    "definitions": [{"file": "pricing.py", "line": 1, "name": "format_price"}],
                    "references": [
                        {
                            "file": "pkg/consumer.py",
                            "line": 2,
                            "kind": "call",
                            "snippet": "raw = format_price(1050)",
                        },
                        {
                            "file": "pkg/other.py",
                            "line": 999,
                            "kind": "call",
                            "snippet": "format_price(999)",
                        },
                    ],
                }
            },
            impact={"format_price": {"direct_callers": []}},
        )
        incomplete_coder = make_coder(
            local_callers=True,
            local_callers_backend="cymbal",
            local_callers_audit=True,
            local_callers_policy="strict",
            local_caller_finder=build_caller_finder(backend="cymbal", runner=incomplete_runner),
            facts=True,
            facts_store=coder.facts_store,
        )
        strict_audit_backend = Mock(return_value="VERDICT: PASS")
        monkeypatch.setattr(
            incomplete_coder.local_caller_auditor, "_complete_fn", strict_audit_backend
        )
        fake_send(incomplete_coder, [EDIT_BLOCK])

        incomplete_coder.run("update format_price")
        assert incomplete_coder._last_caller_result.status == "INCOMPLETE"
        assert repo.head.commit == head_inc
        assert repo.is_dirty()
        strict_audit_backend.assert_not_called()
    finally:
        coder.facts_store.close()


# --- Bounded provenance-labeled evidence & strict precommit semantics ---


def test_supplementary_evidence_is_bounded_and_provenance_labeled(project):
    from aider.project_facts import FakeFactRepository, open_project_fact_store

    store = open_project_fact_store(project, enabled=True, repository=FakeFactRepository())
    huge_facts = [
        store.record_fact(
            f"rule {i}: " + ("x" * 1000),
            source_paths=("pricing.py",),
            fact_id=f"fact_{i}",
        )
        for i in range(12)
    ]
    bounded = normalize_supplementary_evidence(
        huge_facts,
        default_kind="project-fact",
        max_items=5,
        max_item_chars=120,
        max_total_chars=500,
    )
    assert 1 <= len(bounded) <= 5
    rendered = "\n\n".join(item.format() for item in bounded)
    assert len(rendered) <= 500
    assert all(isinstance(item, ProvenanceEvidence) for item in bounded)
    assert all(item.kind == "project-fact" for item in bounded)
    assert all(item.truncated for item in bounded)
    assert "[provenance=project-fact source=fact_0] [TRUNCATED]" in rendered


def test_facts_never_make_missing_caller_evidence_pass_and_strict_precommit_preserved(
    project, monkeypatch
):
    from aider.project_facts import FakeFactRepository, open_project_fact_store

    store = open_project_fact_store(project, enabled=True, repository=FakeFactRepository())
    store.record_fact(
        "format_price returns plain decimal strings without '$'",
        source_paths=("pricing.py",),
        fact_id="currency-rule",
    )
    empty_finder = EmptyCallerFinder()
    reader = create_read_range_reader(project)
    coder = make_coder(
        local_callers=True,
        local_callers_mode="contract",
        local_callers_audit=True,
        local_callers_policy="strict",
        local_caller_finder=empty_finder,
        local_callers_read_range=True,
        read_range_reader=reader,
        facts=True,
        facts_store=store,
    )
    coder.commands.cmd_read_range("pkg/consumer.py:1-3")

    audit_backend = Mock(return_value="VERDICT: PASS")
    monkeypatch.setattr(coder.local_caller_auditor, "_complete_fn", audit_backend)

    # Even with facts and read-range attached, contract mode MUST reject on NO_EVIDENCE
    # and strict mode must not write or commit edits.
    repo = git.Repo(project)
    head = repo.head.commit
    fake_send(
        coder,
        [
            "CALLER_CONTRACT: currency-rule says no '$'.\n"
            "TRACE: format_price(1050) -> '$10.50'.\n"
            + EDIT_BLOCK
        ],
    )
    coder.run("update format_price")

    assert repo.head.commit == head
    assert not repo.is_dirty()
    assert len(coder.local_caller_contract_hook.supplementary_evidence) >= 1
    audit_backend.assert_not_called()

    # In passive + strict audit mode, facts + read-ranges still MUST NOT allow missing
    # caller evidence to PASS or auto-commit.
    passive_coder = make_coder(
        local_callers=True,
        local_callers_mode="passive",
        local_callers_audit=True,
        local_callers_policy="strict",
        local_caller_finder=empty_finder,
        local_callers_read_range=True,
        read_range_reader=reader,
        facts=True,
        facts_store=store,
    )
    passive_coder.commands.cmd_read_range("pkg/consumer.py:1-3")
    passive_audit_backend = Mock(return_value="VERDICT: PASS")
    monkeypatch.setattr(passive_coder.local_caller_auditor, "_complete_fn", passive_audit_backend)
    fake_send(passive_coder, [EDIT_BLOCK])

    passive_coder.run("update format_price")

    assert repo.head.commit == head
    assert repo.is_dirty()
    assert len(passive_coder.local_caller_auditor.supplementary_evidence) >= 1
    passive_audit_backend.assert_not_called()


def test_facts_never_override_incomplete_caller_evidence(project):
    from aider.project_facts import FakeFactRepository, open_project_fact_store

    store = open_project_fact_store(project, enabled=True, repository=FakeFactRepository())
    fact = store.record_fact("format_price returns plain decimal", source_paths=("pricing.py",))

    backend = Mock(return_value="VERDICT: PASS")
    auditor = IsolatedCallerAuditor(backend)
    incomplete_excerpt = CallerExcerpt(
        path="pkg/consumer.py",
        start_line=1,
        symbol="format_price",
        code="raw = format_price(1050)",
        limitations=("excerpt truncated",),
    )
    auditor.begin_turn([incomplete_excerpt], facts=[fact])
    verdict = auditor.audit("+return '$10.50'")
    assert verdict.status == "UNKNOWN"
    assert "INCOMPLETE" in verdict.reason
    backend.assert_not_called()

    hook = CallerContractHook(enabled=True)
    hook.begin_turn((), facts=[fact])
    assert hook(EDIT_BLOCK) == ResponseHookResult()


# --- Regression tests: /arch handoff, /ask contract mode, /drop ranges, /read alias ---


def _script_coder_sends(monkeypatch, replies, *, coders=None):
    calls = []
    remaining = list(replies)

    def send(self, messages, model=None, functions=None):
        if coders is not None:
            coders.append(self)
        user_text = "\n".join(
            m["content"]
            for m in messages
            if m["role"] == "user" and isinstance(m["content"], str)
        )
        calls.append((self.edit_format, user_text))
        reply = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        self.partial_response_content = reply
        self.partial_response_function_call = None
        return iter(())

    monkeypatch.setattr(Coder, "send", send)
    return calls


def test_architect_handoff_contract_mode_edits_and_records_outcome(project, monkeypatch):
    from aider.project_facts import FakeFactRepository, open_project_fact_store

    store = open_project_fact_store(project, enabled=True, repository=FakeFactRepository())
    store.record_fact("format_price returns plain decimal strings", source_paths=("pricing.py",))

    coder = make_coder(
        main_model=Model("gpt-3.5-turbo", editor_edit_format="editor-diff"),
        local_callers=True,
        local_callers_mode="contract",
        local_callers_policy="strict",
        facts=True,
        facts_store=store,
        auto_accept_architect=True,
    )
    arch_reply = (
        "Update `format_price` in `pricing.py` to prepend `$`.\n"
        "```python\n"
        "return f'${cents / 100:.2f}'\n"
        "```\n"
    )
    editor_reply = (
        "CALLER_CONTRACT: pkg/consumer.py calls `raw = format_price(1050)`.\n"
        "TRACE: format_price(1050) -> '$10.50'; format_price(0) -> '$0.00'.\n"
        + EDIT_BLOCK
    )
    calls = _script_coder_sends(monkeypatch, [arch_reply, editor_reply])
    repo = git.Repo(project)
    head = repo.head.commit

    with pytest.raises(SwitchCoder) as exc_info:
        coder.commands.run("/arch update format_price")

    arch_coder = exc_info.value.kwargs["from_coder"]
    assert [fmt for fmt, _ in calls] == ["architect", "editor-diff"]
    assert "pkg/consumer.py" in calls[0][1]
    assert "Before outputting any edit block" not in calls[0][1]
    assert "format_price returns plain decimal strings" in calls[1][1]
    assert "Before outputting any edit block" in calls[1][1]
    assert "pkg/consumer.py" in calls[1][1]
    assert "raw = format_price(1050)" in calls[1][1]
    assert "${cents / 100:.2f}" in (project / "pricing.py").read_text(encoding="utf-8")
    assert repo.head.commit != head
    assert not repo.is_dirty()
    assert arch_coder.aider_edited_files == {"pricing.py"}
    assert any(
        m["content"] == "I made those changes to the files." for m in arch_coder.done_messages
    )


def test_architect_handoff_strict_contract_rejection_records_no_changes(project, monkeypatch):
    coder = make_coder(
        main_model=Model("gpt-3.5-turbo", editor_edit_format="editor-diff"),
        local_callers=True,
        local_callers_mode="contract",
        local_callers_policy="strict",
        auto_accept_architect=True,
    )
    arch_coder = Coder.create(from_coder=coder, edit_format="architect")
    calls = _script_coder_sends(
        monkeypatch,
        ["Update format_price in pricing.py to prepend '$'.", EDIT_BLOCK],
    )
    repo = git.Repo(project)
    head = repo.head.commit
    orig_text = (project / "pricing.py").read_text(encoding="utf-8")

    arch_coder.run("update format_price")

    assert calls[0][0] == "architect"
    assert any(fmt == "editor-diff" for fmt, _ in calls)
    assert (project / "pricing.py").read_text(encoding="utf-8") == orig_text
    assert repo.head.commit == head
    assert not repo.is_dirty()
    assert arch_coder.aider_edited_files == set()
    expected = (
        "No changes were made to the files because "
        "caller contract verification did not pass."
    )
    assert any(m["content"] == expected for m in arch_coder.done_messages)
    assert not any(
        m["content"] == "I made those changes to the files." for m in arch_coder.done_messages
    )


def test_architect_handoff_strict_audit_failure_reports_uncommitted(project, monkeypatch):
    coder = make_coder(
        main_model=Model("gpt-3.5-turbo", editor_edit_format="editor-diff"),
        local_callers=True,
        local_callers_mode="contract",
        local_callers_audit=True,
        local_callers_policy="strict",
        auto_accept_architect=True,
    )
    arch_coder = Coder.create(from_coder=coder, edit_format="architect")
    audit_prompts = []

    def fake_audit(self, messages):
        audit_prompts.append(messages[1]["content"])
        return "VERDICT: FAIL: consumer asserts raw == '10.50' without '$'"

    monkeypatch.setattr(Coder, "_complete_caller_audit", fake_audit)
    editor_reply = (
        "CALLER_CONTRACT: pkg/consumer.py calls `raw = format_price(1050)`.\n"
        "TRACE: format_price(1050) -> '$10.50'; format_price(0) -> '$0.00'.\n"
        + EDIT_BLOCK
    )
    _script_coder_sends(
        monkeypatch,
        [
            "Update format_price in pricing.py to prepend '$'.",
            editor_reply,
            "Cannot satisfy the consumer assertion without modifying the caller.",
        ],
    )
    repo = git.Repo(project)
    head = repo.head.commit

    arch_coder.run("update format_price")

    assert len(audit_prompts) == 1
    assert "update format_price" in audit_prompts[0]
    assert "pkg/consumer.py" in audit_prompts[0]
    assert "${cents / 100:.2f}" in (project / "pricing.py").read_text(encoding="utf-8")
    assert repo.head.commit == head
    assert repo.is_dirty()
    assert "pricing.py" in arch_coder._unreviewed_paths
    expected = (
        "I edited the files, but strict caller verification "
        "left the changes uncommitted."
    )
    assert any(m["content"] == expected for m in arch_coder.done_messages)


def test_architect_dry_run_valid_edit_preserves_file_and_reports_dry_run(project, monkeypatch):
    dry_io = InputOutput(yes=True, pretty=False, fancy_input=False, dry_run=True)
    coder = Coder.create(
        edit_format="diff",
        fnames=["pricing.py"],
        io=dry_io,
        main_model=Model("gpt-3.5-turbo", editor_edit_format="editor-diff"),
        auto_commits=True,
        auto_lint=False,
        stream=False,
        map_tokens=0,
        detect_urls=False,
        dry_run=True,
        auto_accept_architect=True,
    )
    seen_coders = []
    arch_reply = "Update `format_price` in `pricing.py` to prepend `$`."
    calls = _script_coder_sends(monkeypatch, [arch_reply, EDIT_BLOCK], coders=seen_coders)
    repo = git.Repo(project)
    head = repo.head.commit
    orig_bytes = (project / "pricing.py").read_bytes()

    with pytest.raises(SwitchCoder) as exc_info:
        coder.commands.run("/arch update format_price")

    arch_coder = exc_info.value.kwargs["from_coder"]
    editor_coder = next(c for c in seen_coders if c.edit_format == "editor-diff")
    assert [fmt for fmt, _ in calls] == ["architect", "editor-diff"]
    assert editor_coder.dry_run is True
    assert editor_coder.dry_run_edited is True
    assert (project / "pricing.py").read_bytes() == orig_bytes
    assert repo.head.commit == head
    assert not repo.is_dirty()
    assert arch_coder.aider_edited_files == set()
    assert any(
        m["content"] == "Dry run: no files were modified." for m in arch_coder.done_messages
    )
    assert not any(
        m["content"] == "I made those changes to the files." for m in arch_coder.done_messages
    )


def test_architect_strict_contract_no_callers_distinguishes_no_op_from_blocked(
    project, monkeypatch
):
    repo = git.Repo(project)
    standalone = project / "standalone.py"
    standalone.write_text("def helper():\n    return 1\n", encoding="utf-8")
    repo.index.add(["standalone.py"])
    head = repo.index.commit("Add standalone module without callers")
    orig_text = standalone.read_text(encoding="utf-8")
    standalone_edit = (
        "standalone.py\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "def helper():\n"
        "    return 1\n"
        "=======\n"
        "def helper():\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    blocked_msg = (
        "No changes were made to the files because "
        "caller contract verification did not pass."
    )

    def make_standalone_coder():
        return Coder.create(
            edit_format="diff",
            fnames=["standalone.py"],
            io=InputOutput(yes=True, pretty=False, fancy_input=False),
            main_model=Model("gpt-3.5-turbo", editor_edit_format="editor-diff"),
            auto_commits=True,
            auto_lint=False,
            stream=False,
            map_tokens=0,
            detect_urls=False,
            local_callers=True,
            local_callers_mode="contract",
            local_callers_policy="strict",
            auto_accept_architect=True,
        )

    # No-op reply ("Already correct.") with NO caller excerpts: not blocked.
    noop_coders = []
    _script_coder_sends(
        monkeypatch,
        ["Check helper in standalone.py; no edits are required.", "Already correct."],
        coders=noop_coders,
    )
    with pytest.raises(SwitchCoder) as noop_exc:
        make_standalone_coder().commands.run("/arch check helper in standalone.py")

    noop_arch = noop_exc.value.kwargs["from_coder"]
    noop_editor = next(c for c in noop_coders if c.edit_format == "editor-diff")
    assert noop_editor._local_caller_excerpts == ()
    assert noop_editor.local_caller_contract_hook.blocked is False
    assert standalone.read_text(encoding="utf-8") == orig_text
    assert repo.head.commit == head
    assert not repo.is_dirty()
    assert noop_arch.aider_edited_files == set()
    assert any(
        m["content"] == "No changes were made to the files." for m in noop_arch.done_messages
    )
    assert not any(m["content"] == blocked_msg for m in noop_arch.done_messages)

    # Edit proposal with NO caller excerpts in strict contract mode: blocked.
    blocked_coders = []
    _script_coder_sends(
        monkeypatch,
        ["Update helper in standalone.py to return 2.", standalone_edit],
        coders=blocked_coders,
    )
    with pytest.raises(SwitchCoder) as blocked_exc:
        make_standalone_coder().commands.run("/arch update helper in standalone.py")

    blocked_arch = blocked_exc.value.kwargs["from_coder"]
    blocked_editor = next(c for c in blocked_coders if c.edit_format == "editor-diff")
    assert blocked_editor._local_caller_excerpts == ()
    assert blocked_editor.local_caller_contract_hook.blocked is True
    assert standalone.read_text(encoding="utf-8") == orig_text
    assert repo.head.commit == head
    assert not repo.is_dirty()
    assert blocked_arch.aider_edited_files == set()
    assert any(m["content"] == blocked_msg for m in blocked_arch.done_messages)
    assert not any(
        m["content"] == "No changes were made to the files." for m in blocked_arch.done_messages
    )


def test_ask_mode_contract_retains_context_and_allows_fenced_code(project):
    from aider.coders import ArchitectCoder, AskCoder, ContextCoder, HelpCoder
    from aider.project_facts import FakeFactRepository, open_project_fact_store

    assert Coder.allows_edits is True
    assert AskCoder.allows_edits is False
    assert ArchitectCoder.allows_edits is False
    assert HelpCoder.allows_edits is False
    assert ContextCoder.allows_edits is False

    store = open_project_fact_store(project, enabled=True, repository=FakeFactRepository())
    store.record_fact("format_price omits currency symbols", source_paths=("pricing.py",))

    code_coder = make_coder(
        local_callers=True,
        local_callers_mode="contract",
        local_callers_policy="strict",
        facts=True,
        facts_store=store,
    )
    assert code_coder.local_caller_contract_hook.enabled is True

    ask_coder = Coder.create(from_coder=code_coder, edit_format="ask")
    assert ask_coder.allows_edits is False
    assert ask_coder.local_caller_contract_hook.enabled is False
    assert ask_coder.local_caller_contract_hook not in ask_coder.response_hooks

    fenced_reply = (
        "Example usage:\n"
        "```python\n"
        "raw = format_price(1050)\n"
        "```\n"
    )
    sends = fake_send(ask_coder, [fenced_reply])
    ask_coder.run("explain format_price")

    assert len(sends) == 1
    assert "pkg/consumer.py" in sends[0]
    assert "raw = format_price(1050)" in sends[0]
    assert "format_price omits currency symbols" in sends[0]
    assert "Before outputting any edit block" not in sends[0]
    assert ask_coder.partial_response_content == fenced_reply.strip()

    with pytest.raises(SwitchCoder) as exc_info:
        ask_coder.commands.cmd_chat_mode("code")
    restored = Coder.create(from_coder=ask_coder, **exc_info.value.kwargs)
    assert restored.allows_edits is True
    assert restored.local_callers is True
    assert restored.local_callers_mode == "contract"
    assert restored.local_caller_contract_hook.enabled is True
    assert restored.local_caller_contract_hook in restored.response_hooks


def test_drop_removes_read_only_ranges_from_prompt_and_evidence(project):
    repo = git.Repo(project)
    (project / "pkg" / "other.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    repo.index.add(["pkg/other.py"])
    repo.index.commit("Add other module")

    coder = make_coder(
        local_callers=True,
        local_callers_mode="contract",
        local_callers_audit=True,
        local_callers_read_range=False,
        read_range_reader=create_read_range_reader(project),
    )
    outputs = []
    coder.io.tool_output = lambda *a, **kw: outputs.append(" ".join(str(x) for x in a))

    coder.commands.run("/read-range pkg/consumer.py:1-3")
    assert [r.source for r in coder.read_only_ranges] == ["pkg/consumer.py:1-3"]
    drop_completions = coder.commands.completions_drop()
    assert "pkg/consumer.py" in drop_completions
    assert "pkg/consumer.py:1-3" in drop_completions

    sends = fake_send(coder, ["Checked callers.", "Second turn."])
    coder.run("inspect format_price")
    assert "provenance=read-range" in sends[0]
    assert "pkg/consumer.py:1-3" in sends[0]
    assert len(coder.local_caller_contract_hook.supplementary_evidence) == 1
    assert len(coder.local_caller_auditor.supplementary_evidence) == 1

    outputs.clear()
    coder.commands.run("/drop pkg/consumer.py")
    assert "Removed read-only range pkg/consumer.py:1-3 from the chat" in outputs
    assert coder.read_only_ranges == []
    assert coder.local_caller_contract_hook.supplementary_evidence == ()
    assert coder.local_caller_auditor.supplementary_evidence == ()

    coder.run("inspect unrelated topic")
    assert "provenance=read-range" not in sends[1]
    assert "pkg/consumer.py:1-3" not in sends[1]

    # Exact source drop removes only the targeted range.
    coder.commands.run("/read-range pkg/consumer.py:1-2")
    coder.commands.run("/read-range pkg/consumer.py:2-3")
    assert [r.source for r in coder.read_only_ranges] == [
        "pkg/consumer.py:1-2",
        "pkg/consumer.py:2-3",
    ]
    coder.commands.run("/drop pkg/consumer.py:1-2")
    assert [r.source for r in coder.read_only_ranges] == ["pkg/consumer.py:2-3"]

    # Glob drop removes all matching ranges and clears supplementary evidence.
    coder.commands.run("/read-range pkg/other.py:1-2")
    assert len(coder.read_only_ranges) == 2
    coder.commands.run("/drop pkg/*.py")
    assert coder.read_only_ranges == []
    assert coder.local_caller_contract_hook.supplementary_evidence == ()
    assert coder.local_caller_auditor.supplementary_evidence == ()


def test_read_command_alias_works_without_ambiguity(project):
    coder = make_coder(read_range_reader=create_read_range_reader(project))
    errors = []
    coder.io.tool_error = lambda *a, **kw: errors.append(" ".join(str(x) for x in a))

    assert "/read" in coder.commands.get_commands()
    assert coder.commands.get_raw_completions("/read") is not None

    coder.commands.run("/read pkg/consumer.py")
    assert not errors
    consumer_abs = coder.abs_root_path("pkg/consumer.py")
    assert consumer_abs in coder.abs_read_only_fnames

    coder.commands.run("/drop pkg/consumer.py")
    assert consumer_abs not in coder.abs_read_only_fnames

    coder.commands.run("/read-only pkg/consumer.py")
    assert not errors
    assert consumer_abs in coder.abs_read_only_fnames

    coder.commands.run("/read-range pkg/consumer.py:1-2")
    assert not errors
    assert [r.source for r in coder.read_only_ranges] == ["pkg/consumer.py:1-2"]


# --- Regression tests: file-mention exclusion for read-only references ---


class FixedCallerFinder:
    """Deterministic CallerFinder returning fixed caller excerpts."""

    def __init__(self, excerpts):
        self._excerpts = tuple(excerpts)

    def supports(self, path: str) -> bool:
        return True

    def find_callers(
        self,
        root,
        message,
        editable,
        candidates,
        *,
        max_files=3,
        max_chars=700,
        context_lines=3,
        report=None,
    ):
        return self._excerpts


def test_passive_local_callers_edit_reply_does_not_auto_add_large_consumer(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("webbrowser.open", Mock())
    monkeypatch.setattr("aider.repo.GitRepo.get_commit_message", lambda *a, **kw: "test commit")

    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Integration test")
        config.set_value("user", "email", "integration-test@example.invalid")

    (tmp_path / "api.py").write_text(
        "def normalize_code(raw):\n"
        "    cleaned = raw.strip().upper()\n"
        "    return cleaned\n",
        encoding="utf-8",
    )
    padding = "".join(
        f'PADDING_{i:04d} = "xxxxxxxxxxxxxxxxxxxxxxxxxxxx"\n' for i in range(1, 3651)
    )
    consumer_text = (
        "from api import normalize_code\n"
        + padding
        + "\n"
        + "def route_code(raw):\n"
        + "    code = normalize_code(raw)\n"
        + '    if code == "MISSING_CODE":\n'
        + '        return "fallback"\n'
        + '    return f"route:{code}"\n'
    )
    consumer_path = tmp_path / "consumer.py"
    consumer_path.write_text(consumer_text, encoding="utf-8")
    assert consumer_path.stat().st_size > 140 * 1024

    call_line = next(
        idx
        for idx, line in enumerate(consumer_text.splitlines(), 1)
        if "code = normalize_code(raw)" in line
    )
    assert call_line > 3600

    (tmp_path / "smoke.py").write_text(
        "from consumer import route_code\n"
        "\n"
        "\n"
        "def main():\n"
        '    assert route_code(" ab-1 ") == "route:AB-1"\n'
        '    assert route_code("   ") == "fallback"\n'
        '    assert route_code("") == "fallback"\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n",
        encoding="utf-8",
    )
    repo.index.add(["api.py", "consumer.py", "smoke.py"])
    repo.index.commit("Initial commit")

    excerpt = CallerExcerpt(
        path="consumer.py",
        start_line=call_line - 1,
        line=call_line,
        symbol="normalize_code",
        code=(
            "def route_code(raw):\n"
            "    code = normalize_code(raw)\n"
            '    if code == "MISSING_CODE":\n'
            '        return "fallback"\n'
            '    return f"route:{code}"'
        ),
    )
    coder = Coder.create(
        edit_format="diff",
        fnames=["api.py"],
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        main_model=Model("gpt-3.5-turbo"),
        auto_commits=True,
        auto_lint=False,
        stream=False,
        map_tokens=0,
        detect_urls=False,
        local_callers=True,
        local_callers_mode="passive",
        local_callers_read_range=True,
        local_caller_finder=FixedCallerFinder([excerpt]),
        read_range_reader=create_read_range_reader(tmp_path),
    )
    reply = (
        "In consumer.py, route_code checks normalize_code for MISSING_CODE on blank inputs.\n\n"
        "api.py\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "def normalize_code(raw):\n"
        "    cleaned = raw.strip().upper()\n"
        "    return cleaned\n"
        "=======\n"
        "def normalize_code(raw):\n"
        "    cleaned = raw.strip().upper()\n"
        '    return cleaned or "MISSING_CODE"\n'
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    sends = fake_send(coder, [reply])
    coder.run("Update normalize_code to return MISSING_CODE for blank input")

    assert coder.get_inchat_relative_files() == ["api.py"]
    assert len(sends) == 1
    assert f"{call_line}:     code = normalize_code(raw)" in sends[0]

    monkeypatch.syspath_prepend(str(tmp_path))
    for mod_name in ("api", "consumer", "smoke"):
        monkeypatch.delitem(sys.modules, mod_name, raising=False)
    try:
        runpy.run_path(str(tmp_path / "smoke.py"), run_name="__main__")
        consumer = importlib.import_module("consumer")
        assert consumer.route_code(" ab-1 ") == "route:AB-1"
        assert consumer.route_code("   ") == "fallback"
        assert consumer.route_code("") == "fallback"
    finally:
        for mod_name in ("api", "consumer", "smoke"):
            sys.modules.pop(mod_name, None)


def test_manual_read_range_excluded_for_edit_reply_but_prose_only_still_adds(project):
    coder = make_coder(
        local_callers=False,
        read_range_reader=create_read_range_reader(project),
    )
    coder.commands.run("/read-range pkg/consumer.py:1-3")
    assert [r.source for r in coder.read_only_ranges] == ["pkg/consumer.py:1-3"]

    prompted_subjects = []
    orig_confirm = coder.io.confirm_ask

    def spy_confirm(question, *args, **kwargs):
        prompted_subjects.append((question, kwargs.get("subject")))
        return orig_confirm(question, *args, **kwargs)

    coder.io.confirm_ask = spy_confirm

    edit_reply = (
        "Checked pkg/consumer.py before updating format_price in pricing.py.\n\n"
        + EDIT_BLOCK
    )
    edit_sends = fake_send(coder, [edit_reply])
    coder.run("update format_price")

    assert coder.get_inchat_relative_files() == ["pricing.py"]
    assert len(edit_sends) == 1
    assert not any(subj == "pkg/consumer.py" for _, subj in prompted_subjects)

    patch_reply = (
        "Checked pkg/consumer.py before updating pricing.py.\n"
        "*** Begin Patch\n"
        "*** Update File: pricing.py\n"
        "*** End Patch\n"
    )
    assert coder.check_for_file_mentions(patch_reply) is None
    assert coder.get_inchat_relative_files() == ["pricing.py"]

    # A prose-only reply without an edit block or CALLER_CONTRACT marker still prompts and adds.
    prose_reply = "Please add pkg/consumer.py so we can update the caller assertion."
    prose_sends = fake_send(coder, [prose_reply, "Updated plan after adding consumer."])
    coder.run("check whether callers need updates")

    assert any(
        q == "Add file to the chat?" and subj == "pkg/consumer.py"
        for q, subj in prompted_subjects
    )
    assert sorted(coder.get_inchat_relative_files()) == ["pkg/consumer.py", "pricing.py"]
    assert len(prose_sends) == 2
