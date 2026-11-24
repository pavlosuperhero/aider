from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import shutil
import sys
from typing import Callable, Sequence

import pytest

from aider.cymbal_callers import (
    BACKEND_AUTO,
    BACKEND_BUILTIN,
    BACKEND_CYMBAL,
    PINNED_CYMBAL_VERSION,
    CallerEvidenceItem,
    CallerLookupResult,
    CommandResult,
    CymbalCallerFinder,
    CymbalClient,
    CymbalConfig,
    CymbalEvidenceReport,
    CymbalExecutionError,
    CymbalNotFoundError,
    CymbalOutputLimitError,
    CymbalProtocolError,
    CymbalTimeoutError,
    CymbalVersionError,
    ReportingCompositeCallerFinder,
    SubprocessCymbalRunner,
    build_caller_finder,
    find_callers_with_backend,
    normalize_repo_path,
    parse_cymbal_version,
    read_bounded_window,
)
from aider.local_callers import (
    CallerEvidenceReport,
    CompositeCallerFinder,
)


def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


class FakeCymbalRunner:
    """Deterministic fake runner recording invocations and returning scripted outputs."""

    def __init__(
        self,
        *,
        version: str = f"cymbal v{PINNED_CYMBAL_VERSION}\n",
        refs: dict[str, object] | Callable[[str], object] | None = None,
        impact: dict[str, object] | Callable[[str], object] | None = None,
        changed: object = None,
        show: dict[str, object] | Callable[[str], object] | None = None,
        handler: Callable[[tuple[str, ...], Path, float, int], CommandResult] | None = None,
    ):
        self.version = version
        self.refs = refs if refs is not None else {}
        self.impact = impact if impact is not None else {}
        self.changed = changed if changed is not None else {"changed": [], "stale": False}
        self.show = show if show is not None else {}
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], Path, float, int]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandResult:
        argv_tuple = tuple(argv)
        self.calls.append((argv_tuple, cwd, timeout_seconds, max_output_bytes))
        if self.handler is not None:
            return self.handler(argv_tuple, cwd, timeout_seconds, max_output_bytes)

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
            return self._wrap_payload(argv_tuple, payload)

        target = argv_tuple[-1]
        if subcmd == "refs":
            payload = self.refs(target) if callable(self.refs) else self.refs.get(target, {"references": []})
            return self._wrap_payload(argv_tuple, payload)
        if subcmd == "impact":
            payload = (
                self.impact(target)
                if callable(self.impact)
                else self.impact.get(target, {"direct_callers": []})
            )
            return self._wrap_payload(argv_tuple, payload)
        if subcmd == "show":
            payload = self.show(target) if callable(self.show) else self.show.get(target, {})
            return self._wrap_payload(argv_tuple, payload)

        return CommandResult(
            argv=argv_tuple,
            returncode=2,
            stdout=b"",
            stderr=f"unknown subcommand: {subcmd}".encode("utf-8"),
        )

    @staticmethod
    def _wrap_payload(argv_tuple: tuple[str, ...], payload: object) -> CommandResult:
        if isinstance(payload, CommandResult):
            return payload
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            return CommandResult(argv=argv_tuple, returncode=0, stdout=payload, stderr=b"")
        return CommandResult(
            argv=argv_tuple,
            returncode=0,
            stdout=_json_bytes(payload),
            stderr=b"",
        )


def _write_basic_repo(root: Path) -> None:
    (root / "api.py").write_text("def parse(value):\n    return int(value)\n", encoding="utf-8")
    (root / "consumer.py").write_text(
        "from api import parse\n\ndef handle(raw):\n    return parse(raw) + 1\n",
        encoding="utf-8",
    )


