from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable, Sequence

import pytest

from aider.cymbal_callers import (
    BACKEND_AUTO,
    BACKEND_BUILTIN,
    BACKEND_CYMBAL,
    PINNED_CYMBAL_VERSION,
    BoundedWindow,
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
            payload = (
                self.refs(target)
                if callable(self.refs)
                else self.refs.get(target, {"references": []})
            )
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
    (tmp_path / "a.py").write_text(
        "line1 = 'héllo 🚀'\nline2 = 'world'\nline3 = 'end'\n", encoding="utf-8"
    )
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
                    {
                        "file": "b.py",
                        "start_line": 1,
                        "end_line": 2,
                        "code": "def dup():\n    return 2",
                    },
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
    assert (
        normalize_repo_path(project, str(project.resolve() / "pkg" / "consumer.py"))
        == "pkg/consumer.py"
    )

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
    assert isinstance(win, BoundedWindow)
    assert win.line_exists is True
    assert win.truncated is False
    assert win.hit_scan_cap is False
    assert win.start_line == 2499
    assert win.call_line_text == "result = parse(payload)"
    assert "result = parse(payload)" in win.text

    # Reading past max_scan_bytes preserves hit_scan_cap=True instead of looking like EOF.
    capped = read_bounded_window(
        project, "big.py", 2500, context_lines=1, max_chars=700, max_scan_bytes=1024
    )
    assert isinstance(capped, BoundedWindow)
    assert capped.line_exists is False
    assert capped.hit_scan_cap is True


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

    # 2. Out-of-range line past EOF on disk records stale index, does not fabricate code,
    # and reports INCOMPLETE (not NO_EVIDENCE) because a reported caller could not be inspected.
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
    assert eof_result.status == "INCOMPLETE"
    assert eof_result.excerpts == ()
    assert "stale index" in eof_result.ambiguity
    assert any("out-of-range" in d for d in eof_result.diagnostics)

    # 3. Editable re-export alias symbol (like `from .compat import JSONDecodeError`)
    # is flagged as alias.
    (tmp_path / "compat.py").write_text(
        "class JSONDecodeError(ValueError): pass\n", encoding="utf-8"
    )
    (tmp_path / "handler.py").write_text(
        "from api import JSONDecodeError\ntry:\n    pass\nexcept JSONDecodeError:\n    pass\n",
        encoding="utf-8",
    )
    runner_alias = FakeCymbalRunner(
        refs={
            "JSONDecodeError": {
                "definitions": [{"file": "api.py", "line": 1, "name": "JSONDecodeError"}],
                "references": [
                    {
                        "file": "handler.py",
                        "line": 4,
                        "kind": "reference",
                        "snippet": "except JSONDecodeError:",
                    }
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
            {
                "file": "tests/test_api.py",
                "line": 2,
                "kind": "call",
                "snippet": "assert parse(1) == 1",
            },
            {"file": "smoke.py", "line": 2, "kind": "call", "snippet": "assert parse(1)"},
            # Unsafe path should be rejected safely with diagnostic.
            {"file": "../outside.py", "line": 1, "kind": "call"},
        ],
    }
    runner = FakeCymbalRunner(
        refs={"parse": refs_payload}, impact={"parse": {"direct_callers": []}}
    )
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
                    {
                        "file": "consumer.py",
                        "line": 4,
                        "kind": "call",
                        "snippet": "    return parse(raw) + 1",
                    }
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
                    {
                        "file": "consumer.py",
                        "line": 3,
                        "kind": "call",
                        "snippet": "    return svc.convert(42)",
                    }
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

    # Protocol compatibility with existing local_callers.CompositeCallerFinder
    # & CallerEvidenceReport.
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
        refs={
            "parse": {
                "definitions": [{"file": "api.py", "line": 1, "name": "parse"}],
                "references": [],
            }
        },
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


def test_v0_18_0_verified_json_payloads_refs_impact_changed_and_show(tmp_path):
    _write_basic_repo(tmp_path)
    (tmp_path / "indirect.py").write_text(
        "from consumer import handle\n\ndef outer(x):\n    return handle(x)\n",
        encoding="utf-8",
    )

    refs_payload = {
        "version": "0.1",
        "results": [
            {
                "file": "/abs/consumer.py",
                "rel_path": "consumer.py",
                "line": 4,
                "name": "parse",
                "context": ["def handle(raw):", "    return parse(raw) + 1"],
            }
        ],
    }
    impact_payload = {
        "version": "0.1",
        "results": {
            "definition_count": 2,
            "depth": 2,
            "limit": 50,
            "metrics": {"elapsed_ms": 4},
            "production_callers": 2,
            "raw_rows": 2,
            "resolve_scope": "family",
            "results": [
                {
                    "hit_symbols": ["parse"],
                    "row": {
                        "symbol": "parse",
                        "caller": "handle",
                        "file": "/abs/consumer.py",
                        "rel_path": "consumer.py",
                        "line": 4,
                        "depth": 1,
                        "context": ["def handle(raw):", "    return parse(raw) + 1"],
                    },
                },
                {
                    "hit_symbols": ["parse"],
                    "row": {
                        "symbol": "parse",
                        "caller": "outer",
                        "file": "/abs/indirect.py",
                        "rel_path": "indirect.py",
                        "line": 4,
                        "depth": 2,
                        "context": ["def outer(x):", "    return handle(x)"],
                    },
                },
            ],
            "symbols": ["parse"],
            "test_callers": 0,
            "total_callers": 2,
            "truncated": True,
        },
    }
    changed_payload = {
        "version": "0.1",
        "results": {
            "analyzed": 1,
            "base": "working tree",
            "changed_symbols": 1,
            "resolve_scope": "family",
            "results": [
                {
                    "symbol": "parse",
                    "files": ["api.py"],
                    "definition_count": 1,
                    "references": {"count": 1},
                    "impact": {"total_callers": 2},
                }
            ],
            "truncated": False,
        },
    }
    show_symbol_payload = {
        "version": "0.1",
        "results": {
            "file": "/abs/api.py",
            "lines": [
                {"line": 1, "content": "def parse(value):"},
                {"line": 2, "content": "    return int(value)"},
            ],
            "symbol": {
                "name": "parse",
                "kind": "function",
                "file": "/abs/api.py",
                "rel_path": "api.py",
                "start_line": 1,
                "end_line": 2,
                "depth": 0,
                "signature": "(value)",
                "language": "python",
                "body_hash": "abc123",
            },
        },
    }
    show_range_payload = {
        "version": "0.1",
        "results": {
            "file": str(tmp_path.resolve() / "consumer.py"),
            "lines": [
                {"line": 3, "content": "def handle(raw):"},
                {"line": 4, "content": "    return parse(raw) + 1"},
            ],
        },
    }

    runner = FakeCymbalRunner(
        refs={"parse": refs_payload},
        impact={"parse": impact_payload},
        changed=changed_payload,
        show={
            "parse": show_symbol_payload,
            "consumer.py:3-4": show_range_payload,
        },
    )
    client = CymbalClient(CymbalConfig(enforce_pinned_version=True), runner=runner)

    refs_res = client.refs(tmp_path, "parse")
    assert len(refs_res.references) == 1
    assert refs_res.references[0].path == "consumer.py"
    assert refs_res.references[0].line == 4
    assert refs_res.references[0].symbol == "parse"
    assert refs_res.references[0].caller_symbol == ""
    assert refs_res.references[0].snippet == "def handle(raw):\n    return parse(raw) + 1"

    impact_res = client.impact(tmp_path, "parse")
    assert impact_res.ambiguous is True
    assert impact_res.truncated is True
    assert len(impact_res.direct_callers) == 1
    assert impact_res.direct_callers[0].path == "consumer.py"
    assert impact_res.direct_callers[0].caller_symbol == "handle"
    assert impact_res.direct_callers[0].transitive is False
    assert impact_res.direct_callers[0].snippet == "def handle(raw):\n    return parse(raw) + 1"
    assert len(impact_res.transitive_callers) == 1
    assert impact_res.transitive_callers[0].path == "indirect.py"
    assert impact_res.transitive_callers[0].caller_symbol == "outer"
    assert impact_res.transitive_callers[0].transitive is True

    changed_res = client.changed(tmp_path)
    assert changed_res.changed_files == ("api.py",)
    assert len(changed_res.entries) == 1
    assert changed_res.entries[0].path == "api.py"
    assert changed_res.entries[0].symbol == "parse"
    assert changed_res.stale is False
    assert changed_res.truncated is False

    show_sym = client.show(tmp_path, "parse")
    assert show_sym.stale is False
    assert len(show_sym.slices) == 1
    assert show_sym.slices[0].path == "api.py"
    assert show_sym.slices[0].symbol == "parse"
    assert show_sym.slices[0].kind == "function"
    assert show_sym.slices[0].start_line == 1
    assert show_sym.slices[0].end_line == 2
    assert show_sym.slices[0].code == "def parse(value):\n    return int(value)"

    show_range = client.show(tmp_path, "consumer.py:3-4")
    assert show_range.stale is False
    assert len(show_range.slices) == 1
    assert show_range.slices[0].path == "consumer.py"
    assert show_range.slices[0].start_line == 3
    assert show_range.slices[0].end_line == 4
    assert show_range.slices[0].code == "def handle(raw):\n    return parse(raw) + 1"

    finder = build_caller_finder(BACKEND_CYMBAL, runner=runner)
    lookup_res = finder.lookup(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py", "indirect.py"],
    )
    assert lookup_res.status == "INCOMPLETE"
    assert lookup_res.source == BACKEND_CYMBAL
    assert [e.path for e in lookup_res.excerpts] == ["consumer.py", "indirect.py"]
    assert "duplicate symbol definition" in lookup_res.ambiguity
    assert "transitive caller" in lookup_res.ambiguity
    assert "cymbal impact truncated" in lookup_res.truncation
    assert "working_tree_changed" in lookup_res.scope
    assert "transitive_impact" in lookup_res.scope
    assert "stale index" not in lookup_res.ambiguity


def test_pinned_v0_18_0_cymbal_smoke(tmp_path):
    """Live smoke test validating real Cymbal v0.18.0 compatibility when installed."""
    binary = shutil.which("cymbal")
    if not binary:
        pytest.skip("cymbal binary is not installed in PATH")
    git_bin = shutil.which("git")
    if not git_bin:
        pytest.skip("git binary is not installed in PATH")

    config = CymbalConfig(
        executable=binary,
        expected_version=PINNED_CYMBAL_VERSION,
        enforce_pinned_version=True,
        use_changed_check=True,
    )
    client = CymbalClient(config)
    try:
        version = client.verify_version(tmp_path)
    except CymbalVersionError as exc:
        pytest.skip(f"installed cymbal is not pinned v{PINNED_CYMBAL_VERSION}: {exc}")

    assert version == PINNED_CYMBAL_VERSION

    _write_basic_repo(tmp_path)
    subprocess.run([git_bin, "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [git_bin, "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [git_bin, "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [git_bin, "add", "api.py", "consumer.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [git_bin, "-c", "commit.gpgsign=false", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run([binary, "index"], cwd=tmp_path, check=True, capture_output=True)

    refs_res = client.refs(tmp_path, "parse")
    assert isinstance(refs_res.references, tuple)
    assert any(r.path == "consumer.py" and r.line == 4 for r in refs_res.references)

    impact_res = client.impact(tmp_path, "parse")
    assert isinstance(impact_res.direct_callers, tuple)
    assert any(c.path == "consumer.py" and c.line == 4 for c in impact_res.direct_callers)

    changed_clean = client.changed(tmp_path)
    assert isinstance(changed_clean.entries, tuple)
    assert changed_clean.changed_files == ()

    show_sym = client.show(tmp_path, "parse")
    assert len(show_sym.slices) == 1
    assert show_sym.slices[0].path == "api.py"
    assert show_sym.slices[0].symbol == "parse"
    assert show_sym.slices[0].start_line == 1
    assert "def parse(value):" in show_sym.slices[0].code
    assert show_sym.stale is False

    show_range = client.show(tmp_path, "consumer.py:3-4")
    assert len(show_range.slices) == 1
    assert show_range.slices[0].path == "consumer.py"
    assert show_range.slices[0].start_line == 3
    assert show_range.slices[0].end_line == 4
    assert "return parse(raw) + 1" in show_range.slices[0].code
    assert show_range.stale is False

    finder = build_caller_finder(BACKEND_CYMBAL, cymbal_config=config)
    result = finder.lookup(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py"],
    )
    assert result.source == BACKEND_CYMBAL
    assert result.backend_requested == BACKEND_CYMBAL
    assert result.fallback_used is False
    assert "no_federate" in result.scope
    assert [e.path for e in result.excerpts] == ["consumer.py"]
    assert "return parse(raw) + 1" in result.excerpts[0].code

    (tmp_path / "api.py").write_text(
        "def parse(value):\n    return int(value) + 0\n", encoding="utf-8"
    )
    changed_dirty = client.changed(tmp_path)
    assert isinstance(changed_dirty.entries, tuple)
    assert "api.py" in changed_dirty.changed_files
    assert any(e.path == "api.py" and e.symbol == "parse" for e in changed_dirty.entries)


def test_c_cpp_editable_target_extraction_and_regex_fallback(tmp_path, monkeypatch):
    import aider.cymbal_callers as cc

    (tmp_path / "parser.c").write_text(
        "static int ini_parse_stream(ini_reader reader, void *user);\n"
        "long long yajl_parse_integer(const unsigned char *number, unsigned int length) {\n"
        "    return (long long)length;\n"
        "}\n"
        "int redisReaderGetReply(redisReader *r, void **reply) {\n"
        "    return 0;\n"
        "}\n"
        "static int ini_parse_stream(ini_reader reader, void *user) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.c").write_text(
        "long long handle(const unsigned char *buf, unsigned int n) {\n"
        "    return yajl_parse_integer(buf, n);\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "service.cpp").write_text(
        "class Engine {\n"
        "public:\n"
        "    int decode(const char *s) { return 1; }\n"
        "};\n"
        "int Engine::finish(int code) {\n"
        "    return code;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "main.cpp").write_text(
        "int run(Engine &e) {\n"
        "    return e.finish(e.decode(\"ok\"));\n"
        "}\n",
        encoding="utf-8",
    )

    # 1. Tree-sitter + Cymbal lookup discovers C targets without false duplicate from prototype.
    runner = FakeCymbalRunner(
        refs={
            "yajl_parse_integer": {
                "definitions": [{"file": "parser.c", "line": 2, "name": "yajl_parse_integer"}],
                "references": [
                    {
                        "file": "consumer.c",
                        "line": 2,
                        "kind": "call",
                        "snippet": "    return yajl_parse_integer(buf, n);",
                    }
                ],
            },
            "redisReaderGetReply": {"references": []},
            "ini_parse_stream": {"references": []},
        },
        impact={
            "yajl_parse_integer": {"direct_callers": []},
            "redisReaderGetReply": {"direct_callers": []},
            "ini_parse_stream": {"direct_callers": []},
        },
    )
    c_res = find_callers_with_backend(
        tmp_path,
        "fix yajl_parse_integer redisReaderGetReply ini_parse_stream",
        ["parser.c"],
        ["consumer.c"],
        backend=BACKEND_CYMBAL,
        runner=runner,
    )
    assert ("parser.c", "yajl_parse_integer") in c_res.definitions
    assert ("parser.c", "redisReaderGetReply") in c_res.definitions
    assert ("parser.c", "ini_parse_stream") in c_res.definitions
    assert "duplicate symbol definition" not in c_res.ambiguity
    assert [e.path for e in c_res.excerpts] == ["consumer.c"]
    assert "yajl_parse_integer(buf, n)" in c_res.excerpts[0].code

    # 2. C++ inline and out-of-line methods are extracted as targets.
    cpp_runner = FakeCymbalRunner(
        refs={
            "decode": {"references": []},
            "finish": {
                "definitions": [{"file": "service.cpp", "line": 5, "name": "finish"}],
                "references": [
                    {
                        "file": "main.cpp",
                        "line": 2,
                        "kind": "call",
                        "snippet": '    return e.finish(e.decode("ok"));',
                    }
                ],
            },
        },
        impact={"decode": {"direct_callers": []}, "finish": {"direct_callers": []}},
    )
    cpp_res = find_callers_with_backend(
        tmp_path,
        "fix decode and finish",
        ["service.cpp"],
        ["main.cpp"],
        backend=BACKEND_CYMBAL,
        runner=cpp_runner,
    )
    assert ("service.cpp", "decode") in cpp_res.definitions
    assert ("service.cpp", "finish") in cpp_res.definitions
    assert [e.path for e in cpp_res.excerpts] == ["main.cpp"]

    # 3. Regex fallback works when tree-sitter extraction is unavailable.
    monkeypatch.setattr(cc, "_extract_c_cpp_ts_targets", lambda path, source, words: [])
    c_src = (tmp_path / "parser.c").read_text(encoding="utf-8")
    fallback_defs = cc._extract_generic_targets(
        "parser.c",
        c_src,
        {"yajl_parse_integer", "redisReaderGetReply", "ini_parse_stream"},
    )
    assert {d.symbol for d in fallback_defs} == {
        "yajl_parse_integer",
        "redisReaderGetReply",
        "ini_parse_stream",
    }


def test_per_symbol_not_found_isolated_while_other_errors_propagate(tmp_path):
    (tmp_path / "api.py").write_text(
        "def missing_helper(x):\n    return x\n\ndef parse_value(x):\n    return int(x)\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from api import parse_value\n\ndef run(raw):\n    return parse_value(raw) + 1\n",
        encoding="utf-8",
    )

    # 1. Non-zero exit with "Symbol not found" for one symbol continues querying the second.
    runner_isolated = FakeCymbalRunner(
        refs={
            "missing_helper": CommandResult(
                argv=("cymbal", "refs", "--json", "--no-federate", "missing_helper"),
                returncode=1,
                stdout=b"",
                stderr=b"error: Symbol Not Found: missing_helper\n",
            ),
            "parse_value": {
                "definitions": [{"file": "api.py", "line": 4, "name": "parse_value"}],
                "references": [
                    {
                        "file": "consumer.py",
                        "line": 4,
                        "kind": "call",
                        "snippet": "    return parse_value(raw) + 1",
                    }
                ],
            },
        },
        impact={
            "missing_helper": {"error": "symbol not found in index"},
            "parse_value": {"direct_callers": []},
        },
    )
    res = find_callers_with_backend(
        tmp_path,
        "fix missing_helper and parse_value",
        ["api.py"],
        ["consumer.py"],
        backend=BACKEND_CYMBAL,
        runner=runner_isolated,
    )
    assert [e.path for e in res.excerpts] == ["consumer.py"]
    assert res.excerpts[0].symbol == "parse_value"
    assert any("symbol not found (missing_helper)" in d for d in res.diagnostics)

    # 2. Other CymbalExecutionError (not symbol-not-found) MUST still propagate loudly.
    runner_exec_err = FakeCymbalRunner(
        refs={
            "missing_helper": CommandResult(
                argv=("cymbal", "refs", "--json", "--no-federate", "missing_helper"),
                returncode=2,
                stdout=b"",
                stderr=b"fatal: index database corrupted\n",
            ),
            "parse_value": {"references": []},
        },
        impact={"missing_helper": {"direct_callers": []}, "parse_value": {"direct_callers": []}},
    )
    with pytest.raises(CymbalExecutionError, match="corrupted"):
        find_callers_with_backend(
            tmp_path,
            "fix missing_helper and parse_value",
            ["api.py"],
            ["consumer.py"],
            backend=BACKEND_CYMBAL,
            runner=runner_exec_err,
        )

    # 3. Protocol/timeout/version errors continue propagating loudly.
    runner_proto = FakeCymbalRunner(refs={"missing_helper": b"{bad json"})
    with pytest.raises(CymbalProtocolError):
        find_callers_with_backend(
            tmp_path,
            "fix missing_helper and parse_value",
            ["api.py"],
            ["consumer.py"],
            backend=BACKEND_CYMBAL,
            runner=runner_proto,
        )


def test_builtin_backend_large_file_skip_marks_lookup_result_incomplete(tmp_path):
    (tmp_path / "api.py").write_text(
        "def normalize_code(raw):\n    return raw.strip().upper()\n",
        encoding="utf-8",
    )
    padding = "\n".join(f"# pad {i:04d} " + ("x" * 64) for i in range(2200))
    (tmp_path / "consumer.py").write_text(
        f"from api import normalize_code\n{padding}\nval = normalize_code('a')\n",
        encoding="utf-8",
    )
    (tmp_path / "test_smoke.py").write_text(
        "from api import normalize_code\nassert normalize_code(' a ') == 'A'\n",
        encoding="utf-8",
    )

    res = find_callers_with_backend(
        tmp_path,
        "fix normalize_code",
        ["api.py"],
        ["consumer.py", "test_smoke.py"],
        backend=BACKEND_BUILTIN,
    )
    assert res.status == "INCOMPLETE"
    assert [e.path for e in res.excerpts] == ["test_smoke.py"]
    assert res.excerpts[0].incomplete is True
    assert any("128 KiB" in note for note in res.truncation)


def test_cymbal_scan_cap_candidate_is_incomplete_not_stale_or_128kib(tmp_path):
    (tmp_path / "parser.cpp").write_text(
        "int parse_value(int input) {\n    return input + 1;\n}\n",
        encoding="utf-8",
    )
    padding_lines = [f"// padding {i:04d} " + ("x" * 64) for i in range(60)]
    call_line = len(padding_lines) + 2
    (tmp_path / "consumer.cpp").write_text(
        "\n".join(padding_lines)
        + "\nint handle(int v) {\n    return parse_value(v);\n}\n",
        encoding="utf-8",
    )

    runner = FakeCymbalRunner(
        refs={
            "parse_value": {
                "definitions": [{"file": "parser.cpp", "line": 1, "name": "parse_value"}],
                "references": [
                    {
                        "file": "consumer.cpp",
                        "line": call_line,
                        "kind": "call",
                        "snippet": "    return parse_value(v);",
                    }
                ],
            }
        },
        impact={"parse_value": {"direct_callers": []}},
        changed={"changed": [], "stale": False},
        show={
            f"consumer.cpp:{call_line}": {
                "file": "consumer.cpp",
                "start_line": call_line,
                "end_line": call_line,
                "lines": [{"line": call_line, "content": "    return parse_value(v);"}],
            }
        },
    )
    cfg = CymbalConfig(enforce_pinned_version=False, max_scan_bytes=2048)
    report = CymbalEvidenceReport()
    res = find_callers_with_backend(
        tmp_path,
        "fix parse_value",
        ["parser.cpp"],
        ["consumer.cpp"],
        backend=BACKEND_CYMBAL,
        cymbal_config=cfg,
        runner=runner,
        report=report,
    )
    assert res.status == "INCOMPLETE"
    assert res.status != "NO_EVIDENCE"
    assert res.excerpts == ()
    assert report.skipped_large == 1
    assert "stale index" not in res.ambiguity
    assert any("scan limit (>2 KiB)" in note for note in res.truncation)
    assert not any("128 KiB" in note for note in res.truncation)

    # CymbalClient.show hitting max_scan_bytes marks truncated, NOT stale index.
    client = CymbalClient(cfg, runner=runner)
    show_res = client.show(tmp_path, f"consumer.cpp:{call_line}", verify_disk=True)
    assert show_res.truncated is True
    assert show_res.stale is False
    assert len(show_res.slices) == 1
    assert show_res.slices[0].truncated is True
    assert show_res.slices[0].stale is False
    assert "stale index" not in show_res.slices[0].ambiguity
    assert any("scan limit (>2 KiB)" in d for d in show_res.diagnostics)


def test_cymbal_binary_unreadable_candidate_and_oversized_editable_are_incomplete(tmp_path):
    (tmp_path / "api.py").write_text("def parse_value(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "binary_caller.py").write_bytes(b"from api import parse_value\n\x00parse_value(1)\n")

    runner = FakeCymbalRunner(
        refs={
            "parse_value": {
                "definitions": [{"file": "api.py", "line": 1, "name": "parse_value"}],
                "references": [{"file": "binary_caller.py", "line": 2, "kind": "call"}],
            }
        },
        impact={"parse_value": {"direct_callers": []}},
        changed={"changed": [], "stale": False},
    )
    res_bin = find_callers_with_backend(
        tmp_path,
        "fix parse_value",
        ["api.py"],
        ["binary_caller.py"],
        backend=BACKEND_CYMBAL,
        runner=runner,
    )
    assert res_bin.status == "INCOMPLETE"
    assert res_bin.excerpts == ()
    assert "unreadable or binary candidate file omitted" in res_bin.truncation
    assert any("unreadable or binary caller" in d for d in res_bin.diagnostics)

    # Oversized editable source file (> max_scan_bytes) yields INCOMPLETE, not NO_EVIDENCE.
    big_editable = tmp_path / "big_api.cpp"
    big_editable.write_text(
        ("// pad " + ("a" * 80) + "\n") * 50 + "int parse_value(int x) { return x; }\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.cpp").write_text("int run() { return parse_value(1); }\n", encoding="utf-8")
    cfg = CymbalConfig(enforce_pinned_version=False, max_scan_bytes=1024)
    ed_report = CymbalEvidenceReport()
    res_big_ed = find_callers_with_backend(
        tmp_path,
        "fix parse_value",
        ["big_api.cpp"],
        ["consumer.cpp"],
        backend=BACKEND_CYMBAL,
        cymbal_config=cfg,
        runner=FakeCymbalRunner(),
        report=ed_report,
    )
    assert res_big_ed.status == "INCOMPLETE"
    assert res_big_ed.excerpts == ()
    assert ed_report.skipped_large == 1
    assert any("editable file exceeded scan limit (>1 KiB)" in note for note in res_big_ed.truncation)
    assert not any("128 KiB" in note for note in res_big_ed.truncation)

    # Binary editable code file yields INCOMPLETE, whereas binary non-code asset does not.
    (tmp_path / "broken_api.py").write_bytes(b"def parse_value(x):\n\x00return x\n")
    res_bin_ed = find_callers_with_backend(
        tmp_path,
        "fix parse_value",
        ["broken_api.py"],
        ["consumer.cpp"],
        backend=BACKEND_CYMBAL,
        runner=FakeCymbalRunner(),
    )
    assert res_bin_ed.status == "INCOMPLETE"
    assert "unreadable or binary editable file omitted" in res_bin_ed.truncation

    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + (b"x" * 2048))
    empty_runner = FakeCymbalRunner(
        refs={"parse_value": {"references": []}},
        impact={"parse_value": {"direct_callers": []}},
        changed={"changed": [], "stale": False},
    )
    res_asset = find_callers_with_backend(
        tmp_path,
        "fix parse_value",
        ["api.py", "logo.png"],
        ["consumer.cpp"],
        backend=BACKEND_CYMBAL,
        cymbal_config=cfg,
        runner=empty_runner,
    )
    assert res_asset.status == "NO_EVIDENCE"


def test_auto_mode_propagates_cymbal_skipped_large_and_truncation(tmp_path):
    (tmp_path / "parser.cpp").write_text(
        "int parse_value(int input) {\n    return input + 1;\n}\n",
        encoding="utf-8",
    )
    padding_lines = [f"// pad {i:04d} " + ("z" * 64) for i in range(60)]
    call_line = len(padding_lines) + 2
    (tmp_path / "consumer.cpp").write_text(
        "\n".join(padding_lines)
        + "\nint handle(int v) {\n    return parse_value(v);\n}\n",
        encoding="utf-8",
    )

    runner = FakeCymbalRunner(
        refs={
            "parse_value": {
                "definitions": [{"file": "parser.cpp", "line": 1, "name": "parse_value"}],
                "references": [{"file": "consumer.cpp", "line": call_line, "kind": "call"}],
            }
        },
        impact={"parse_value": {"direct_callers": []}},
        changed={"changed": [], "stale": False},
    )
    cfg = CymbalConfig(enforce_pinned_version=False, max_scan_bytes=2048)
    report = CymbalEvidenceReport()
    res = find_callers_with_backend(
        tmp_path,
        "fix parse_value",
        ["parser.cpp"],
        ["consumer.cpp"],
        backend=BACKEND_AUTO,
        cymbal_config=cfg,
        runner=runner,
        report=report,
    )
    assert res.source == BACKEND_CYMBAL
    assert res.fallback_used is False
    assert res.status == "INCOMPLETE"
    assert report.skipped_large == 1
    assert "consumer.cpp" in report._skipped_large_paths
    assert any("scan limit (>2 KiB)" in note for note in res.truncation)
    assert not any("128 KiB" in note for note in res.truncation)


def test_c_cpp_target_extraction_declaration_rules_ts_and_regex(monkeypatch):
    import aider.cymbal_callers as cc

    c_decls_only = (
        "extern int parse_value(int input);\n"
        "int helper(int x);\n"
    )
    cpp_decls_only = (
        "extern int parse_value(int input);\n"
        "int helper(int x);\n"
        "class Engine {\n"
        "public:\n"
        "    int unbacked_method(int x);\n"
        "};\n"
    )
    cpp_with_defs = (
        "extern int parse_value(int input);\n"
        "int helper(int x);\n"
        "int *ptr_func(int x) { return nullptr; }\n"
        "class Engine {\n"
        "public:\n"
        "    int unbacked_method(int x);\n"
        "    int inline_method(int x) {\n"
        "        extern int block_extern(int y);\n"
        "        return block_extern(x);\n"
        "    }\n"
        "};\n"
        "int Engine::out_of_line(int x) { return x; }\n"
    )
    header_h = (
        "extern int parse_value(int input);\n"
        "int helper(int x);\n"
        "static inline int helper(int x) { return x + 1; }\n"
        "class Widget {\n"
        "public:\n"
        "    int render(int w);\n"
        "};\n"
    )
    header_hpp = (
        "extern int parse_value(int input);\n"
        "int helper(int x);\n"
        "inline int helper(int x) {\n"
        "    extern int block_extern(int y);\n"
        "    return block_extern(x);\n"
        "}\n"
        "class Widget {\n"
        "public:\n"
        "    int render(int w);\n"
        "    int draw(int w) {\n"
        "        extern int nested_extern(int z);\n"
        "        return nested_extern(w);\n"
        "    }\n"
        "};\n"
    )

    words_decl = {"parse_value", "helper", "unbacked_method"}
    words_all = {
        "parse_value",
        "helper",
        "ptr_func",
        "unbacked_method",
        "inline_method",
        "block_extern",
        "out_of_line",
        "render",
        "draw",
        "nested_extern",
    }

    for mode in ("tree_sitter", "regex_fallback"):
        if mode == "regex_fallback":
            monkeypatch.setattr(cc, "_extract_c_cpp_ts_targets", lambda path, source, words: [])

        # Non-header .c and .cpp MUST NOT extract unbacked extern / forward / method declarations.
        assert cc._extract_c_cpp_targets("mod.c", c_decls_only, words_decl) == []
        assert cc._extract_c_cpp_targets("mod.cpp", cpp_decls_only, words_decl) == []

        cpp_targets = cc._extract_c_cpp_targets("mod.cpp", cpp_with_defs, words_all)
        assert {t.symbol for t in cpp_targets} == {"ptr_func", "inline_method", "out_of_line"}

        # Headers .h and .hpp keep prototypes, externs, inline defs, and class method declarations,
        # preferring definitions over prototypes and excluding block-scoped externs in .hpp bodies.
        h_targets = cc._extract_c_cpp_targets("api.h", header_h, words_all)
        h_by_sym = {t.symbol: t for t in h_targets}
        assert set(h_by_sym) == {"parse_value", "helper", "render"}
        assert h_by_sym["helper"].line == 3  # definition preferred over line-2 prototype

        hpp_targets = cc._extract_c_cpp_targets("api.hpp", header_hpp, words_all)
        hpp_by_sym = {t.symbol: t for t in hpp_targets}
        assert set(hpp_by_sym) == {"parse_value", "helper", "render", "draw"}
        assert hpp_by_sym["helper"].line == 3  # definition preferred over line-2 prototype
        assert "block_extern" not in hpp_by_sym
        assert "nested_extern" not in hpp_by_sym


@pytest.mark.parametrize(
    "ambiguity_note",
    [
        "ambiguous symbol binding",
        "duplicate symbol definition",
        "ambiguous method receiver",
        "stale index",
    ],
)
def test_cymbal_zero_excerpts_with_ambiguity_is_incomplete(ambiguity_note):
    report = CymbalEvidenceReport(backend_requested=BACKEND_CYMBAL, backend_used=BACKEND_CYMBAL)
    report.ambiguity.append(ambiguity_note)

    res = report.to_result(())
    assert res.status == "INCOMPLETE"
    assert res.excerpts == ()
    assert ambiguity_note in res.ambiguity

    clean = CymbalEvidenceReport(
        backend_requested=BACKEND_CYMBAL, backend_used=BACKEND_CYMBAL
    ).to_result(())
    assert clean.status == "NO_EVIDENCE"


def test_cymbal_callers_imports_only_public_local_callers_contract():
    import ast

    cymbal_path = Path(__file__).resolve().parents[2] / "aider" / "cymbal_callers.py"
    tree = ast.parse(cymbal_path.read_text(encoding="utf-8"))

    imported_from_local_callers: list[str] = []
    private_imports: list[str] = []
    local_callers_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "aider.local_callers":
            for alias in node.names:
                imported_from_local_callers.append(alias.name)
                if alias.name.startswith("_"):
                    private_imports.append(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "aider.local_callers":
                    local_callers_aliases.add(alias.asname or "aider")

    assert imported_from_local_callers, "expected public imports from aider.local_callers"
    assert private_imports == [], (
        "aider/cymbal_callers.py must not import private local_callers internals: "
        f"{private_imports}"
    )

    if local_callers_aliases:
        private_attrs = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in local_callers_aliases
            and node.attr.startswith("_")
        ]
        assert private_attrs == []


def test_cymbal_same_file_callers_parity_and_cross_file_ranking(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    yajl_lines = [
        "static long long yajl_parse_integer(const unsigned char *number, unsigned int length);",
        "",
        "/* parser helper */",
        "long long yajl_parse_integer(const unsigned char *number, unsigned int length) {",
        "    return (long long)length;",
        "}",
        *[f"/* padding {i} */" for i in range(7, 18)],
        "long long yajl_do_parse(const unsigned char *buf, unsigned int len) {",
        "    long long v = 0;",
        "    v = yajl_parse_integer(buf, len);",
        "    return v;",
        "}",
    ]
    (src_dir / "yajl_parser.c").write_text("\n".join(yajl_lines) + "\n", encoding="utf-8")
    (src_dir / "zzz_external.c").write_text(
        "long long parse_ext(const unsigned char *b, unsigned int n) {\n"
        "    return yajl_parse_integer(b, n);\n"
        "}\n",
        encoding="utf-8",
    )

    # 1. Same-file caller returned while prototype (line 1) and definition (line 4) are skipped.
    same_file_runner = FakeCymbalRunner(
        refs={
            "yajl_parse_integer": {
                "definitions": [
                    {"file": "src/yajl_parser.c", "line": 4, "name": "yajl_parse_integer"}
                ],
                "references": [
                    {
                        "file": "src/yajl_parser.c",
                        "line": 1,
                        "kind": "reference",
                        "snippet": yajl_lines[0],
                    },
                    {
                        "file": "src/yajl_parser.c",
                        "line": 4,
                        "kind": "reference",
                        "snippet": yajl_lines[3],
                    },
                    {
                        "file": "src/yajl_parser.c",
                        "line": 20,
                        "kind": "call",
                        "snippet": "    v = yajl_parse_integer(buf, len);",
                    },
                ],
            }
        },
        impact={"yajl_parse_integer": {"direct_callers": []}},
    )
    same_only = find_callers_with_backend(
        tmp_path,
        "fix yajl_parse_integer",
        ["src/yajl_parser.c"],
        [],
        backend=BACKEND_CYMBAL,
        runner=same_file_runner,
    )
    assert same_only.status == "COMPLETE_WITHIN_SCOPE"
    assert len(same_only.excerpts) == 1
    assert same_only.excerpts[0].path == "src/yajl_parser.c"
    assert same_only.excerpts[0].line == 20
    assert "v = yajl_parse_integer(buf, len);" in same_only.excerpts[0].code

    # 2. Cross-file caller ranks strictly ahead of same-file caller (even when lexically later).
    both_runner = FakeCymbalRunner(
        refs={
            "yajl_parse_integer": {
                "definitions": [
                    {"file": "src/yajl_parser.c", "line": 4, "name": "yajl_parse_integer"}
                ],
                "references": [
                    {
                        "file": "src/yajl_parser.c",
                        "line": 4,
                        "kind": "reference",
                        "snippet": yajl_lines[3],
                    },
                    {
                        "file": "src/yajl_parser.c",
                        "line": 20,
                        "kind": "call",
                        "snippet": "    v = yajl_parse_integer(buf, len);",
                    },
                    {
                        "file": "src/zzz_external.c",
                        "line": 2,
                        "kind": "call",
                        "snippet": "    return yajl_parse_integer(b, n);",
                    },
                ],
            }
        },
        impact={"yajl_parse_integer": {"direct_callers": []}},
    )
    ranked = find_callers_with_backend(
        tmp_path,
        "fix yajl_parse_integer",
        ["src/yajl_parser.c"],
        ["src/zzz_external.c"],
        backend=BACKEND_CYMBAL,
        max_files=2,
        runner=both_runner,
    )
    assert [e.path for e in ranked.excerpts] == ["src/zzz_external.c", "src/yajl_parser.c"]

    capped = find_callers_with_backend(
        tmp_path,
        "fix yajl_parse_integer",
        ["src/yajl_parser.c"],
        ["src/zzz_external.c"],
        backend=BACKEND_CYMBAL,
        max_files=1,
        runner=both_runner,
    )
    assert [e.path for e in capped.excerpts] == ["src/zzz_external.c"]

    # 3. Recursive self-call inside target's own body is skipped; sibling caller is returned.
    (tmp_path / "algo.py").write_text(
        "def compute(n):\n"
        "    if n <= 1:\n"
        "        return 1\n"
        "    return n * compute(n - 1)\n"
        "\n"
        "def run(x):\n"
        "    return compute(x) + 1\n",
        encoding="utf-8",
    )
    py_runner = FakeCymbalRunner(
        refs={
            "compute": {
                "definitions": [{"file": "algo.py", "line": 1, "name": "compute"}],
                "references": [
                    {"file": "algo.py", "line": 1, "kind": "reference"},
                    {"file": "algo.py", "line": 4, "kind": "call"},
                    {
                        "file": "algo.py",
                        "line": 7,
                        "kind": "call",
                        "snippet": "    return compute(x) + 1",
                    },
                ],
            }
        },
        impact={"compute": {"direct_callers": []}},
    )
    py_res = find_callers_with_backend(
        tmp_path,
        "fix compute",
        ["algo.py"],
        ["algo.py"],
        backend=BACKEND_CYMBAL,
        runner=py_runner,
    )
    assert len(py_res.excerpts) == 1
    assert py_res.excerpts[0].path == "algo.py"
    assert py_res.excerpts[0].line == 7
    assert "return compute(x) + 1" in py_res.excerpts[0].code


def test_cymbal_vague_prompt_target_seeding_parity_and_c_decl_safety(tmp_path):
    import aider.cymbal_callers as cc

    # 1. Python vague prompt omitting exact function name seeds target via tiered selection.
    (tmp_path / "api.py").write_text(
        "def parse_payload(raw):\n    return int(raw)\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from api import parse_payload\n\ndef handle(x):\n    return parse_payload(x)\n",
        encoding="utf-8",
    )
    py_runner = FakeCymbalRunner(
        refs={
            "parse_payload": {
                "definitions": [{"file": "api.py", "line": 1, "name": "parse_payload"}],
                "references": [
                    {
                        "file": "consumer.py",
                        "line": 4,
                        "kind": "call",
                        "snippet": "    return parse_payload(x)",
                    }
                ],
            }
        },
        impact={"parse_payload": {"direct_callers": []}},
    )
    # 1a. Generic/unrelated prompt with zero lexical or sub-token overlap attaches NO callers.
    py_unrelated = find_callers_with_backend(
        tmp_path,
        "fix the bug",
        ["api.py"],
        ["consumer.py"],
        backend=BACKEND_CYMBAL,
        runner=py_runner,
    )
    assert py_unrelated.definitions == ()
    assert py_unrelated.excerpts == ()
    assert py_unrelated.status == "NO_EVIDENCE"

    # 1b. Python vague prompt with domain sub-token overlap seeds target via Tier 2.
    py_vague = find_callers_with_backend(
        tmp_path,
        "fix payload validation",
        ["api.py"],
        ["consumer.py"],
        backend=BACKEND_CYMBAL,
        runner=py_runner,
    )
    assert ("api.py", "parse_payload") in py_vague.definitions
    assert [e.path for e in py_vague.excerpts] == ["consumer.py"]

    # 2. C subtoken prompt omitting exact function name selects matching C definition only.
    (tmp_path / "parser.c").write_text(
        "void yajl_free_state(void *p) {\n"
        "    (void)p;\n"
        "}\n"
        "long long yajl_parse_integer(const unsigned char *num, unsigned int len) {\n"
        "    return (long long)len;\n"
        "}\n"
        "long long yajl_do_parse(const unsigned char *buf, unsigned int n) {\n"
        "    return yajl_parse_integer(buf, n);\n"
        "}\n",
        encoding="utf-8",
    )
    c_runner = FakeCymbalRunner(
        refs={
            "yajl_parse_integer": {
                "definitions": [{"file": "parser.c", "line": 4, "name": "yajl_parse_integer"}],
                "references": [
                    {
                        "file": "parser.c",
                        "line": 8,
                        "kind": "call",
                        "snippet": "    return yajl_parse_integer(buf, n);",
                    }
                ],
            }
        },
        impact={"yajl_parse_integer": {"direct_callers": []}},
    )
    c_vague = find_callers_with_backend(
        tmp_path,
        "fix negative integer overflow",
        ["parser.c"],
        [],
        backend=BACKEND_CYMBAL,
        runner=c_runner,
    )
    assert c_vague.definitions == (("parser.c", "yajl_parse_integer"),)
    assert len(c_vague.excerpts) == 1
    assert c_vague.excerpts[0].path == "parser.c"
    assert c_vague.excerpts[0].line == 8

    # 3. Non-header .c file with ONLY unbacked extern/forward declarations still yields [].
    (tmp_path / "decls_only.c").write_text(
        "extern int parse_value(int input);\nint helper(int x);\n",
        encoding="utf-8",
    )
    extracted, _ = cc._extract_editable_targets(
        tmp_path,
        ["decls_only.c"],
        {"fix", "the", "bug"},
        max_scan_bytes=65536,
    )
    assert extracted == {}
    decls_res = find_callers_with_backend(
        tmp_path,
        "fix the bug",
        ["decls_only.c"],
        ["consumer.py"],
        backend=BACKEND_CYMBAL,
        runner=FakeCymbalRunner(),
    )
    assert decls_res.definitions == ()
    assert decls_res.excerpts == ()
    assert decls_res.status == "NO_EVIDENCE"


def test_fallback_caller_finder_auto_mode_authoritative_cymbal_vs_error_fallback(tmp_path):
    from aider.cymbal_callers import FallbackCallerFinder

    _write_basic_repo(tmp_path)

    # Explicit docstring contract for `auto` mode.
    doc = (FallbackCallerFinder.__doc__ or "").lower()
    assert "auto" in doc
    assert "authoritative" in doc

    class SpyBuiltinFinder:
        def __init__(self):
            self.calls = 0
            self._inner = ReportingCompositeCallerFinder()

        def supports(self, path: str) -> bool:
            return self._inner.supports(path)

        def find_callers(self, *args, **kwargs):
            self.calls += 1
            return self._inner.find_callers(*args, **kwargs)

    # 1. Successful empty Cymbal result (NO_EVIDENCE) is authoritative; no silent builtin union.
    empty_runner = FakeCymbalRunner(
        refs={"parse": {"definitions": [{"file": "api.py", "line": 1}], "references": []}},
        impact={"parse": {"direct_callers": []}},
    )
    spy_empty = SpyBuiltinFinder()
    finder_empty = FallbackCallerFinder(
        backend=BACKEND_AUTO,
        cymbal_finder=CymbalCallerFinder(runner=empty_runner),
        builtin_finder=spy_empty,
    )
    res_empty = finder_empty.lookup(tmp_path, "fix parse", ["api.py"], ["consumer.py"])
    assert res_empty.source == BACKEND_CYMBAL
    assert res_empty.fallback_used is False
    assert res_empty.status == "NO_EVIDENCE"
    assert res_empty.excerpts == ()
    assert spy_empty.calls == 0

    # 2. Successful impaired Cymbal result (INCOMPLETE) is authoritative; no silent builtin union.
    stale_runner = FakeCymbalRunner(
        refs={
            "parse": {
                "definitions": [{"file": "api.py", "line": 1}],
                "references": [],
                "stale": True,
            }
        },
        impact={"parse": {"direct_callers": []}},
    )
    spy_incomplete = SpyBuiltinFinder()
    finder_incomplete = FallbackCallerFinder(
        backend=BACKEND_AUTO,
        cymbal_finder=CymbalCallerFinder(runner=stale_runner),
        builtin_finder=spy_incomplete,
    )
    res_incomplete = finder_incomplete.lookup(tmp_path, "fix parse", ["api.py"], ["consumer.py"])
    assert res_incomplete.source == BACKEND_CYMBAL
    assert res_incomplete.fallback_used is False
    assert res_incomplete.status == "INCOMPLETE"
    assert res_incomplete.excerpts == ()
    assert "stale index" in res_incomplete.ambiguity
    assert spy_incomplete.calls == 0

    # 3. CymbalError triggers fallback to builtin with diagnostics.
    err_runner = FakeCymbalRunner(
        handler=lambda *args: (_ for _ in ()).throw(CymbalTimeoutError("deadline exceeded"))
    )
    spy_fallback = SpyBuiltinFinder()
    finder_fallback = FallbackCallerFinder(
        backend=BACKEND_AUTO,
        cymbal_finder=CymbalCallerFinder(runner=err_runner),
        builtin_finder=spy_fallback,
    )
    res_fallback = finder_fallback.lookup(tmp_path, "fix parse", ["api.py"], ["consumer.py"])
    assert res_fallback.source == BACKEND_BUILTIN
    assert res_fallback.fallback_used is True
    assert res_fallback.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in res_fallback.excerpts] == ["consumer.py"]
    assert spy_fallback.calls == 1
    assert any("cymbal fallback (timeout)" in d for d in res_fallback.diagnostics)


def test_cymbal_extracts_bash_targets_and_extensionless_shebang_scripts(tmp_path):
    from aider import cymbal_callers as cc

    # 1. .bash file with POSIX, `function`, and hyphenated Bash definitions + sibling caller.
    bash_dir = tmp_path / "lib" / "bats-core"
    bash_dir.mkdir(parents=True)
    (bash_dir / "semaphore.bash").write_text(
        "bats_semaphore_setup() {\n"
        "  # don't clobber caller traps\n"
        "  export BATS_SEMAPHORE_DIR=/tmp/sem\n"
        "}\n"
        "\n"
        "function bats_semaphore_release_wrapper {\n"
        "  local status=$?\n"
        "  return \"$status\"\n"
        "}\n"
        "\n"
        "function bats-helper() {\n"
        "  echo helper\n"
        "}\n"
        "\n"
        "bats_semaphore_run() {\n"
        "  bats_semaphore_release_wrapper \"$@\"\n"
        "}\n",
        encoding="utf-8",
    )

    # 2. Extensionless Bash script identified via #!/usr/bin/env bash shebang.
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    (libexec / "rbenv-init").write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "\n"
        "rbenv() {\n"
        "  command rbenv \"$@\"\n"
        "}\n"
        "\n"
        "function detect_shell() {\n"
        "  echo bash\n"
        "}\n",
        encoding="utf-8",
    )
    completions = tmp_path / "completions"
    completions.mkdir()
    (completions / "rbenv.bash").write_text(
        "_rbenv() {\n"
        "  rbenv commands\n"
        "}\n",
        encoding="utf-8",
    )

    # 3. Non-code assets (binary image, markdown, extensionless plain text) must be ignored.
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + (b"x" * 1024))
    (tmp_path / "README.md").write_text("# Docs\nrbenv() {\n  echo nope\n}\n", encoding="utf-8")
    (tmp_path / "README").write_text("Plain readme without shebang\n", encoding="utf-8")

    # Direct extraction covers all Bash definition forms including hyphenated names.
    sem_src = (bash_dir / "semaphore.bash").read_text(encoding="utf-8")
    bash_defs = cc._extract_generic_targets("lib/bats-core/semaphore.bash", sem_src, None)
    by_name = {d.symbol: d for d in bash_defs}
    assert set(by_name) == {
        "bats_semaphore_setup",
        "bats_semaphore_release_wrapper",
        "bats-helper",
        "bats_semaphore_run",
    }
    assert by_name["bats_semaphore_setup"].line == 1
    assert by_name["bats_semaphore_setup"].end_line == 4
    assert by_name["bats_semaphore_release_wrapper"].line == 6
    assert by_name["bats_semaphore_release_wrapper"].end_line == 9

    # Supported path check recognizes .bash and extensionless shebangs, rejects non-code assets.
    assert cc._is_supported_editable_code_path("lib/bats-core/semaphore.bash", root=tmp_path)
    assert cc._is_supported_editable_code_path("libexec/rbenv-init", root=tmp_path)
    assert not cc._is_supported_editable_code_path("logo.png", root=tmp_path)
    assert not cc._is_supported_editable_code_path("README.md", root=tmp_path)
    assert not cc._is_supported_editable_code_path("README", root=tmp_path)

    # End-to-end Cymbal lookup on .bash file under a vague prompt finds the sibling caller.
    sem_runner = FakeCymbalRunner(
        refs={
            "bats_semaphore_release_wrapper": {
                "definitions": [
                    {
                        "file": "lib/bats-core/semaphore.bash",
                        "line": 6,
                        "name": "bats_semaphore_release_wrapper",
                    }
                ],
                "references": [
                    {
                        "file": "lib/bats-core/semaphore.bash",
                        "line": 16,
                        "kind": "call",
                        "snippet": '  bats_semaphore_release_wrapper "$@"',
                    }
                ],
            }
        },
        impact={"bats_semaphore_release_wrapper": {"direct_callers": []}},
    )
    # Zero-overlap prompt must not attach arbitrary callers (Precision Gate).
    unrelated_sem = find_callers_with_backend(
        tmp_path,
        "unrelated task",
        ["lib/bats-core/semaphore.bash"],
        [],
        backend=BACKEND_CYMBAL,
        runner=sem_runner,
    )
    assert unrelated_sem.status == "NO_EVIDENCE"
    assert unrelated_sem.definitions == ()
    assert unrelated_sem.excerpts == ()

    # End-to-end Cymbal lookup on .bash file under a vague domain prompt finds the sibling caller.
    sem_res = find_callers_with_backend(
        tmp_path,
        "fix semaphore release trap exit status",
        ["lib/bats-core/semaphore.bash"],
        [],
        backend=BACKEND_CYMBAL,
        runner=sem_runner,
    )
    assert ("lib/bats-core/semaphore.bash", "bats_semaphore_release_wrapper") in sem_res.definitions
    assert len(sem_res.excerpts) == 1
    assert sem_res.excerpts[0].path == "lib/bats-core/semaphore.bash"
    assert sem_res.excerpts[0].line == 16
    assert sem_res.excerpts[0].symbol == "bats_semaphore_release_wrapper"

    # End-to-end Cymbal lookup on extensionless shebang script + non-code assets stays clean.
    rbenv_runner = FakeCymbalRunner(
        refs={
            "rbenv": {
                "definitions": [{"file": "libexec/rbenv-init", "line": 4, "name": "rbenv"}],
                "references": [
                    {
                        "file": "completions/rbenv.bash",
                        "line": 2,
                        "kind": "call",
                        "snippet": "  rbenv commands",
                    }
                ],
            },
            "detect_shell": {
                "definitions": [{"file": "libexec/rbenv-init", "line": 8, "name": "detect_shell"}],
                "references": [],
            },
        },
        impact={"rbenv": {"direct_callers": []}, "detect_shell": {"direct_callers": []}},
    )
    rbenv_res = find_callers_with_backend(
        tmp_path,
        "fix rbenv profile output",
        ["libexec/rbenv-init", "logo.png", "README.md", "README"],
        ["completions/rbenv.bash"],
        backend=BACKEND_CYMBAL,
        runner=rbenv_runner,
    )
    assert rbenv_res.status == "COMPLETE_WITHIN_SCOPE"
    assert rbenv_res.definitions == (("libexec/rbenv-init", "rbenv"),)
    assert [e.path for e in rbenv_res.excerpts] == ["completions/rbenv.bash"]
    assert "unreadable or binary editable file omitted" not in rbenv_res.truncation

    # Zero-overlap prompt on extensionless shebang script must NOT attach arbitrary callers.
    rbenv_unrelated = find_callers_with_backend(
        tmp_path,
        "fix auto detection",
        ["libexec/rbenv-init", "logo.png", "README.md", "README"],
        ["completions/rbenv.bash"],
        backend=BACKEND_CYMBAL,
        runner=rbenv_runner,
    )
    assert rbenv_unrelated.status == "NO_EVIDENCE"
    assert rbenv_unrelated.definitions == ()
    assert rbenv_unrelated.excerpts == ()
    assert "unreadable or binary editable file omitted" not in rbenv_unrelated.truncation

    # Sub-token prompt on extensionless shebang script selects only the matching Bash definition.
    rbenv_subtoken = find_callers_with_backend(
        tmp_path,
        "fix shell profile handling",
        ["libexec/rbenv-init", "logo.png", "README.md", "README"],
        ["completions/rbenv.bash"],
        backend=BACKEND_CYMBAL,
        runner=rbenv_runner,
    )
    assert rbenv_subtoken.definitions == (("libexec/rbenv-init", "detect_shell"),)
    assert "unreadable or binary editable file omitted" not in rbenv_subtoken.truncation