def test_version_pinning_and_no_federate_enforced(tmp_path):
    _write_basic_repo(tmp_path)
    assert parse_cymbal_version("cymbal v0.18.0 (abc123)") == "0.18.0"
    assert parse_cymbal_version("0.18.0") == "0.18.0"
    assert parse_cymbal_version("no version here") is None

    runner = FakeCymbalRunner(
        refs={
            "parse": {
                "symbol": "parse",
                "definitions": [{"file": "api.py", "line": 1, "name": "parse"}],
                "references": [
                    {
                        "file": "consumer.py",
                        "line": 4,
                        "kind": "call",
                        "snippet": "    return parse(raw) + 1",
                    }
                ],
            }
        },
        impact={
            "parse": {
                "symbol": "parse",
                "direct_callers": [
                    {"file": "consumer.py", "line": 4, "caller": "handle", "kind": "impact"}
                ],
            }
        },
        changed={"changed": [{"file": "api.py", "symbol": "parse", "line": 1}], "stale": False},
        show={
            "parse": {
                "file": "api.py",
                "start_line": 1,
                "end_line": 2,
                "symbol": "parse",
                "code": "def parse(value):\n    return int(value)",
            }
        },
    )
    client = CymbalClient(CymbalConfig(enforce_pinned_version=True), runner=runner)

    refs_res = client.refs(tmp_path, "parse")
    impact_res = client.impact(tmp_path, "parse")
    changed_res = client.changed(tmp_path)
    show_res = client.show(tmp_path, "parse")

    assert refs_res.references[0].path == "consumer.py"
    assert refs_res.references[0].line == 4
    assert impact_res.direct_callers[0].caller_symbol == "handle"
    assert changed_res.changed_files == ("api.py",)
    assert show_res.slices[0].path == "api.py"
    assert show_res.slices[0].code == "def parse(value):\n    return int(value)"

    # `--version` is cached after the first check.
    version_calls = [argv for argv, *_ in runner.calls if argv[1] == "--version"]
    assert len(version_calls) == 1

    # Every data subcommand strictly passes `--json` and `--no-federate`.
    data_calls = [argv for argv, *_ in runner.calls if argv[1] != "--version"]
    assert len(data_calls) == 4
    for argv in data_calls:
        assert "--json" in argv
        assert "--no-federate" in argv

    # Incompatible version fails explicitly.
    wrong_version_client = CymbalClient(
        CymbalConfig(enforce_pinned_version=True),
        runner=FakeCymbalRunner(version="cymbal v0.19.0\n"),
    )
    with pytest.raises(CymbalVersionError, match="v0.18.0"):
        wrong_version_client.refs(tmp_path, "parse")


def test_flag_injection_and_malformed_json_errors(tmp_path):
    _write_basic_repo(tmp_path)
    runner = FakeCymbalRunner()
    client = CymbalClient(CymbalConfig(enforce_pinned_version=False), runner=runner)

    with pytest.raises(CymbalProtocolError, match="flag-like"):
        client.refs(tmp_path, "--federate")
    with pytest.raises(CymbalProtocolError, match="Unsafe control"):
        client.show(tmp_path, "parse\x00--federate")
    with pytest.raises(CymbalProtocolError, match="Empty"):
        client.impact(tmp_path, "   ")
    assert runner.calls == []

    bad_json_client = CymbalClient(
        CymbalConfig(enforce_pinned_version=False),
        runner=FakeCymbalRunner(refs={"parse": b"not-json{"}),
    )
    with pytest.raises(CymbalProtocolError, match="Malformed JSON"):
        bad_json_client.refs(tmp_path, "parse")

    empty_stdout_client = CymbalClient(
        CymbalConfig(enforce_pinned_version=False),
        runner=FakeCymbalRunner(refs={"parse": b"   "}),
    )
    with pytest.raises(CymbalProtocolError, match="empty JSON"):
        empty_stdout_client.refs(tmp_path, "parse")

    unknown_schema_client = CymbalClient(
        CymbalConfig(enforce_pinned_version=False),
        runner=FakeCymbalRunner(refs={"parse": {"future_v2_key": 123}}),
    )
    with pytest.raises(CymbalProtocolError, match="Unrecognized"):
        unknown_schema_client.refs(tmp_path, "parse")

    error_envelope_client = CymbalClient(
        CymbalConfig(enforce_pinned_version=False),
        runner=FakeCymbalRunner(refs={"parse": {"error": "corrupt index"}}),
    )
    with pytest.raises(CymbalExecutionError, match="corrupt index"):
        error_envelope_client.refs(tmp_path, "parse")


def test_show_validation_disk_freshness_utf8_truncation_and_duplicate_ambiguity(tmp_path):
    (tmp_path / "a.py").write_text("line1 = 'héllo 🚀'\nline2 = 'world'\nline3 = 'end'\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def dup():\n    return 2\n", encoding="utf-8")

    # 1. Disk content differs from stale Cymbal show output -> stale flagged, disk text returned.
    runner_stale = FakeCymbalRunner(
        show={
            "a.py:1": {
                "file": "a.py",
                "start_line": 1,
                "end_line": 2,
                "code": "line1 = 'OLD'\nline2 = 'world'",
            }
        }
    )
    client = CymbalClient(CymbalConfig(enforce_pinned_version=False), runner=runner_stale)
    res = client.show(tmp_path, "a.py:1", max_lines=2, max_bytes=18)
    assert res.stale is True
    assert res.truncated is True
    assert len(res.slices) == 1
    sl = res.slices[0]
    assert sl.stale is True
    assert "stale index" in sl.ambiguity
    # Must be valid UTF-8 and <= 18 bytes without splitting the 4-byte emoji.
    assert len(sl.code.encode("utf-8")) <= 18
    assert sl.code == "line1 = 'héllo "

    # 2. Disk shortened relative to Cymbal end_line -> stale flagged.
    runner_short = FakeCymbalRunner(
        show={
            "b.py:1": {
                "file": "b.py",
                "start_line": 1,
                "end_line": 5,
                "code": "def dup():\n    return 2\nx = 3\ny = 4\nz = 5",
            }
        }
    )
    short_res = CymbalClient(
        CymbalConfig(enforce_pinned_version=False), runner=runner_short
    ).show(tmp_path, "b.py:1")
    assert short_res.stale is True
    assert "stale index" in short_res.slices[0].ambiguity

    # 3. Multiple slices for a symbol -> duplicate ambiguity.
    runner_dup = FakeCymbalRunner(
        show={
            "dup": {
                "matches": [
                    {"file": "a.py", "start_line": 1, "end_line": 1, "code": "line1 = 'héllo 🚀'"},
                    {"file": "b.py", "start_line": 1, "end_line": 2, "code": "def dup():\n    return 2"},
                ]
            }
        }
    )
    dup_res = CymbalClient(
        CymbalConfig(enforce_pinned_version=False), runner=runner_dup
    ).show(tmp_path, "dup")
    assert dup_res.ambiguous is True
    assert len(dup_res.slices) == 2
    assert all("duplicate symbol definition" in s.ambiguity for s in dup_res.slices)


def test_subprocess_runner_enforces_missing_binary_timeout_and_output_cap(tmp_path):
    runner = SubprocessCymbalRunner()

    # Missing executable raises explicit CymbalNotFoundError.
    with pytest.raises(CymbalNotFoundError):
        runner.run(
            ["definitely-not-a-real-cymbal-binary-xyz", "--version"],
            cwd=tmp_path,
            timeout_seconds=1.0,
            max_output_bytes=1024,
        )

    # Hard deadline kills hung child promptly and client surfaces CymbalTimeoutError.
    timed = runner.run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout_seconds=0.15,
        max_output_bytes=4096,
    )
    assert timed.timed_out is True
    assert timed.elapsed_seconds < 3.0

    # Hard stdout byte cap kills noisy child and client surfaces CymbalOutputLimitError.
    capped = runner.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 65536); sys.stdout.flush()"],
        cwd=tmp_path,
        timeout_seconds=5.0,
        max_output_bytes=512,
    )
    assert capped.stdout_truncated is True
    assert len(capped.stdout) <= 512

    # Client maps CommandResult flags to explicit errors.
    timeout_client = CymbalClient(
        CymbalConfig(enforce_pinned_version=False),
        runner=FakeCymbalRunner(
            refs={
                "parse": CommandResult(
                    argv=("cymbal", "refs", "--json", "--no-federate", "parse"),
                    returncode=-9,
                    stdout=b"",
                    stderr=b"",
                    timed_out=True,
                )
            }
        ),
    )
    with pytest.raises(CymbalTimeoutError):
        timeout_client.refs(tmp_path, "parse")

    cap_client = CymbalClient(
        CymbalConfig(enforce_pinned_version=False),
        runner=FakeCymbalRunner(
            refs={
                "parse": CommandResult(
                    argv=("cymbal", "refs", "--json", "--no-federate", "parse"),
                    returncode=-9,
                    stdout=b"{",
                    stderr=b"",
                    stdout_truncated=True,
                )
            }
        ),
    )
    with pytest.raises(CymbalOutputLimitError):
        cap_client.refs(tmp_path, "parse")


def test_robust_path_normalization_and_large_file_bounded_window(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    pkg = project / "pkg"
    pkg.mkdir()
    (pkg / "consumer.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    (project / "link.py").symlink_to(pkg / "consumer.py")
    (project / "linkdir").symlink_to(pkg)
    (project / "binary.py").write_bytes(b"def f():\n\x00binary\n")
    (project / "badutf8.py").write_bytes(b"\xff\xfeinvalid\n")

    assert normalize_repo_path(project, "pkg/consumer.py") == "pkg/consumer.py"
    assert normalize_repo_path(project, "./pkg/consumer.py") == "pkg/consumer.py"
    assert normalize_repo_path(project, r"pkg\consumer.py") == "pkg/consumer.py"
    assert normalize_repo_path(project, str(project / "pkg" / "consumer.py")) == "pkg/consumer.py"
    assert normalize_repo_path(project, str(project.resolve() / "pkg" / "consumer.py")) == "pkg/consumer.py"

    # Unsafe paths rejected.
    assert normalize_repo_path(project, "../outside.py") is None
    assert normalize_repo_path(project, "pkg/../../outside.py") is None
    assert normalize_repo_path(project, str(outside)) is None
    assert normalize_repo_path(project, "link.py") is None
    assert normalize_repo_path(project, "linkdir/consumer.py") is None
    assert normalize_repo_path(project, "pkg") is None
    assert normalize_repo_path(project, "missing.py") is None
    assert normalize_repo_path(project, "pkg/consumer.py\x00.txt") is None

    # Binary and invalid UTF-8 rejected by bounded window reader.
    assert read_bounded_window(project, "binary.py", 1) is None
    assert read_bounded_window(project, "badutf8.py", 1) is None

    # Large file > 128 KiB still readable at deep line numbers.
    big_lines = [f"# padding line {i:04d} " + ("a" * 60) for i in range(1, 2500)]
    big_lines.append("result = parse(payload)")
    big_lines.append("return result")
    big_file = project / "big.py"
    big_file.write_text("\n".join(big_lines) + "\n", encoding="utf-8")
    assert big_file.stat().st_size > 131_072

    win = read_bounded_window(project, "big.py", 2500, context_lines=1, max_chars=700)
    assert win is not None
    start_line, snippet, center_text, exists, truncated = win
    assert exists is True
    assert truncated is False
    assert start_line == 2499
    assert center_text == "result = parse(payload)"
    assert "result = parse(payload)" in snippet


def test_conservative_stale_alias_and_duplicate_ambiguity(tmp_path):
    (tmp_path / "api.py").write_text(
        "from .compat import JSONDecodeError\n\ndef parse(x):\n    return x\n",
        encoding="utf-8",
    )
    (tmp_path / "api2.py").write_text("def parse(x):\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text(
        "from api import parse as p\n\ndef run(v):\n    return p(v)\n",
        encoding="utf-8",
    )

    # 1. Duplicate editable definitions + alias reference + stale snippet mismatch.
    runner = FakeCymbalRunner(
        refs={
            "parse": {
                "definitions": [
                    {"file": "api.py", "line": 3, "name": "parse"},
                    {"file": "api2.py", "line": 1, "name": "parse"},
                ],
                "references": [
                    {
                        "file": "consumer.py",
                        "line": 4,
                        "kind": "alias",
                        "alias": True,
                        "snippet": "    return OLD_CALL(v)",
                    }
                ],
                "stale": False,
            }
        },
        impact={"parse": {"direct_callers": [], "stale": False}},
        changed={"changed": ["consumer.py"], "stale": False},
    )
    result = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py", "api2.py"],
        ["consumer.py"],
        backend=BACKEND_CYMBAL,
        runner=runner,
    )
    assert result.status == "INCOMPLETE"
    assert result.source == BACKEND_CYMBAL
    assert "duplicate symbol definition" in result.ambiguity
    assert "alias reference" in result.ambiguity
    assert "stale index" in result.ambiguity
    assert "working_tree_changed" in result.scope
    assert len(result.excerpts) == 1
    assert result.excerpts[0].incomplete is True
    # Uses fresh disk text rather than stale Cymbal snippet.
    assert "return p(v)" in result.excerpts[0].code

    # 2. Out-of-range line past EOF on disk records stale index and does not fabricate code.
    runner_eof = FakeCymbalRunner(
        refs={
            "parse": {
                "definitions": [{"file": "api.py", "line": 3, "name": "parse"}],
                "references": [{"file": "consumer.py", "line": 999, "kind": "call"}],
            }
        },
        impact={"parse": {"direct_callers": []}},
        changed={"changed": [], "stale": False},
    )
    eof_result = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py"],
        backend=BACKEND_CYMBAL,
        runner=runner_eof,
    )
    assert eof_result.status == "NO_EVIDENCE"
    assert eof_result.excerpts == ()
    assert "stale index" in eof_result.ambiguity
    assert any("out-of-range" in d for d in eof_result.diagnostics)

    # 3. Editable re-export alias symbol (like `from .compat import JSONDecodeError`) is flagged as alias.
    (tmp_path / "compat.py").write_text("class JSONDecodeError(ValueError): pass\n", encoding="utf-8")
    (tmp_path / "handler.py").write_text(
        "from api import JSONDecodeError\ntry:\n    pass\nexcept JSONDecodeError:\n    pass\n",
        encoding="utf-8",
    )
    runner_alias = FakeCymbalRunner(
        refs={
            "JSONDecodeError": {
                "definitions": [{"file": "api.py", "line": 1, "name": "JSONDecodeError"}],
                "references": [
                    {"file": "handler.py", "line": 4, "kind": "reference", "snippet": "except JSONDecodeError:"}
                ],
            }
        },
        impact={"JSONDecodeError": {"direct_callers": []}},
    )
    alias_res = find_callers_with_backend(
        tmp_path,
        "fix JSONDecodeError",
        ["api.py"],
        ["handler.py"],
        backend=BACKEND_CYMBAL,
        runner=runner_alias,
    )
    assert alias_res.status == "INCOMPLETE"
    assert "alias reference" in alias_res.ambiguity
    assert alias_res.excerpts[0].path == "handler.py"


def test_production_first_caps_and_call_over_import_priority(tmp_path):
    (tmp_path / "api.py").write_text("def parse(x):\n    return x\n", encoding="utf-8")
    for idx in range(4):
        (tmp_path / f"prod{idx}.py").write_text(
            "from api import parse\n\ndef f():\n    return parse(1)\n",
            encoding="utf-8",
        )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_api.py").write_text(
        "from api import parse\nassert parse(1) == 1\n", encoding="utf-8"
    )
    (tmp_path / "smoke.py").write_text("from api import parse\nassert parse(1)\n", encoding="utf-8")

    refs_payload = {
        "definitions": [{"file": "api.py", "line": 1, "name": "parse"}],
        "references": [
            # Import on line 1 should lose to call on line 4 in the same file.
            {"file": "prod0.py", "line": 1, "kind": "import", "snippet": "from api import parse"},
            {"file": "prod0.py", "line": 4, "kind": "call", "snippet": "    return parse(1)"},
            {"file": "prod1.py", "line": 4, "kind": "call", "snippet": "    return parse(1)"},
            {"file": "prod2.py", "line": 4, "kind": "call", "snippet": "    return parse(1)"},
            {"file": "prod3.py", "line": 4, "kind": "call", "snippet": "    return parse(1)"},
            {"file": "tests/test_api.py", "line": 2, "kind": "call", "snippet": "assert parse(1) == 1"},
            {"file": "smoke.py", "line": 2, "kind": "call", "snippet": "assert parse(1)"},
            # Unsafe path should be rejected safely with diagnostic.
            {"file": "../outside.py", "line": 1, "kind": "call"},
        ],
    }
    runner = FakeCymbalRunner(refs={"parse": refs_payload}, impact={"parse": {"direct_callers": []}})
    candidates = ["smoke.py", "tests/test_api.py", "prod3.py", "prod1.py", "prod0.py", "prod2.py"]

    res = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py"],
        candidates,
        backend=BACKEND_CYMBAL,
        max_files=3,
        runner=runner,
    )
    assert [e.path for e in res.excerpts] == ["prod0.py", "prod1.py", "prod2.py"]
    assert res.items[0].line == 4
    assert res.items[0].kind == "call"
    assert "production_only" in res.scope
    assert "test_callers_deprioritized" in res.scope
    assert "additional files omitted" in res.truncation
    assert res.status == "INCOMPLETE"
    assert any("rejected unsafe reference path" in d for d in res.diagnostics)

    # When only test/smoke files are in candidates, falls back to tests cleanly.
    test_only = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["smoke.py", "tests/test_api.py"],
        backend=BACKEND_CYMBAL,
        runner=runner,
    )
    assert [e.path for e in test_only.excerpts] == ["smoke.py", "tests/test_api.py"]
    assert "test_fallback" in test_only.scope
    assert test_only.status == "COMPLETE_WITHIN_SCOPE"


def test_backend_modes_builtin_default_auto_fallback_and_explicit_cymbal_error(tmp_path):
    _write_basic_repo(tmp_path)

    # 1. Invalid backend rejected explicitly.
    with pytest.raises(ValueError, match="Unknown caller backend"):
        build_caller_finder(backend="bogus")  # type: ignore[arg-type]

    # 2. Default backend is `builtin`: runner is never touched.
    failing_runner = FakeCymbalRunner(
        handler=lambda *args: (_ for _ in ()).throw(CymbalNotFoundError("cymbal not found"))
    )
    default_res = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py"],
        runner=failing_runner,
    )
    assert failing_runner.calls == []
    assert default_res.backend_requested == BACKEND_BUILTIN
    assert default_res.source == BACKEND_BUILTIN
    assert default_res.fallback_used is False
    assert default_res.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in default_res.excerpts] == ["consumer.py"]
    assert default_res.items[0].source == "builtin:python_ast"

    # 3. `auto` mode falls back to `builtin` with clear diagnostics on any CymbalError.
    auto_res = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py"],
        backend=BACKEND_AUTO,
        runner=failing_runner,
    )
    assert len(failing_runner.calls) == 1
    assert auto_res.backend_requested == BACKEND_AUTO
    assert auto_res.source == BACKEND_BUILTIN
    assert auto_res.fallback_used is True
    assert auto_res.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in auto_res.excerpts] == ["consumer.py"]
    assert any("cymbal fallback (not_found)" in d for d in auto_res.diagnostics)

    # 4. `auto` mode uses Cymbal when healthy without fallback.
    healthy_runner = FakeCymbalRunner(
        refs={
            "parse": {
                "definitions": [{"file": "api.py", "line": 1, "name": "parse"}],
                "references": [
                    {"file": "consumer.py", "line": 4, "kind": "call", "snippet": "    return parse(raw) + 1"}
                ],
            }
        },
        impact={"parse": {"direct_callers": []}},
    )
    auto_ok = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py"],
        backend=BACKEND_AUTO,
        runner=healthy_runner,
    )
    assert auto_ok.source == BACKEND_CYMBAL
    assert auto_ok.fallback_used is False
    assert auto_ok.status == "COMPLETE_WITHIN_SCOPE"
    assert auto_ok.items[0].source == "cymbal:refs"

    # 5. Explicit `cymbal` mode surfaces CymbalError directly without falling back.
    with pytest.raises(CymbalNotFoundError):
        find_callers_with_backend(
            tmp_path,
            "fix parse",
            ["api.py"],
            ["consumer.py"],
            backend=BACKEND_CYMBAL,
            runner=failing_runner,
        )


def test_report_propagation_and_immutability_across_builtin_and_cymbal_adapters(tmp_path):
    (tmp_path / "api.py").write_text(
        "class Service:\n    def convert(self, x):\n        return x\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from api import Service\ndef run(svc):\n    return svc.convert(42)\n",
        encoding="utf-8",
    )

    # Built-in adapter via ReportingCompositeCallerFinder propagates method receiver ambiguity.
    builtin_res = find_callers_with_backend(
        tmp_path,
        "fix convert",
        ["api.py"],
        ["consumer.py"],
        backend=BACKEND_BUILTIN,
    )
    assert builtin_res.status == "INCOMPLETE"
    assert "ambiguous method receiver" in builtin_res.ambiguity
    assert ("api.py", "convert") in builtin_res.definitions
    assert isinstance(builtin_res, CallerLookupResult)
    assert isinstance(builtin_res.items[0], CallerEvidenceItem)
    with pytest.raises(FrozenInstanceError):
        builtin_res.status = "NO_EVIDENCE"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        builtin_res.items[0].symbol = "other"  # type: ignore[misc]

    # Third-party / Cymbal adapter inside ReportingCompositeCallerFinder also receives report!
    runner = FakeCymbalRunner(
        refs={
            "convert": {
                "definitions": [{"file": "api.py", "line": 2, "name": "convert"}],
                "references": [
                    {"file": "consumer.py", "line": 3, "kind": "call", "snippet": "    return svc.convert(42)"}
                ],
                "truncated": True,
            }
        },
        impact={"convert": {"direct_callers": []}},
    )
    cymbal_finder = CymbalCallerFinder(runner=runner)
    composite = ReportingCompositeCallerFinder(finders=(cymbal_finder,))
    report = CymbalEvidenceReport(backend_requested=BACKEND_CYMBAL, backend_used=BACKEND_CYMBAL)
    excerpts = composite.find_callers(
        tmp_path,
        "fix convert",
        ["api.py"],
        ["consumer.py"],
        report=report,
    )
    res = report.to_result(excerpts)
    assert res.status == "INCOMPLETE"
    assert ("api.py", "convert") in res.definitions
    assert "ambiguous method receiver" in res.ambiguity
    assert "cymbal refs truncated" in res.truncation
    assert "no_federate" in res.scope
    assert "INCOMPLETE" in report.describe(excerpts)

    # Protocol compatibility with existing local_callers.CompositeCallerFinder & CallerEvidenceReport.
    legacy_report = CallerEvidenceReport()
    direct_excerpts = cymbal_finder.find_callers(
        tmp_path,
        "fix convert",
        ["api.py"],
        ["consumer.py"],
        report=legacy_report,
    )
    assert ("api.py", "convert") in legacy_report.definitions
    assert "INCOMPLETE" in legacy_report.describe(direct_excerpts)

    legacy_composite = CompositeCallerFinder(finders=(cymbal_finder,))
    compat_excerpts = legacy_composite.find_callers(
        tmp_path,
        "fix convert",
        ["api.py"],
        ["consumer.py"],
    )
    assert len(compat_excerpts) == 1
    assert compat_excerpts[0].path == "consumer.py"


def test_total_deadline_and_impact_transitive_handling(tmp_path):
    _write_basic_repo(tmp_path)
    (tmp_path / "indirect.py").write_text(
        "from consumer import handle\n\ndef outer(x):\n    return handle(x)\n",
        encoding="utf-8",
    )

    runner = FakeCymbalRunner(
        refs={"parse": {"definitions": [{"file": "api.py", "line": 1, "name": "parse"}], "references": []}},
        impact={
            "parse": {
                "direct_callers": [],
                "transitive_callers": [
                    {
                        "file": "indirect.py",
                        "line": 4,
                        "caller": "outer",
                        "snippet": "    return handle(x)",
                    }
                ],
            }
        },
    )
    res = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["indirect.py"],
        backend=BACKEND_CYMBAL,
        runner=runner,
    )
    assert res.status == "INCOMPLETE"
    assert [e.path for e in res.excerpts] == ["indirect.py"]
    assert "transitive caller" in res.ambiguity
    assert "transitive_impact" in res.scope

    # Zero/negative total deadline raises CymbalTimeoutError explicitly in cymbal mode.
    with pytest.raises(CymbalTimeoutError):
        find_callers_with_backend(
            tmp_path,
            "fix parse",
            ["api.py"],
            ["consumer.py"],
            backend=BACKEND_CYMBAL,
            cymbal_config=CymbalConfig(total_deadline_seconds=-1.0),
            runner=runner,
        )


def test_pinned_v0_18_0_cymbal_smoke(tmp_path):
    """Live smoke test validating real Cymbal v0.18.0 compatibility when installed."""
    binary = shutil.which("cymbal")
    if not binary:
        pytest.skip("cymbal binary is not installed in PATH")

    config = CymbalConfig(
        executable=binary,
        expected_version=PINNED_CYMBAL_VERSION,
        enforce_pinned_version=True,
        use_changed_check=False,
    )
    client = CymbalClient(config)
    try:
        version = client.verify_version(tmp_path)
    except CymbalVersionError as exc:
        pytest.skip(f"installed cymbal is not pinned v{PINNED_CYMBAL_VERSION}: {exc}")

    assert version == PINNED_CYMBAL_VERSION

    _write_basic_repo(tmp_path)
    refs_res = client.refs(tmp_path, "parse")
    assert isinstance(refs_res.references, tuple)
    impact_res = client.impact(tmp_path, "parse")
    assert isinstance(impact_res.direct_callers, tuple)
    show_res = client.show(tmp_path, "parse")
    assert isinstance(show_res.slices, tuple)

    result = find_callers_with_backend(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py"],
        backend=BACKEND_CYMBAL,
        cymbal_config=config,
    )
    assert result.source == BACKEND_CYMBAL
    assert "no_federate" in result.scope
