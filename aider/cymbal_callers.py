"""Cymbal v0.18.0 caller adapter, bounded subprocess runner, and backend composition.

Architecture:
- Pure boundary DTOs & evidence contract (`CallerEvidenceItem`, `CallerLookupResult`,
  `CymbalEvidenceReport`) keep scope, ambiguity, truncation, and diagnostics uniform
  across built-in and Cymbal adapters.
- Runner port (`CymbalCommandRunner`) and bounded OS adapter (`SubprocessCymbalRunner`)
  enforce wall-clock deadlines, hard stdout/stderr byte caps, `shell=False`, and
  process-group termination without external dependencies.
- `CymbalClient` pins `v0.18.0` JSON handling for `refs`, `impact`, `changed`, and `show`
  with `--no-federate` always enforced and strict path/freshness validation.
- `CymbalCallerFinder` implements `aider.local_callers.CallerFinder`, while
  `ReportingCompositeCallerFinder` and `FallbackCallerFinder` propagate report state
  across built-in and Cymbal backends with `builtin` default, diagnostic `auto`
  fallback, and explicit errors in `cymbal` mode.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace
import inspect
import json
import os
from pathlib import Path
import posixpath
import re
import signal
import subprocess
import threading
import time
from typing import Any, Iterable, Literal, Protocol, Sequence
import warnings

from aider.local_callers import (
    CONTROL_KEYWORDS,
    DEF_REGEXES,
    LARGE_FILE_LIMITATION,
    WORD_RE,
    CallerEvidenceReport,
    CallerExcerpt,
    CallerFinder,
    PythonAstCallerFinder,
    TreeSitterCallerFinder,
    has_function_definition_declarator_ancestor,
    is_c_cpp_header,
    is_test_path,
    resolve_lang,
    select_target_definitions,
)

PINNED_CYMBAL_VERSION = "0.18.0"

BACKEND_BUILTIN: Literal["builtin"] = "builtin"
BACKEND_AUTO: Literal["auto"] = "auto"
BACKEND_CYMBAL: Literal["cymbal"] = "cymbal"
VALID_CALLER_BACKENDS = (BACKEND_BUILTIN, BACKEND_AUTO, BACKEND_CYMBAL)

DEFAULT_COMMAND_TIMEOUT_SECONDS = 5.0
DEFAULT_TOTAL_DEADLINE_SECONDS = 12.0
DEFAULT_MAX_OUTPUT_BYTES = 262_144
DEFAULT_MAX_STDERR_BYTES = 16_384
DEFAULT_MAX_SCAN_BYTES = 2 * 1024 * 1024
DEFAULT_SHOW_MAX_LINES = 120
DEFAULT_SHOW_MAX_BYTES = 16_384

EvidenceStatus = Literal["NO_EVIDENCE", "INCOMPLETE", "COMPLETE_WITHIN_SCOPE"]
CallerBackend = Literal["builtin", "auto", "cymbal"]

_SEMVER_RE = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")
_ALIAS_KINDS = frozenset({"alias", "reexport", "re-export", "import_alias", "indirect"})
_IMPORT_KINDS = frozenset({"import", "import_alias", "reexport", "re-export"})
_CALL_KINDS = frozenset({"call", "method_call", "function_call", "invocation"})
_DEFINITION_KINDS = frozenset({"definition", "def", "declaration", "decl"})

_REFS_SCHEMA_KEYS = frozenset(
    {
        "version",
        "references",
        "refs",
        "matches",
        "results",
        "items",
        "callers",
        "definitions",
        "defs",
        "candidates",
        "symbol",
        "ambiguous",
        "stale",
        "truncated",
    }
)
_IMPACT_SCHEMA_KEYS = frozenset(
    {
        "version",
        "direct_callers",
        "direct",
        "transitive_callers",
        "transitive",
        "indirect_callers",
        "callers",
        "impacted",
        "affected",
        "affected_files",
        "references",
        "results",
        "definitions",
        "definition_count",
        "depth",
        "limit",
        "metrics",
        "production_callers",
        "raw_rows",
        "resolve_scope",
        "symbols",
        "test_callers",
        "total_callers",
        "symbol",
        "ambiguous",
        "stale",
        "truncated",
    }
)
_CHANGED_SCHEMA_KEYS = frozenset(
    {
        "version",
        "analyzed",
        "base",
        "changed_symbols",
        "resolve_scope",
        "changed",
        "changes",
        "symbols",
        "entries",
        "items",
        "results",
        "files",
        "changed_files",
        "stale",
        "index_stale",
        "outdated",
        "truncated",
    }
)
_SHOW_SCHEMA_KEYS = frozenset(
    {
        "version",
        "file",
        "path",
        "filename",
        "rel_path",
        "symbol",
        "code",
        "content",
        "body",
        "text",
        "snippet",
        "lines",
        "start_line",
        "end_line",
        "line",
        "matches",
        "definitions",
        "results",
        "items",
        "slices",
        "ambiguous",
        "stale",
        "truncated",
    }
)


class CymbalError(RuntimeError):
    """Base error for explicit Cymbal adapter failures."""

    def __init__(self, message: str, *, code: str = "cymbal_error", details: str = ""):
        super().__init__(message)
        self.code = code
        self.details = details


class CymbalNotFoundError(CymbalError):
    """Raised when the configured Cymbal executable cannot be found."""

    def __init__(self, message: str, *, details: str = ""):
        super().__init__(message, code="not_found", details=details)


class CymbalVersionError(CymbalError):
    """Raised when the Cymbal binary does not match the pinned version."""

    def __init__(self, message: str, *, details: str = ""):
        super().__init__(message, code="version_mismatch", details=details)


class CymbalTimeoutError(CymbalError):
    """Raised when a Cymbal subprocess or total lookup exceeds its deadline."""

    def __init__(self, message: str, *, details: str = ""):
        super().__init__(message, code="timeout", details=details)


class CymbalOutputLimitError(CymbalError):
    """Raised when Cymbal stdout exceeds the configured hard byte cap."""

    def __init__(self, message: str, *, details: str = ""):
        super().__init__(message, code="output_limit_exceeded", details=details)


class CymbalProtocolError(CymbalError):
    """Raised when Cymbal output is malformed or violates the pinned JSON schema."""

    def __init__(self, message: str, *, details: str = ""):
        super().__init__(message, code="protocol_error", details=details)


class CymbalExecutionError(CymbalError):
    """Raised when Cymbal exits non-zero or reports an execution failure."""

    def __init__(self, message: str, *, details: str = "", returncode: int | None = None):
        super().__init__(message, code="execution_failed", details=details)
        self.returncode = returncode


@dataclass(frozen=True)
class CommandResult:
    """Immutable result of running a bounded external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    elapsed_seconds: float = 0.0


class CymbalCommandRunner(Protocol):
    """Port interface for executing bounded Cymbal commands."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandResult:
        """Execute ``argv`` in ``cwd`` with hard timeout and stdout byte limits."""
        ...


@dataclass(frozen=True)
class CymbalConfig:
    """Configuration for Cymbal client execution and adapter behavior."""

    executable: str = "cymbal"
    expected_version: str = PINNED_CYMBAL_VERSION
    enforce_pinned_version: bool = True
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS
    total_deadline_seconds: float = DEFAULT_TOTAL_DEADLINE_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES
    use_changed_check: bool = True
    use_impact: bool = True
    use_show: bool = False


@dataclass(frozen=True)
class CymbalDefinition:
    """Normalized definition entry returned by Cymbal."""

    path: str
    line: int
    symbol: str
    kind: str = "definition"


@dataclass(frozen=True)
class CymbalReference:
    """Normalized reference or caller entry returned by Cymbal."""

    path: str
    line: int
    symbol: str
    kind: str = "reference"
    column: int = 0
    snippet: str = ""
    caller_symbol: str = ""
    is_alias: bool = False
    transitive: bool = False


@dataclass(frozen=True)
class CymbalRefsResult:
    """Parsed `cymbal refs --json --no-federate` payload."""

    symbol: str
    references: tuple[CymbalReference, ...]
    definitions: tuple[CymbalDefinition, ...] = ()
    ambiguous: bool = False
    stale: bool = False
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CymbalImpactResult:
    """Parsed `cymbal impact --json --no-federate` payload."""

    symbol: str
    direct_callers: tuple[CymbalReference, ...]
    transitive_callers: tuple[CymbalReference, ...] = ()
    definitions: tuple[CymbalDefinition, ...] = ()
    ambiguous: bool = False
    stale: bool = False
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CymbalChangedEntry:
    """Single changed symbol or file entry reported by `cymbal changed`."""

    path: str
    symbol: str = ""
    line: int = 0
    kind: str = "changed"
    status: str = "modified"


@dataclass(frozen=True)
class CymbalChangedResult:
    """Parsed `cymbal changed --json --no-federate` payload."""

    entries: tuple[CymbalChangedEntry, ...]
    changed_files: tuple[str, ...]
    stale: bool = False
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CymbalShowSlice:
    """Validated bounded code slice returned by `cymbal show`."""

    path: str
    start_line: int
    end_line: int
    symbol: str
    kind: str
    code: str
    stale: bool = False
    truncated: bool = False
    ambiguity: tuple[str, ...] = ()


@dataclass(frozen=True)
class CymbalShowResult:
    """Parsed and validated `cymbal show --json --no-federate` payload."""

    target: str
    slices: tuple[CymbalShowSlice, ...]
    stale: bool = False
    ambiguous: bool = False
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CallerEvidenceItem:
    """Immutable typed evidence record shared across built-in and Cymbal backends."""

    path: str
    line: int
    symbol: str
    kind: str
    source: str
    scope: tuple[str, ...] = ()
    ambiguity: tuple[str, ...] = ()
    truncation: tuple[str, ...] = ()


@dataclass(frozen=True)
class CallerLookupResult:
    """Immutable typed lookup outcome propagating scope, ambiguity, and truncation."""

    status: EvidenceStatus
    excerpts: tuple[CallerExcerpt, ...]
    items: tuple[CallerEvidenceItem, ...]
    definitions: tuple[tuple[str, str], ...]
    source: str
    backend_requested: CallerBackend
    fallback_used: bool
    scope: tuple[str, ...]
    ambiguity: tuple[str, ...]
    truncation: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()


def _unique_ordered(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return tuple(ordered)


def classify_limitation(limitation: str) -> Literal["ambiguity", "truncation"]:
    """Classify a limitation label into ambiguity vs truncation for uniform reporting."""
    lower = limitation.lower()
    if any(
        token in lower
        for token in (
            "truncat",
            "omitted",
            "unmatched",
            "capped",
            "limit",
            "budget",
            "max_",
        )
    ):
        return "truncation"
    return "ambiguity"


@dataclass
class CymbalEvidenceReport(CallerEvidenceReport):
    """Extended `CallerEvidenceReport` collecting structured scope/ambiguity/truncation."""

    backend_requested: CallerBackend = BACKEND_BUILTIN
    backend_used: str = BACKEND_BUILTIN
    fallback_used: bool = False
    scope: list[str] = field(default_factory=list)
    ambiguity: list[str] = field(default_factory=list)
    truncation: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    items: list[CallerEvidenceItem] = field(default_factory=list)

    def add_scope(self, note: str) -> None:
        if not note:
            return
        if not isinstance(self.scope, list):
            self.scope = []
        if note not in self.scope:
            self.scope.append(note)

    def add_ambiguity(self, note: str) -> None:
        if note and note not in self.ambiguity:
            self.ambiguity.append(note)

    def add_truncation(self, note: str) -> None:
        if note and note not in self.truncation:
            self.truncation.append(note)

    def add_diagnostic(self, note: str) -> None:
        if note and note not in self.diagnostics:
            self.diagnostics.append(note)

    def add_item(self, item: CallerEvidenceItem) -> None:
        self.items.append(item)
        for note in item.scope:
            self.add_scope(note)
        for note in item.ambiguity:
            self.add_ambiguity(note)
        for note in item.truncation:
            self.add_truncation(note)

    def to_result(self, excerpts: Sequence[CallerExcerpt] = ()) -> CallerLookupResult:
        """Build an immutable `CallerLookupResult` from accumulated report evidence."""
        excerpt_tuple = tuple(excerpts)
        ambiguities = list(self.ambiguity)
        truncations = list(self.truncation)
        for lim in self.limitations:
            if classify_limitation(lim) == "truncation":
                truncations.append(lim)
            else:
                ambiguities.append(lim)
        for excerpt in excerpt_tuple:
            for lim in excerpt.limitations:
                if classify_limitation(lim) == "truncation":
                    truncations.append(lim)
                else:
                    ambiguities.append(lim)

        has_scan_limit_trunc = any(
            "scan limit" in note.lower() or "large" in note.lower() for note in truncations
        )
        if self.skipped_large > 0 and not has_scan_limit_trunc:
            truncations.append(LARGE_FILE_LIMITATION)

        unique_ambiguity = _unique_ordered(ambiguities)
        unique_truncation = _unique_ordered(truncations)
        if isinstance(self.scope, list):
            unique_scope = _unique_ordered(self.scope)
        elif hasattr(self.scope, "describe"):
            unique_scope = _unique_ordered((self.scope.describe(),))
        else:
            unique_scope = ()
        unique_diagnostics = _unique_ordered(self.diagnostics)

        if not excerpt_tuple:
            has_impairment = (
                self.skipped_large > 0
                or bool(unique_truncation)
                or bool(unique_ambiguity)
            )
            status: EvidenceStatus = "INCOMPLETE" if has_impairment else "NO_EVIDENCE"
        elif (
            unique_ambiguity
            or unique_truncation
            or self.skipped_large > 0
            or any(e.incomplete for e in excerpt_tuple)
        ):
            status = "INCOMPLETE"
        else:
            status = "COMPLETE_WITHIN_SCOPE"

        return CallerLookupResult(
            status=status,
            excerpts=excerpt_tuple,
            items=tuple(self.items),
            definitions=tuple(sorted(self.definitions)),
            source=self.backend_used,
            backend_requested=self.backend_requested,
            fallback_used=self.fallback_used,
            scope=unique_scope,
            ambiguity=unique_ambiguity,
            truncation=unique_truncation,
            diagnostics=unique_diagnostics,
        )

    def describe(self, excerpts: Sequence[CallerExcerpt] | None = None) -> str:
        """Render a human-readable summary compatible with `CallerEvidenceReport`."""
        result = self.to_result(excerpts or ())
        definitions = list(result.definitions)
        targets = ", ".join(f"{path}:{symbol}" for path, symbol in definitions[:8]) or "none"
        if len(definitions) > 8:
            targets += ", [additional definitions omitted]"
        sources = ", ".join(e.format().splitlines()[0] for e in result.excerpts) or "none"
        scope_label = ", ".join(result.scope) if result.scope else "first direct calls only"
        parts = [
            f"{result.status} ({scope_label}).",
            f"Backend: {result.source}.",
            f"Definitions: {targets}.",
            f"Sources: {sources}",
        ]
        if result.ambiguity:
            parts.append(f"Ambiguity: {', '.join(result.ambiguity)}")
        if result.truncation:
            parts.append(f"Truncation: {', '.join(result.truncation)}")
        if result.diagnostics:
            parts.append(f"Diagnostics: {'; '.join(result.diagnostics)}")
        return " ".join(parts)


def _report_add_definition(report: Any, path: str, symbol: str) -> None:
    if report is None:
        return
    defs = getattr(report, "definitions", None)
    if isinstance(defs, set):
        defs.add((path, symbol))


def _report_add_list_or_call(
    report: Any, method_name: str, attr_names: tuple[str, ...], value: str
) -> None:
    if report is None or not value:
        return
    method = getattr(report, method_name, None)
    if callable(method):
        method(value)
        return
    for attr in attr_names:
        container = getattr(report, attr, None)
        if isinstance(container, list):
            if value not in container:
                container.append(value)
            return
        if isinstance(container, set):
            container.add(value)
            return


def _report_add_scope(report: Any, note: str) -> None:
    _report_add_list_or_call(report, "add_scope", ("scope", "scopes"), note)


def _report_add_ambiguity(report: Any, note: str) -> None:
    _report_add_list_or_call(report, "add_ambiguity", ("ambiguity", "ambiguities"), note)


def _report_add_truncation(report: Any, note: str) -> None:
    _report_add_list_or_call(report, "add_truncation", ("truncation", "truncations"), note)


def _report_add_diagnostic(report: Any, note: str) -> None:
    _report_add_list_or_call(report, "add_diagnostic", ("diagnostics",), note)


def _format_byte_limit(max_bytes: int) -> str:
    if max_bytes >= 1024 * 1024 and max_bytes % (1024 * 1024) == 0:
        return f"{max_bytes // (1024 * 1024)} MiB"
    if max_bytes >= 1024 and max_bytes % 1024 == 0:
        return f"{max_bytes // 1024} KiB"
    if max_bytes >= 1024 * 1024:
        return f"{max_bytes / (1024 * 1024):.1f} MiB"
    if max_bytes >= 1024:
        return f"{max_bytes / 1024:.1f} KiB"
    return f"{max_bytes} B"


def _format_scan_limit_note(max_scan_bytes: int, *, role: str = "candidate") -> str:
    return f"{role} file exceeded scan limit (>{_format_byte_limit(max_scan_bytes)})"


def _report_record_skipped_large(
    report: Any, path: str, *, reason: str | None = None
) -> None:
    if report is None:
        return
    method = getattr(report, "record_skipped_large", None)
    if callable(method):
        try:
            sig = inspect.signature(method)
            if "reason" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                method(path, reason=reason)
                return
        except (TypeError, ValueError):
            pass
        method(path)
        if reason:
            _report_add_truncation(report, reason)
        return
    if hasattr(report, "skipped_large"):
        report.skipped_large += 1
    if reason:
        _report_add_truncation(report, reason)


def _report_add_item(report: Any, item: CallerEvidenceItem) -> None:
    if report is None:
        return
    method = getattr(report, "add_item", None)
    if callable(method):
        method(item)
        return
    items = getattr(report, "items", None)
    if isinstance(items, list):
        items.append(item)
    for note in item.scope:
        _report_add_scope(report, note)
    for note in item.ambiguity:
        _report_add_ambiguity(report, note)
    for note in item.truncation:
        _report_add_truncation(report, note)


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


class SubprocessCymbalRunner:
    """Safe, bounded subprocess executor enforcing hard deadlines and byte limits."""

    def __init__(self, *, max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES):
        self.max_stderr_bytes = max(1024, int(max_stderr_bytes))

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandResult:
        if not argv:
            raise CymbalExecutionError("Empty command argv")
        if timeout_seconds <= 0:
            raise CymbalTimeoutError("Cymbal command deadline exhausted before launch")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")

        argv_tuple = tuple(str(arg) for arg in argv)
        started = time.monotonic()
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(list(argv_tuple), **popen_kwargs)
        except FileNotFoundError as exc:
            raise CymbalNotFoundError(
                f"Cymbal executable not found: {argv_tuple[0]!r}",
                details=str(exc),
            ) from exc
        except OSError as exc:
            raise CymbalExecutionError(
                f"Failed to start Cymbal executable {argv_tuple[0]!r}: {exc}",
                details=str(exc),
            ) from exc

        stdout_buf = bytearray()
        stderr_buf = bytearray()
        stdout_truncated = [False]
        stderr_truncated = [False]
        abort_event = threading.Event()

        def _drain_stdout() -> None:
            stream = proc.stdout
            if stream is None:
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    remaining = max_output_bytes - len(stdout_buf)
                    if remaining > 0:
                        stdout_buf.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        stdout_truncated[0] = True
                        abort_event.set()
                        break
            except OSError:
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        def _drain_stderr() -> None:
            stream = proc.stderr
            if stream is None:
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    remaining = self.max_stderr_bytes - len(stderr_buf)
                    if remaining > 0:
                        stderr_buf.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        stderr_truncated[0] = True
                        if abort_event.is_set():
                            break
            except OSError:
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        t_out = threading.Thread(target=_drain_stdout, daemon=True)
        t_err = threading.Thread(target=_drain_stderr, daemon=True)
        t_out.start()
        t_err.start()

        deadline = started + timeout_seconds
        timed_out = False
        try:
            while True:
                if abort_event.is_set():
                    _terminate_process(proc)
                    break
                if proc.poll() is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _terminate_process(proc)
                    break
                abort_event.wait(timeout=min(0.01, remaining))

            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _terminate_process(proc)
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

        elapsed = max(0.0, time.monotonic() - started)
        returncode = proc.returncode if proc.returncode is not None else -1
        return CommandResult(
            argv=argv_tuple,
            returncode=returncode,
            stdout=bytes(stdout_buf),
            stderr=bytes(stderr_buf),
            timed_out=timed_out,
            stdout_truncated=stdout_truncated[0],
            stderr_truncated=stderr_truncated[0],
            elapsed_seconds=elapsed,
        )


def parse_cymbal_version(raw: str) -> str | None:
    """Extract semantic version ``X.Y.Z`` from ``cymbal --version`` output."""
    if not raw:
        return None
    match = _SEMVER_RE.search(raw.strip())
    return match.group(1) if match else None


def normalize_repo_path(root: Path | str, raw_path: str) -> str | None:
    """Safely normalize ``raw_path`` to a POSIX path strictly inside ``root``.

    Rejects empty strings, control characters, path traversal, symlinks (file or
    intermediate parent directories), directories, and files outside ``root``.
    """
    if not raw_path or not isinstance(raw_path, str):
        return None
    if "\x00" in raw_path or "\r" in raw_path or "\n" in raw_path:
        return None
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        return None

    root_path = Path(root)
    try:
        resolved_root = root_path.resolve()
    except OSError:
        return None

    if len(cleaned) >= 2 and cleaned[1] == ":" and cleaned[0].isalpha():
        candidate_raw = Path(cleaned)
        if not candidate_raw.is_absolute():
            return None
    else:
        candidate_raw = Path(cleaned)

    if candidate_raw.is_absolute():
        rel: Path | None = None
        for base in (resolved_root, root_path):
            try:
                rel = candidate_raw.relative_to(base)
                break
            except ValueError:
                continue
        if rel is None:
            return None
        norm_rel = posixpath.normpath(rel.as_posix())
        if norm_rel in ("", ".") or norm_rel == ".." or norm_rel.startswith("../"):
            return None
        rel = Path(norm_rel)
    else:
        norm_rel = posixpath.normpath(cleaned)
        if norm_rel in ("", ".") or norm_rel == ".." or norm_rel.startswith("../"):
            return None
        rel = Path(norm_rel)

    if any(part in ("", ".", "..") for part in rel.parts):
        return None

    cur = resolved_root
    try:
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                return None
        resolved_target = cur.resolve()
    except OSError:
        return None

    if (
        resolved_target != cur
        or not resolved_target.is_relative_to(resolved_root)
        or not resolved_target.is_file()
    ):
        return None

    return rel.as_posix()


def _truncate_utf8_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    if max_bytes <= 0:
        return "", True
    end = max_bytes
    while end > 0:
        try:
            return raw[:end].decode("utf-8"), True
        except UnicodeDecodeError:
            end -= 1
    return "", True


def read_bounded_lines(
    root: Path | str,
    rel_path: str,
    start_line: int,
    end_line: int,
    *,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
) -> tuple[list[str], int, bool] | None:
    """Safely stream lines ``[start_line, end_line]`` (1-based inclusive) from disk.

    Returns ``(lines, total_lines_scanned, hit_scan_cap)`` or ``None`` if the path is
    unsafe, binary (contains NUL), or invalid UTF-8.
    """
    if start_line < 1 or end_line < start_line or max_scan_bytes <= 0:
        return None
    norm = normalize_repo_path(root, rel_path)
    if norm is None:
        return None
    full_path = Path(root).resolve() / norm

    collected: list[str] = []
    scanned_bytes = 0
    current_line = 0
    hit_scan_cap = False
    try:
        with full_path.open("rb") as stream:
            for raw_line in stream:
                scanned_bytes += len(raw_line)
                if b"\x00" in raw_line:
                    return None
                if scanned_bytes > max_scan_bytes:
                    hit_scan_cap = True
                    break
                current_line += 1
                try:
                    decoded = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    return None
                if current_line >= start_line:
                    collected.append(decoded.rstrip("\r\n"))
                if current_line >= end_line:
                    break
    except OSError:
        return None

    return collected, current_line, hit_scan_cap


@dataclass(frozen=True)
class BoundedWindow:
    """Bounded UTF-8 text window safely read around a target 1-based line."""

    start_line: int
    text: str
    call_line_text: str
    line_exists: bool
    truncated: bool
    hit_scan_cap: bool = False


def read_bounded_window(
    root: Path | str,
    rel_path: str,
    center_line: int,
    *,
    context_lines: int = 3,
    max_chars: int = 700,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
) -> BoundedWindow | None:
    """Read a bounded line window around 1-based ``center_line``.

    Returns a ``BoundedWindow`` or ``None`` if the file cannot be safely read as
    UTF-8 text (e.g., missing, unsafe path, symlink, binary NUL, or invalid UTF-8).
    """
    if center_line < 1 or max_chars <= 0:
        return None
    ctx = max(0, context_lines)
    start_line = max(1, center_line - ctx)
    end_line = center_line + ctx
    result = read_bounded_lines(
        root,
        rel_path,
        start_line,
        end_line,
        max_scan_bytes=max_scan_bytes,
    )
    if result is None:
        return None
    lines, last_line, hit_scan_cap = result
    line_exists = last_line >= center_line and (center_line - start_line) < len(lines)
    if not line_exists:
        return BoundedWindow(
            start_line=start_line,
            text="",
            call_line_text="",
            line_exists=False,
            truncated=False,
            hit_scan_cap=hit_scan_cap,
        )
    center_text = lines[center_line - start_line]
    snippet = "\n".join(lines)
    truncated_chars = len(snippet) > max_chars
    return BoundedWindow(
        start_line=start_line,
        text=snippet[:max_chars],
        call_line_text=center_text,
        line_exists=True,
        truncated=truncated_chars,
        hit_scan_cap=hit_scan_cap,
    )


def _validate_cli_arg(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise CymbalProtocolError(f"Invalid {label}: expected string")
    cleaned = value.strip()
    if not cleaned:
        raise CymbalProtocolError(f"Empty {label}")
    if "\x00" in cleaned or "\r" in cleaned or "\n" in cleaned:
        raise CymbalProtocolError(f"Unsafe control character in {label}")
    if cleaned.startswith("-"):
        raise CymbalProtocolError(f"Refusing flag-like {label}: {cleaned!r}")
    return cleaned


def _coerce_positive_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def _extract_str(mapping: dict[str, Any], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        val = mapping.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return default


def _extract_bool(mapping: dict[str, Any], keys: tuple[str, ...]) -> bool:
    for key in keys:
        val = mapping.get(key)
        if isinstance(val, bool) and val:
            return True
    return False


class CymbalClient:
    """Pinned Cymbal v0.18.0 JSON client using `--no-federate` and bounded execution."""

    def __init__(
        self,
        config: CymbalConfig | None = None,
        *,
        runner: CymbalCommandRunner | None = None,
    ):
        self.config = config or CymbalConfig()
        self.runner: CymbalCommandRunner = runner or SubprocessCymbalRunner(
            max_stderr_bytes=self.config.max_stderr_bytes
        )
        self._verified_version: str | None = None

    def _effective_timeout(self, deadline: float | None) -> float:
        timeout = float(self.config.timeout_seconds)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CymbalTimeoutError("Cymbal lookup deadline exceeded")
            timeout = min(timeout, remaining)
        if timeout <= 0:
            raise CymbalTimeoutError("Cymbal timeout must be positive")
        return timeout

    def _run_raw(
        self,
        root: Path | str,
        argv: Sequence[str],
        *,
        deadline: float | None = None,
    ) -> CommandResult:
        resolved_root = Path(root).resolve()
        timeout = self._effective_timeout(deadline)
        result = self.runner.run(
            argv,
            cwd=resolved_root,
            timeout_seconds=timeout,
            max_output_bytes=self.config.max_output_bytes,
        )
        if result.timed_out:
            raise CymbalTimeoutError(
                f"Cymbal command timed out after {timeout:.2f}s: {' '.join(argv[:3])}",
                details=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        if result.stdout_truncated:
            raise CymbalOutputLimitError(
                f"Cymbal output exceeded hard cap of {self.config.max_output_bytes} bytes",
                details=" ".join(argv[:3]),
            )
        if result.returncode != 0:
            stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
            preview = stderr_text[:400] if stderr_text else f"exit status {result.returncode}"
            raise CymbalExecutionError(
                f"Cymbal command failed ({result.returncode}): {preview}",
                details=stderr_text,
                returncode=result.returncode,
            )
        return result

    def get_version(self, root: Path | str, *, deadline: float | None = None) -> str:
        """Query the Cymbal binary version and parse semantic version ``X.Y.Z``."""
        argv = (self.config.executable, "--version")
        result = self._run_raw(root, argv, deadline=deadline)
        combined = (
            result.stdout.decode("utf-8", errors="replace")
            + "\n"
            + result.stderr.decode("utf-8", errors="replace")
        )
        version = parse_cymbal_version(combined)
        if not version:
            raise CymbalProtocolError(
                f"Unable to parse Cymbal version from output: {combined.strip()[:120]!r}"
            )
        return version

    def verify_version(
        self,
        root: Path | str,
        *,
        expected: str | None = None,
        deadline: float | None = None,
    ) -> str:
        """Verify that the Cymbal binary matches the pinned version."""
        target = (expected or self.config.expected_version).lstrip("v")
        if self._verified_version == target:
            return self._verified_version
        actual = self.get_version(root, deadline=deadline)
        if actual != target:
            raise CymbalVersionError(
                f"Expected pinned Cymbal v{target}, found v{actual}"
            )
        self._verified_version = actual
        return actual

    def _ensure_version(self, root: Path | str, *, deadline: float | None = None) -> None:
        if self.config.enforce_pinned_version:
            self.verify_version(root, deadline=deadline)

    def _run_json(
        self,
        root: Path | str,
        subcommand: str,
        extra_args: Sequence[str] = (),
        *,
        deadline: float | None = None,
    ) -> Any:
        self._ensure_version(root, deadline=deadline)
        argv = (
            self.config.executable,
            subcommand,
            "--json",
            "--no-federate",
            *extra_args,
        )
        if "--no-federate" not in argv:
            raise CymbalProtocolError("Refusing Cymbal invocation without --no-federate")
        result = self._run_raw(root, argv, deadline=deadline)
        if not result.stdout or not result.stdout.strip():
            raise CymbalProtocolError(f"Cymbal {subcommand} returned empty JSON output")
        try:
            decoded = result.stdout.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CymbalProtocolError(
                f"Malformed JSON from cymbal {subcommand}: {exc}",
                details=result.stdout[:300].decode("utf-8", errors="replace"),
            ) from exc

        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            raise CymbalExecutionError(
                f"Cymbal {subcommand} reported error: {err}",
                details=str(err),
            )
        if not isinstance(payload, (dict, list)):
            raise CymbalProtocolError(
                f"Unexpected JSON root type {type(payload).__name__} from cymbal {subcommand}"
            )
        return payload

    def _parse_definitions(
        self,
        root: Path,
        raw_items: Any,
        default_symbol: str,
        diagnostics: list[str],
    ) -> tuple[CymbalDefinition, ...]:
        if not isinstance(raw_items, list):
            return ()
        definitions: list[CymbalDefinition] = []
        seen: set[tuple[str, int, str]] = set()
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            raw_path = _extract_str(entry, ("rel_path", "file", "path", "filename"))
            norm_path = normalize_repo_path(root, raw_path)
            if norm_path is None:
                if raw_path:
                    diagnostics.append(f"rejected unsafe definition path: {raw_path}")
                continue
            line = _coerce_positive_int(
                entry.get("line") or entry.get("start_line") or entry.get("lineno"),
                default=1,
            )
            sym = _extract_str(entry, ("symbol", "name"), default=default_symbol)
            kind = _extract_str(entry, ("kind", "type"), default="definition").lower()
            key = (norm_path, line, sym)
            if key not in seen:
                seen.add(key)
                definitions.append(
                    CymbalDefinition(path=norm_path, line=line, symbol=sym, kind=kind)
                )
        return tuple(definitions)

    def _parse_references(
        self,
        root: Path,
        raw_items: Any,
        *,
        default_symbol: str,
        default_kind: str,
        transitive: bool,
        diagnostics: list[str],
    ) -> tuple[CymbalReference, ...]:
        if not isinstance(raw_items, list):
            return ()
        refs: list[CymbalReference] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            hit_symbols = entry.get("hit_symbols")
            row_obj = entry.get("row")
            item = row_obj if isinstance(row_obj, dict) else entry
            raw_path = _extract_str(item, ("rel_path", "file", "path", "filename"))
            norm_path = normalize_repo_path(root, raw_path)
            if norm_path is None:
                if raw_path:
                    diagnostics.append(f"rejected unsafe reference path: {raw_path}")
                continue
            line = _coerce_positive_int(
                item.get("line")
                or item.get("start_line")
                or item.get("lineno")
                or item.get("line_number"),
                default=0,
            )
            if line < 1:
                diagnostics.append(f"skipped reference with invalid line in {norm_path}")
                continue
            col = _coerce_positive_int(
                item.get("column") or item.get("col") or item.get("col_offset"),
                default=0,
            )
            kind = _extract_str(item, ("kind", "type", "role"), default=default_kind).lower()
            fallback_sym = default_symbol
            if isinstance(hit_symbols, list):
                for hs in hit_symbols:
                    if isinstance(hs, str) and hs.strip():
                        fallback_sym = hs.strip()
                        break
            sym = _extract_str(item, ("symbol", "callee", "target"), default=fallback_sym)
            caller_sym = _extract_str(
                item,
                ("caller", "caller_symbol", "enclosing_symbol", "function", "name"),
                default="",
            )
            if caller_sym == sym and "caller" not in item and "caller_symbol" not in item:
                caller_sym = ""
            snippet = _extract_str(item, ("snippet", "line_text", "text", "code", "content"))
            if not snippet:
                raw_context = item.get("context")
                if isinstance(raw_context, str) and raw_context.strip():
                    snippet = raw_context
                elif isinstance(raw_context, list):
                    ctx_lines = [
                        line_text.rstrip("\r\n")
                        for line_text in raw_context
                        if isinstance(line_text, str)
                    ]
                    if ctx_lines:
                        snippet = "\n".join(ctx_lines)
            is_alias = (
                _extract_bool(item, ("alias", "is_alias", "aliased"))
                or kind in _ALIAS_KINDS
            )
            depth = _coerce_positive_int(
                item.get("depth") if "depth" in item else entry.get("depth"),
                default=0,
            )
            is_transitive = (
                transitive
                or _extract_bool(item, ("transitive", "indirect"))
                or depth > 1
            )
            refs.append(
                CymbalReference(
                    path=norm_path,
                    line=line,
                    symbol=sym,
                    kind=kind,
                    column=col,
                    snippet=snippet,
                    caller_symbol=caller_sym,
                    is_alias=is_alias,
                    transitive=is_transitive,
                )
            )
        return tuple(refs)

    def refs(
        self,
        root: Path | str,
        symbol: str,
        *,
        deadline: float | None = None,
    ) -> CymbalRefsResult:
        """Run `cymbal refs --json --no-federate <symbol>` and validate the response."""
        clean_symbol = _validate_cli_arg(symbol, label="symbol")
        resolved_root = Path(root).resolve()
        payload = self._run_json(resolved_root, "refs", (clean_symbol,), deadline=deadline)
        diagnostics: list[str] = []

        if isinstance(payload, list):
            references = self._parse_references(
                resolved_root,
                payload,
                default_symbol=clean_symbol,
                default_kind="reference",
                transitive=False,
                diagnostics=diagnostics,
            )
            return CymbalRefsResult(
                symbol=clean_symbol,
                references=references,
                diagnostics=_unique_ordered(diagnostics),
            )

        if payload and not (set(payload.keys()) & _REFS_SCHEMA_KEYS):
            raise CymbalProtocolError(
                f"Unrecognized cymbal refs JSON keys: {sorted(payload.keys())}"
            )

        raw_refs = None
        for key in ("references", "refs", "matches", "results", "items", "callers"):
            if key in payload:
                raw_refs = payload[key]
                break
        if raw_refs is not None and not isinstance(raw_refs, list):
            raise CymbalProtocolError("cymbal refs references field must be a JSON array")

        raw_defs = None
        for key in ("definitions", "defs", "candidates"):
            if key in payload:
                raw_defs = payload[key]
                break

        definitions = self._parse_definitions(resolved_root, raw_defs, clean_symbol, diagnostics)
        references = self._parse_references(
            resolved_root,
            raw_refs or [],
            default_symbol=clean_symbol,
            default_kind="reference",
            transitive=False,
            diagnostics=diagnostics,
        )
        ambiguous = _extract_bool(
            payload, ("ambiguous", "is_ambiguous", "multiple_definitions")
        ) or len(definitions) > 1
        stale = _extract_bool(payload, ("stale", "is_stale", "index_stale", "outdated"))
        truncated = _extract_bool(payload, ("truncated", "is_truncated", "has_more", "capped"))

        return CymbalRefsResult(
            symbol=clean_symbol,
            references=references,
            definitions=definitions,
            ambiguous=ambiguous,
            stale=stale,
            truncated=truncated,
            diagnostics=_unique_ordered(diagnostics),
        )

    def impact(
        self,
        root: Path | str,
        symbol: str,
        *,
        deadline: float | None = None,
    ) -> CymbalImpactResult:
        """Run `cymbal impact --json --no-federate <symbol>` and validate the response."""
        clean_symbol = _validate_cli_arg(symbol, label="symbol")
        resolved_root = Path(root).resolve()
        payload = self._run_json(resolved_root, "impact", (clean_symbol,), deadline=deadline)
        diagnostics: list[str] = []

        if isinstance(payload, list):
            direct = self._parse_references(
                resolved_root,
                payload,
                default_symbol=clean_symbol,
                default_kind="impact",
                transitive=False,
                diagnostics=diagnostics,
            )
            return CymbalImpactResult(
                symbol=clean_symbol,
                direct_callers=direct,
                diagnostics=_unique_ordered(diagnostics),
            )

        if payload and not (set(payload.keys()) & _IMPACT_SCHEMA_KEYS):
            raise CymbalProtocolError(
                f"Unrecognized cymbal impact JSON keys: {sorted(payload.keys())}"
            )

        body = payload
        if isinstance(payload.get("results"), dict):
            body = payload["results"]
            if body and not (set(body.keys()) & _IMPACT_SCHEMA_KEYS):
                raise CymbalProtocolError(
                    f"Unrecognized cymbal impact results JSON keys: {sorted(body.keys())}"
                )

        raw_direct = None
        for key in (
            "direct_callers",
            "direct",
            "callers",
            "impacted",
            "affected",
            "references",
            "results",
        ):
            if key in body:
                raw_direct = body[key]
                break
        if raw_direct is not None and not isinstance(raw_direct, list):
            raise CymbalProtocolError("cymbal impact callers field must be a JSON array")

        raw_trans = None
        for key in ("transitive_callers", "transitive", "indirect_callers"):
            if key in body:
                raw_trans = body[key]
                break
        if raw_trans is not None and not isinstance(raw_trans, list):
            raise CymbalProtocolError("cymbal impact transitive_callers field must be a JSON array")

        raw_defs = (
            body.get("definitions")
            or body.get("candidates")
            or payload.get("definitions")
            or payload.get("candidates")
        )
        definitions = self._parse_definitions(resolved_root, raw_defs, clean_symbol, diagnostics)
        parsed_direct = self._parse_references(
            resolved_root,
            raw_direct or [],
            default_symbol=clean_symbol,
            default_kind="impact",
            transitive=False,
            diagnostics=diagnostics,
        )
        parsed_trans = self._parse_references(
            resolved_root,
            raw_trans or [],
            default_symbol=clean_symbol,
            default_kind="impact",
            transitive=True,
            diagnostics=diagnostics,
        )
        direct_callers = tuple(ref for ref in parsed_direct if not ref.transitive)
        transitive_callers = tuple(
            [*(ref for ref in parsed_direct if ref.transitive), *parsed_trans]
        )
        raw_def_count = (
            body.get("definition_count")
            if "definition_count" in body
            else payload.get("definition_count")
        )
        definition_count = _coerce_positive_int(raw_def_count, default=0)
        ambiguous = (
            _extract_bool(payload, ("ambiguous", "is_ambiguous", "multiple_definitions"))
            or _extract_bool(body, ("ambiguous", "is_ambiguous", "multiple_definitions"))
            or len(definitions) > 1
            or definition_count > 1
        )
        stale = _extract_bool(
            payload, ("stale", "is_stale", "index_stale", "outdated")
        ) or _extract_bool(body, ("stale", "is_stale", "index_stale", "outdated"))
        truncated = _extract_bool(
            payload, ("truncated", "is_truncated", "has_more", "capped")
        ) or _extract_bool(body, ("truncated", "is_truncated", "has_more", "capped"))

        return CymbalImpactResult(
            symbol=clean_symbol,
            direct_callers=direct_callers,
            transitive_callers=transitive_callers,
            definitions=definitions,
            ambiguous=ambiguous,
            stale=stale,
            truncated=truncated,
            diagnostics=_unique_ordered(diagnostics),
        )

    def changed(
        self,
        root: Path | str,
        *,
        base: str | None = None,
        deadline: float | None = None,
    ) -> CymbalChangedResult:
        """Run `cymbal changed --json --no-federate` and validate the response."""
        extra_args: list[str] = []
        if base is not None:
            clean_base = _validate_cli_arg(base, label="base")
            extra_args.extend(["--base", clean_base])
        resolved_root = Path(root).resolve()
        payload = self._run_json(resolved_root, "changed", extra_args, deadline=deadline)
        diagnostics: list[str] = []

        if isinstance(payload, list):
            raw_entries = payload
            raw_files: list[Any] = []
            stale = False
            truncated = False
        else:
            if payload and not (set(payload.keys()) & _CHANGED_SCHEMA_KEYS):
                raise CymbalProtocolError(
                    f"Unrecognized cymbal changed JSON keys: {sorted(payload.keys())}"
                )
            body = payload
            if isinstance(payload.get("results"), dict):
                body = payload["results"]
                if body and not (set(body.keys()) & _CHANGED_SCHEMA_KEYS):
                    raise CymbalProtocolError(
                        f"Unrecognized cymbal changed results JSON keys: {sorted(body.keys())}"
                    )
            raw_entries = []
            for key in ("changed", "changes", "symbols", "entries", "items", "results"):
                if key in body:
                    val = body[key]
                    if val is None:
                        raw_entries = []
                    elif not isinstance(val, list):
                        raise CymbalProtocolError(
                            f"cymbal changed {key} field must be a JSON array"
                        )
                    else:
                        raw_entries = val
                    break
            raw_files = []
            for key in ("files", "changed_files"):
                if key in body:
                    val = body[key]
                    if val is None:
                        raw_files = []
                    elif not isinstance(val, list):
                        raise CymbalProtocolError(
                            f"cymbal changed {key} field must be a JSON array"
                        )
                    else:
                        raw_files = val
                    break
            stale = _extract_bool(
                payload, ("stale", "is_stale", "index_stale", "outdated")
            ) or _extract_bool(body, ("stale", "is_stale", "index_stale", "outdated"))
            truncated = _extract_bool(
                payload, ("truncated", "is_truncated", "has_more", "capped")
            ) or _extract_bool(body, ("truncated", "is_truncated", "has_more", "capped"))

        entries: list[CymbalChangedEntry] = []
        files_set: set[str] = set()
        for item in raw_entries:
            if isinstance(item, str):
                norm = normalize_repo_path(resolved_root, item)
                if norm is not None:
                    files_set.add(norm)
                    entries.append(CymbalChangedEntry(path=norm))
                else:
                    diagnostics.append(f"rejected unsafe changed path: {item}")
                continue
            if not isinstance(item, dict):
                continue
            if _extract_bool(item, ("stale", "is_stale", "unindexed")):
                stale = True
            item_symbol = _extract_str(item, ("symbol", "name"))
            item_line = _coerce_positive_int(
                item.get("line") or item.get("start_line") or item.get("lineno"),
                default=0,
            )
            item_kind = _extract_str(item, ("kind", "type"), default="changed").lower()
            item_status = _extract_str(
                item, ("status", "change_type"), default="modified"
            ).lower()

            candidate_paths: list[str] = []
            raw_path = _extract_str(item, ("rel_path", "file", "path", "filename"))
            if raw_path:
                candidate_paths.append(raw_path)
            item_files = item.get("files")
            if isinstance(item_files, list):
                for raw_file_item in item_files:
                    if isinstance(raw_file_item, str) and raw_file_item.strip():
                        candidate_paths.append(raw_file_item)

            seen_item_paths: set[str] = set()
            for candidate_path in candidate_paths:
                norm = normalize_repo_path(resolved_root, candidate_path)
                if norm is None:
                    diagnostics.append(f"rejected unsafe changed path: {candidate_path}")
                    continue
                files_set.add(norm)
                if norm in seen_item_paths:
                    continue
                seen_item_paths.add(norm)
                entries.append(
                    CymbalChangedEntry(
                        path=norm,
                        symbol=item_symbol,
                        line=item_line,
                        kind=item_kind,
                        status=item_status,
                    )
                )

        for raw_file in raw_files:
            if not isinstance(raw_file, str):
                continue
            norm = normalize_repo_path(resolved_root, raw_file)
            if norm is not None:
                files_set.add(norm)
            else:
                diagnostics.append(f"rejected unsafe changed file path: {raw_file}")

        return CymbalChangedResult(
            entries=tuple(entries),
            changed_files=tuple(sorted(files_set)),
            stale=stale,
            truncated=truncated,
            diagnostics=_unique_ordered(diagnostics),
        )

    def show(
        self,
        root: Path | str,
        target: str,
        *,
        max_lines: int = DEFAULT_SHOW_MAX_LINES,
        max_bytes: int = DEFAULT_SHOW_MAX_BYTES,
        verify_disk: bool = True,
        deadline: float | None = None,
    ) -> CymbalShowResult:
        """Run `cymbal show --json --no-federate <target>` with path/UTF-8/size/freshness guards."""
        clean_target = _validate_cli_arg(target, label="target")
        if max_lines <= 0 or max_bytes <= 0:
            raise ValueError("max_lines and max_bytes must be positive")
        resolved_root = Path(root).resolve()
        payload = self._run_json(resolved_root, "show", (clean_target,), deadline=deadline)
        diagnostics: list[str] = []

        stale = False
        ambiguous = False
        truncated = False
        raw_items: list[dict[str, Any]] = []

        if isinstance(payload, list):
            raw_items = [item for item in payload if isinstance(item, dict)]
        else:
            if payload and not (set(payload.keys()) & _SHOW_SCHEMA_KEYS):
                raise CymbalProtocolError(
                    f"Unrecognized cymbal show JSON keys: {sorted(payload.keys())}"
                )
            stale = _extract_bool(payload, ("stale", "is_stale", "index_stale", "outdated"))
            ambiguous = _extract_bool(
                payload, ("ambiguous", "is_ambiguous", "multiple_definitions")
            )
            truncated = _extract_bool(
                payload, ("truncated", "is_truncated", "has_more", "capped")
            )
            body = payload
            if isinstance(payload.get("results"), dict):
                body = payload["results"]
                if body and not (set(body.keys()) & _SHOW_SCHEMA_KEYS):
                    raise CymbalProtocolError(
                        f"Unrecognized cymbal show results JSON keys: {sorted(body.keys())}"
                    )
                stale = stale or _extract_bool(
                    body, ("stale", "is_stale", "index_stale", "outdated")
                )
                ambiguous = ambiguous or _extract_bool(
                    body, ("ambiguous", "is_ambiguous", "multiple_definitions")
                )
                truncated = truncated or _extract_bool(
                    body, ("truncated", "is_truncated", "has_more", "capped")
                )

            container = None
            for key in ("slices", "matches", "definitions", "results", "items"):
                if key in body:
                    container = body[key]
                    break
            if container is not None:
                if not isinstance(container, list):
                    raise CymbalProtocolError("cymbal show container field must be a JSON array")
                raw_items = [item for item in container if isinstance(item, dict)]
            elif any(
                k in body
                for k in (
                    "file",
                    "path",
                    "filename",
                    "rel_path",
                    "code",
                    "content",
                    "body",
                    "lines",
                    "symbol",
                )
            ):
                raw_items = [body]

        slices: list[CymbalShowSlice] = []
        for entry in raw_items:
            sym_obj = entry.get("symbol") if isinstance(entry.get("symbol"), dict) else None
            entry_rel = _extract_str(entry, ("rel_path",))
            sym_rel = _extract_str(sym_obj, ("rel_path",)) if sym_obj is not None else ""
            entry_file = _extract_str(entry, ("file", "path", "filename"))
            sym_file = (
                _extract_str(sym_obj, ("file", "path", "filename"))
                if sym_obj is not None
                else ""
            )

            raw_path = entry_rel
            if not raw_path:
                if entry_file:
                    if sym_rel and (
                        entry_file == sym_file
                        or normalize_repo_path(resolved_root, entry_file) is None
                    ):
                        raw_path = sym_rel
                    else:
                        raw_path = entry_file
                elif sym_rel:
                    raw_path = sym_rel
                else:
                    raw_path = sym_file

            norm_path = normalize_repo_path(resolved_root, raw_path)
            if norm_path is None:
                if raw_path:
                    diagnostics.append(f"rejected unsafe show path: {raw_path}")
                continue

            raw_lines = entry.get("lines")
            derived_start = 0
            derived_end = 0
            parsed_line_texts: list[str] | None = None
            if isinstance(raw_lines, list):
                line_texts: list[str] = []
                line_nums: list[int] = []
                for line_item in raw_lines:
                    if isinstance(line_item, dict):
                        ln = _coerce_positive_int(
                            line_item.get("line") or line_item.get("lineno"),
                            default=0,
                        )
                        if ln >= 1:
                            line_nums.append(ln)
                        content = (
                            line_item.get("content")
                            if "content" in line_item
                            else line_item.get("text")
                        )
                        line_texts.append(
                            content.rstrip("\r\n") if isinstance(content, str) else ""
                        )
                    elif isinstance(line_item, str):
                        line_texts.append(line_item.rstrip("\r\n"))
                    else:
                        line_texts.append(str(line_item))
                if line_nums:
                    derived_start = line_nums[0]
                    derived_end = line_nums[-1]
                if line_texts:
                    parsed_line_texts = line_texts

            sym_start = (
                _coerce_positive_int(
                    sym_obj.get("start_line") or sym_obj.get("line"), default=0
                )
                if sym_obj is not None
                else 0
            )
            start_line = _coerce_positive_int(
                entry.get("start_line")
                or entry.get("line")
                or entry.get("lineno")
                or derived_start
                or sym_start,
                default=1,
            )
            if start_line < 1:
                start_line = 1

            raw_code = _extract_str(entry, ("code", "content", "body", "text", "snippet"))
            if raw_code:
                code_lines = raw_code.splitlines()
            elif parsed_line_texts is not None:
                code_lines = list(parsed_line_texts)
                raw_code = "\n".join(code_lines)
            else:
                code_lines = []
            if "\x00" in raw_code:
                diagnostics.append(f"rejected binary show content for {norm_path}")
                continue

            sym_end = (
                _coerce_positive_int(sym_obj.get("end_line"), default=0)
                if sym_obj is not None
                else 0
            )
            end_line = _coerce_positive_int(
                entry.get("end_line")
                or entry.get("endLine")
                or derived_end
                or (sym_end if not code_lines else 0),
                default=start_line + max(0, len(code_lines) - 1),
            )
            if end_line < start_line:
                end_line = start_line

            slice_stale = stale or _extract_bool(entry, ("stale", "is_stale"))
            slice_truncated = truncated or _extract_bool(entry, ("truncated", "is_truncated"))
            slice_ambiguity: list[str] = []

            if verify_disk:
                disk_end = min(end_line, start_line + max_lines - 1)
                disk = read_bounded_lines(
                    resolved_root,
                    norm_path,
                    start_line,
                    disk_end,
                    max_scan_bytes=self.config.max_scan_bytes,
                )
                if disk is None:
                    diagnostics.append(f"rejected unreadable/binary file on disk: {norm_path}")
                    continue
                disk_lines, last_line, hit_cap = disk
                if hit_cap:
                    slice_truncated = True
                    diagnostics.append(
                        f"{_format_scan_limit_note(self.config.max_scan_bytes, role='file')}: "
                        f"{norm_path}:{start_line}"
                    )
                if last_line < start_line or not disk_lines:
                    if not hit_cap:
                        slice_stale = True
                        slice_ambiguity.append("stale index")
                else:
                    disk_text = "\n".join(disk_lines)
                    if code_lines:
                        compare_lines = code_lines[: len(disk_lines)]
                        if "\n".join(compare_lines) != disk_text or (
                            not hit_cap
                            and last_line < disk_end
                            and len(code_lines) > len(disk_lines)
                        ):
                            slice_stale = True
                            slice_ambiguity.append("stale index")
                    code_lines = disk_lines
                    if end_line > disk_end:
                        slice_truncated = True
                    end_line = start_line + len(code_lines) - 1

            if len(code_lines) > max_lines:
                code_lines = code_lines[:max_lines]
                end_line = start_line + len(code_lines) - 1
                slice_truncated = True

            bounded_code, clipped_bytes = _truncate_utf8_bytes("\n".join(code_lines), max_bytes)
            if clipped_bytes:
                slice_truncated = True
                clipped_line_count = len(bounded_code.splitlines()) or 1
                end_line = start_line + clipped_line_count - 1

            if slice_stale:
                stale = True
                if "stale index" not in slice_ambiguity:
                    slice_ambiguity.append("stale index")
            if slice_truncated:
                truncated = True

            if sym_obj is not None:
                slice_symbol = _extract_str(sym_obj, ("name", "symbol"), default=clean_target)
                slice_kind = _extract_str(
                    entry,
                    ("kind", "type"),
                    default=_extract_str(sym_obj, ("kind", "type"), default="symbol"),
                ).lower()
            else:
                slice_symbol = _extract_str(entry, ("symbol", "name"), default=clean_target)
                slice_kind = _extract_str(entry, ("kind", "type"), default="symbol").lower()

            slices.append(
                CymbalShowSlice(
                    path=norm_path,
                    start_line=start_line,
                    end_line=end_line,
                    symbol=slice_symbol,
                    kind=slice_kind,
                    code=bounded_code,
                    stale=slice_stale,
                    truncated=slice_truncated,
                    ambiguity=_unique_ordered(slice_ambiguity),
                )
            )

        if len(slices) > 1:
            ambiguous = True
            slices = [
                replace(
                    s,
                    ambiguity=_unique_ordered((*s.ambiguity, "duplicate symbol definition")),
                )
                for s in slices
            ]

        return CymbalShowResult(
            target=clean_target,
            slices=tuple(slices),
            stale=stale,
            ambiguous=ambiguous,
            truncated=truncated,
            diagnostics=_unique_ordered(diagnostics),
        )


@dataclass(frozen=True)
class _TargetDefinition:
    path: str
    symbol: str
    kind: str
    line: int = 1
    end_line: int = 0


_NON_CODE_LANGS = frozenset(
    {
        "markdown",
        "json",
        "toml",
        "xml",
        "html",
        "css",
        "yaml",
        "csv",
        "rst",
        "ini",
        "org",
        "bibtex",
        "dockerfile",
        "make",
        "cmake",
        "sql",
        "regex",
        "diff",
    }
)


@dataclass(frozen=True)
class _EditableReadResult:
    text: str
    status: Literal["ok", "too_large", "unreadable"]


def _is_supported_editable_code_path(rel_path: str) -> bool:
    suffix = Path(rel_path).suffix.lower()
    if suffix == ".py" or suffix in _C_CPP_SUFFIXES:
        return True
    lang = resolve_lang(rel_path)
    return bool(lang and lang not in _NON_CODE_LANGS)


def _read_editable_text_checked(
    root: Path, rel_path: str, *, max_bytes: int
) -> _EditableReadResult:
    norm = normalize_repo_path(root, rel_path)
    if norm is None:
        return _EditableReadResult(text="", status="unreadable")
    full_path = root / norm
    try:
        with full_path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if b"\x00" in data:
            return _EditableReadResult(text="", status="unreadable")
        if len(data) > max_bytes:
            return _EditableReadResult(text="", status="too_large")
        return _EditableReadResult(text=data.decode("utf-8"), status="ok")
    except (OSError, UnicodeDecodeError):
        return _EditableReadResult(text="", status="unreadable")


def _read_editable_text(root: Path, rel_path: str, *, max_bytes: int) -> str:
    return _read_editable_text_checked(root, rel_path, max_bytes=max_bytes).text


def _extract_python_targets(
    path: str, source: str, words: set[str] | None
) -> list[_TargetDefinition]:
    """Extract candidate Python target definitions from ``source``.

    Preconditions:
        - ``path`` is a repository-relative POSIX path to a Python source file.
        - ``words`` is either an explicit filter set of identifier names or ``None``
          to extract all candidate definitions for downstream tiered selection.
    Postconditions:
        - Returns deduplicated ``_TargetDefinition`` entries with 1-based inclusive
          ``line`` and ``end_line`` spans, or ``[]`` on syntax errors.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    found: list[_TargetDefinition] = []
    seen: set[tuple[str, str, int]] = set()

    def _add(sym: str, kind: str, line: int, end_line: int) -> None:
        if (words is None or sym in words) and (path, sym, line) not in seen:
            seen.add((path, sym, line))
            found.append(
                _TargetDefinition(
                    path=path,
                    symbol=sym,
                    kind=kind,
                    line=line,
                    end_line=max(line, end_line),
                )
            )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            line = getattr(node, "lineno", 1)
            _add(node.name, "function", line, getattr(node, "end_lineno", line) or line)
        elif isinstance(node, ast.ClassDef):
            line = getattr(node, "lineno", 1)
            _add(node.name, "class", line, getattr(node, "end_lineno", line) or line)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    item_line = getattr(item, "lineno", 1)
                    _add(
                        item.name,
                        "method",
                        item_line,
                        getattr(item, "end_lineno", item_line) or item_line,
                    )
        elif isinstance(node, ast.Assign):
            if isinstance(node.value, (ast.Name, ast.Attribute)):
                line = getattr(node, "lineno", 1)
                end_line = getattr(node, "end_lineno", line) or line
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        _add(target.id, "alias", line, end_line)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and isinstance(
                node.value, (ast.Name, ast.Attribute)
            ):
                line = getattr(node, "lineno", 1)
                _add(
                    node.target.id,
                    "alias",
                    line,
                    getattr(node, "end_lineno", line) or line,
                )
        elif isinstance(node, ast.ImportFrom):
            line = getattr(node, "lineno", 1)
            end_line = getattr(node, "end_lineno", line) or line
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                if node.level > 0 or alias.asname:
                    _add(bound, "alias", line, end_line)
    return found


_C_CPP_SUFFIXES = frozenset(
    {".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".ipp", ".tpp"}
)
_CPP_ONLY_SUFFIXES = frozenset({".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".ipp", ".tpp"})
_C_NON_DECL_KEYWORDS = frozenset(
    {
        "return",
        "if",
        "else",
        "for",
        "while",
        "switch",
        "case",
        "do",
        "goto",
        "throw",
        "delete",
        "new",
        "sizeof",
        "typeof",
        "typedef",
        "using",
        "namespace",
        "co_return",
        "co_await",
        "co_yield",
    }
)
_C_PARAM_TYPE_KEYWORDS = frozenset(
    {
        "const",
        "unsigned",
        "signed",
        "struct",
        "enum",
        "union",
        "volatile",
        "restrict",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "void",
        "size_t",
        "ssize_t",
        "bool",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
        "int8_t",
        "int16_t",
        "int32_t",
        "int64_t",
        "ptrdiff_t",
        "intptr_t",
        "uintptr_t",
        "wchar_t",
        "char16_t",
        "char32_t",
        "FILE",
    }
)
_C_DECL_LINE_PREFIX_RE = re.compile(
    r"^(?:extern\s+\"C\"\s+)?"
    r"(?:template\s*<[^<>{};]*>\s*)?"
    r"(?:[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*(?:\s*<[^<>{};]*>)?(?:\s*[*&]+|\s+))+"
    r"(?:[A-Za-z_]\w*\s*::\s*)?$"
)
_C_RETURN_TYPE_LINE_RE = re.compile(
    r"^(?:extern\s+\"C\"\s+)?"
    r"(?:template\s*<[^<>{};]*>\s*)?"
    r"[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*(?:\s*<[^<>{};]*>)?"
    r"(?:\s+[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*(?:\s*<[^<>{};]*>)?)*"
    r"(?:\s*[*&]+)*$"
)
_C_VOID_PARAMS_RE = re.compile(r"^\s*void\s*\)")
_C_FIRST_PARAM_WORD_RE = re.compile(r"^\s*([A-Za-z_]\w*)(?:\s+|\s*[*&])")
_C_CPP_FUNC_DEF_RE = re.compile(
    r"(?m)^[ \t]*"
    r"(?P<prefix>(?:[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*(?:\s*<[^<>{};]*>)?(?:\s*[*&]+[ \t]*|[ \t]+))+)"
    r"(?:(?P<owner>[A-Za-z_]\w*)\s*::\s*)?"
    r"(?P<name>[A-Za-z_]\w*)[ \t]*\([^;{}]*\)[ \t\r\n]*"
    r"(?:(?:const|noexcept|override|final)\b[ \t\r\n]*)*"
    r"(?P<term>\{|;)"
)


def _is_c_cpp_declaration_line(
    path: str,
    line_text: str,
    symbol: str,
    *,
    prev_line: str = "",
) -> bool:
    """Return True when ``line_text`` is a C/C++ prototype or definition header for ``symbol``.

    Preconditions:
        - ``path`` is a repository-relative path and ``line_text`` is a single source line.
    Postconditions:
        - Returns ``False`` for non-C/C++ paths and for genuine call expressions.
        - Returns ``True`` for prototypes, ``extern`` declarations, and definition headers.
    """
    if Path(path).suffix.lower() not in _C_CPP_SUFFIXES:
        return False
    match = re.search(rf"\b{re.escape(symbol)}\s*\(", line_text)
    if not match:
        return False
    before = line_text[: match.start()]
    same_line_stmt_reset = "{" in before or ";" in before
    stmt_before = before.rsplit("{", 1)[-1].rsplit(";", 1)[-1].lstrip()
    stripped_before = stmt_before.strip()
    if stripped_before:
        if any(ch in stripped_before for ch in ("=", ",", "?", "(", ")", "[", "]", ".", "->")):
            return False
        if _C_DECL_LINE_PREFIX_RE.match(stmt_before):
            first_word_match = WORD_RE.search(stripped_before)
            first_word = first_word_match.group(0) if first_word_match else ""
            if first_word and first_word not in _C_NON_DECL_KEYWORDS:
                return True
        return False
    after_paren = line_text[match.end() :]
    if _C_VOID_PARAMS_RE.match(after_paren):
        return True
    first_param = _C_FIRST_PARAM_WORD_RE.match(after_paren)
    if first_param and first_param.group(1) in _C_PARAM_TYPE_KEYWORDS:
        return True
    if not same_line_stmt_reset and prev_line:
        prev_stripped = prev_line.strip()
        if prev_stripped and _C_RETURN_TYPE_LINE_RE.match(prev_stripped):
            prev_word_match = WORD_RE.search(prev_stripped)
            prev_word = prev_word_match.group(0) if prev_word_match else ""
            if prev_word and prev_word not in _C_NON_DECL_KEYWORDS:
                return True
    return False


def _is_python_definition_line(path: str, line_text: str, symbol: str) -> bool:
    """Return True when ``line_text`` is a Python ``def``/``class`` header for ``symbol``."""
    if Path(path).suffix.lower() != ".py":
        return False
    return bool(
        re.match(
            rf"^\s*(?:(?:async\s+)?def|class)\s+{re.escape(symbol)}\b",
            line_text,
        )
    )


def _unwrap_c_declarator_name(node: Any, *, in_class: bool = False) -> tuple[str | None, str]:
    """Extract ``(symbol_name, kind)`` from a tree-sitter C/C++ declarator node."""
    cur = node
    # Locate the innermost function_declarator (e.g., through pointer_declarator).
    while cur is not None and getattr(cur, "type", "") != "function_declarator":
        child = cur.child_by_field_name("declarator")
        if child is None:
            return None, "function"
        cur = child
    if cur is None:
        return None, "function"

    inner = cur.child_by_field_name("declarator")
    if inner is None:
        return None, "function"

    inner_type = getattr(inner, "type", "")
    # Skip function-pointer declarators such as `int (*cb)(int)`.
    if inner_type == "parenthesized_declarator":
        return None, "function"
    if inner_type == "identifier":
        name = inner.text.decode("utf-8", errors="ignore").strip()
        return (name or None), ("method" if in_class else "function")
    if inner_type == "field_identifier":
        name = inner.text.decode("utf-8", errors="ignore").strip()
        return (name or None), "method"
    if inner_type == "qualified_identifier":
        leaf = inner
        while leaf is not None and getattr(leaf, "type", "") == "qualified_identifier":
            leaf = leaf.child_by_field_name("name")
        if leaf is not None:
            name = leaf.text.decode("utf-8", errors="ignore").strip()
            return (name or None), "method"
    return None, "function"


def _find_matching_brace(source: str, open_idx: int) -> int:
    """Return index of the closing ``}`` matching ``source[open_idx] == '{'`` (or len(source))."""
    depth = 0
    i = open_idx
    n = len(source)
    in_str = ""
    while i < n:
        ch = source[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_str:
                in_str = ""
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = source[i + 1]
            if nxt == "/":
                nl = source.find("\n", i + 2)
                i = n if nl == -1 else nl + 1
                continue
            if nxt == "*":
                end = source.find("*/", i + 2)
                i = n if end == -1 else end + 2
                continue
        if ch in ('"', "'"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _extract_c_cpp_ts_targets(
    path: str,
    source: str,
    words: set[str] | None,
) -> list[_TargetDefinition]:
    suffix = Path(path).suffix.lower()
    lang = "cpp" if suffix in _CPP_ONLY_SUFFIXES else "c"
    is_header = is_c_cpp_header(path)
    try:
        warnings.simplefilter("ignore", category=FutureWarning)
        from grep_ast.tsl import get_parser

        parser = get_parser(lang)
        tree = parser.parse(source.encode("utf-8"))
    except Exception:
        return []

    definitions: list[_TargetDefinition] = []
    declarations: list[_TargetDefinition] = []
    seen_defs: set[str] = set()
    seen_decls: set[str] = set()

    stack: list[tuple[Any, bool, bool, bool]] = [(tree.root_node, False, False, False)]
    while stack:
        node, in_class, in_param, in_compound = stack.pop()
        node_type = getattr(node, "type", "")
        if node_type == "parameter_declaration":
            continue
        if node_type == "function_definition":
            decl = node.child_by_field_name("declarator")
            name, kind = _unwrap_c_declarator_name(decl, in_class=in_class)
            if (
                name
                and (words is None or name in words)
                and name not in CONTROL_KEYWORDS
                and name not in seen_defs
            ):
                seen_defs.add(name)
                line = node.start_point[0] + 1
                end_line = max(line, node.end_point[0] + 1)
                definitions.append(
                    _TargetDefinition(
                        path=path, symbol=name, kind=kind, line=line, end_line=end_line
                    )
                )
        elif (
            is_header
            and node_type == "function_declarator"
            and not in_param
            and not (lang == "cpp" and in_compound)
            and not has_function_definition_declarator_ancestor(node)
        ):
            name, kind = _unwrap_c_declarator_name(node, in_class=in_class)
            if (
                name
                and (words is None or name in words)
                and name not in CONTROL_KEYWORDS
                and name not in seen_decls
            ):
                seen_decls.add(name)
                line = node.start_point[0] + 1
                end_line = max(line, node.end_point[0] + 1)
                declarations.append(
                    _TargetDefinition(
                        path=path, symbol=name, kind=kind, line=line, end_line=end_line
                    )
                )

        next_in_class = in_class or node_type in ("class_specifier", "struct_specifier")
        next_in_param = in_param or node_type == "parameter_list"
        next_in_compound = in_compound or node_type == "compound_statement"
        for child in reversed(node.children):
            stack.append((child, next_in_class, next_in_param, next_in_compound))

    found = list(definitions)
    for item in declarations:
        if item.symbol not in seen_defs:
            found.append(item)
            seen_defs.add(item.symbol)
    return found


def _extract_c_cpp_targets(
    path: str, source: str, words: set[str] | None
) -> list[_TargetDefinition]:
    """Extract C/C++ target definitions (and header prototypes) from ``source``.

    Preconditions:
        - ``path`` has a C/C++ file suffix.
        - ``words`` is either an explicit symbol filter set or ``None`` for unfiltered
          candidate extraction.
    Postconditions:
        - Unbacked ``extern``/forward/method declarations in non-header ``.c``/``.cpp``
          files are strictly excluded regardless of ``words``.
        - In header files, definitions take precedence over prototypes and block-scoped
          ``extern`` declarations inside function bodies are excluded.
    """
    found = _extract_c_cpp_ts_targets(path, source, words)
    seen = {item.symbol for item in found}
    is_header = is_c_cpp_header(path)

    matches = list(_C_CPP_FUNC_DEF_RE.finditer(source))
    body_spans: list[tuple[int, int]] = []
    if is_header:
        for match in matches:
            if (match.group("term") or "") != "{":
                continue
            prefix = match.group("prefix") or ""
            if "=" in prefix:
                continue
            first_tok = prefix.split()[0] if prefix.split() else ""
            if not first_tok or first_tok in _C_NON_DECL_KEYWORDS:
                continue
            open_idx = match.end() - 1
            body_spans.append((open_idx, _find_matching_brace(source, open_idx)))

    proto_candidates: list[_TargetDefinition] = []
    for match in matches:
        prefix = match.group("prefix") or ""
        name = match.group("name") or ""
        owner = match.group("owner") or ""
        term = match.group("term") or ""
        if (
            not name
            or (words is not None and name not in words)
            or name in CONTROL_KEYWORDS
            or name in seen
        ):
            continue
        if "=" in prefix:
            continue
        first_tok = prefix.split()[0] if prefix.split() else ""
        if not first_tok or first_tok in _C_NON_DECL_KEYWORDS:
            continue
        line = source.count("\n", 0, match.start()) + 1
        kind = "method" if owner else "function"
        if term == "{":
            close_idx = _find_matching_brace(source, match.end() - 1)
            end_line = max(line, source.count("\n", 0, close_idx) + 1)
            target = _TargetDefinition(
                path=path, symbol=name, kind=kind, line=line, end_line=end_line
            )
            seen.add(name)
            found.append(target)
        elif is_header:
            if any(start < match.start() < end for start, end in body_spans):
                continue
            end_line = max(line, source.count("\n", 0, match.end()) + 1)
            target = _TargetDefinition(
                path=path, symbol=name, kind=kind, line=line, end_line=end_line
            )
            proto_candidates.append(target)

    for target in proto_candidates:
        if target.symbol not in seen:
            seen.add(target.symbol)
            found.append(target)
    return found


def _extract_generic_targets(
    path: str, source: str, words: set[str] | None
) -> list[_TargetDefinition]:
    if Path(path).suffix.lower() in _C_CPP_SUFFIXES:
        return _extract_c_cpp_targets(path, source, words)
    found: list[_TargetDefinition] = []
    seen: set[str] = set()
    for pattern in DEF_REGEXES:
        for match in pattern.finditer(source):
            name = match.group(1)
            if (
                name
                and (words is None or name in words)
                and name not in CONTROL_KEYWORDS
                and name not in seen
            ):
                seen.add(name)
                line = source.count("\n", 0, match.start()) + 1
                end_line = line
                line_end = source.find("\n", match.end())
                search_end = len(source) if line_end == -1 else line_end
                brace_idx = source.find("{", match.start(), search_end)
                if brace_idx != -1:
                    close_idx = _find_matching_brace(source, brace_idx)
                    end_line = max(line, source.count("\n", 0, close_idx) + 1)
                found.append(
                    _TargetDefinition(
                        path=path,
                        symbol=name,
                        kind="function",
                        line=line,
                        end_line=end_line,
                    )
                )
    return found


def _extract_editable_targets(
    resolved_root: Path,
    editable_paths: Sequence[str],
    words: set[str],
    *,
    max_scan_bytes: int,
    report: CallerEvidenceReport | None = None,
    editable_sources: dict[str, str] | None = None,
) -> tuple[dict[str, list[_TargetDefinition]], list[str]]:
    """Extract and select target definitions from editable files using shared tiered rules.

    Preconditions:
        - ``resolved_root`` is an existing resolved repository root path.
        - ``editable_paths`` are normalized repository-relative POSIX paths.
        - ``words`` contains identifier words extracted from the task message.
    Postconditions:
        - Returns ``(targets_by_symbol, impairment_truncations)`` filtered via
          ``aider.local_callers.select_target_definitions`` for parity with built-in
          finders under both exact and vague prompts.
        - Populates ``editable_sources`` (when provided) with read text of editable files.
    """
    all_by_symbol: dict[str, list[_TargetDefinition]] = {}
    impairment_truncations: list[str] = []
    for rel_path in editable_paths:
        if not _is_supported_editable_code_path(rel_path):
            continue
        read_res = _read_editable_text_checked(
            resolved_root, rel_path, max_bytes=max_scan_bytes
        )
        if read_res.status == "too_large":
            note = _format_scan_limit_note(max_scan_bytes, role="editable")
            impairment_truncations.append(note)
            _report_record_skipped_large(report, rel_path, reason=note)
            _report_add_diagnostic(
                report,
                f"editable file exceeded scan limit ({max_scan_bytes} bytes): {rel_path}",
            )
            continue
        if read_res.status == "unreadable":
            note = "unreadable or binary editable file omitted"
            impairment_truncations.append(note)
            _report_add_truncation(report, note)
            _report_add_diagnostic(
                report, f"skipped unreadable or binary editable file: {rel_path}"
            )
            continue
        source = read_res.text
        if not source:
            continue
        if editable_sources is not None:
            editable_sources[rel_path] = source
        suffix = Path(rel_path).suffix.lower()
        if suffix == ".py":
            defs = _extract_python_targets(rel_path, source, None)
        elif suffix in _C_CPP_SUFFIXES:
            defs = _extract_c_cpp_targets(rel_path, source, None)
        else:
            defs = _extract_generic_targets(rel_path, source, None)
        for item in defs:
            all_by_symbol.setdefault(item.symbol, []).append(item)

    selected_symbols, _tier = select_target_definitions(all_by_symbol.keys(), words)
    by_symbol = {
        sym: defs for sym, defs in all_by_symbol.items() if sym in selected_symbols
    }
    return by_symbol, impairment_truncations


def _is_symbol_not_found_error(exc: CymbalExecutionError) -> bool:
    """Return True when a Cymbal execution failure specifically reports symbol-not-found."""
    haystack = f"{exc}\n{exc.details}".lower()
    return "symbol not found" in haystack


def _reference_priority(ref: CymbalReference) -> int:
    if ref.is_alias or ref.kind in _IMPORT_KINDS:
        return 4
    if ref.transitive:
        return 3
    if ref.kind in _CALL_KINDS:
        return 0
    if ref.kind == "impact":
        return 1
    return 2


@dataclass(frozen=True)
class _CandidateHit:
    path: str
    display_path: str
    line: int
    column: int
    symbol: str
    kind: str
    source: str
    snippet: str
    priority: int
    ambiguity: tuple[str, ...]
    truncation: tuple[str, ...]
    scope: tuple[str, ...]


class CymbalCallerFinder:
    """`CallerFinder` adapter backed by pinned Cymbal v0.18.0 (`--no-federate`)."""

    def __init__(
        self,
        client: CymbalClient | None = None,
        *,
        config: CymbalConfig | None = None,
        runner: CymbalCommandRunner | None = None,
    ):
        self.client = client or CymbalClient(config=config, runner=runner)
        self.config = self.client.config

    def supports(self, path: str) -> bool:
        return bool(path and isinstance(path, str) and "\x00" not in path and path.strip())

    def find_callers(
        self,
        root: Path,
        message: str,
        editable: Sequence[str],
        candidates: Sequence[str],
        *,
        max_files: int = 3,
        max_chars: int = 700,
        context_lines: int = 3,
        report: CallerEvidenceReport | None = None,
    ) -> tuple[CallerExcerpt, ...]:
        if max_files <= 0 or max_chars <= 0:
            return ()

        resolved_root = Path(root).resolve()
        words = set(WORD_RE.findall(message or ""))
        if not words:
            return ()

        editable_norm_list: list[str] = []
        editable_norm_set: set[str] = set()
        for raw_ed in editable:
            norm = normalize_repo_path(resolved_root, raw_ed)
            if norm is not None and norm not in editable_norm_set:
                editable_norm_set.add(norm)
                editable_norm_list.append(norm)

        if not editable_norm_list:
            return ()

        candidate_display_by_norm: dict[str, str] = {}
        for raw_cand in sorted(set(candidates)):
            norm = normalize_repo_path(resolved_root, raw_cand)
            if norm is None:
                continue
            candidate_display_by_norm.setdefault(norm, norm)
        # Allow sibling same-file callers inside non-test editable files even when
        # callers omit editable files from `candidates`, matching builtin parity.
        for norm_ed in editable_norm_list:
            if not is_test_path(norm_ed):
                candidate_display_by_norm.setdefault(norm_ed, norm_ed)

        _report_add_scope(report, "no_federate")
        editable_sources: dict[str, str] = {}
        targets_by_symbol, editable_truncations = _extract_editable_targets(
            resolved_root,
            editable_norm_list,
            words,
            max_scan_bytes=self.config.max_scan_bytes,
            report=report,
            editable_sources=editable_sources,
        )
        editable_lines_by_path = {
            path: src.splitlines() for path, src in editable_sources.items()
        }
        for sym, defs in sorted(targets_by_symbol.items()):
            for target_def in defs:
                _report_add_definition(report, target_def.path, sym)

        if not targets_by_symbol:
            return ()
        if not candidate_display_by_norm:
            return ()

        deadline = time.monotonic() + float(self.config.total_deadline_seconds)
        changed_files: set[str] = set()
        global_stale = False
        global_truncations: list[str] = []

        if self.config.use_changed_check:
            changed_res = self.client.changed(resolved_root, deadline=deadline)
            changed_files.update(changed_res.changed_files)
            for diag in changed_res.diagnostics:
                _report_add_diagnostic(report, diag)
            if changed_res.stale:
                global_stale = True
                _report_add_ambiguity(report, "stale index")
            if changed_res.truncated:
                global_truncations.append("cymbal changed truncated")
                _report_add_truncation(report, "cymbal changed truncated")

        hits_by_path: dict[str, _CandidateHit] = {}
        out_of_scope_filtered = False

        for symbol in sorted(targets_by_symbol):
            target_defs = targets_by_symbol[symbol]
            symbol_ambiguities: list[str] = []
            symbol_truncations: list[str] = list(global_truncations)
            symbol_scopes: list[str] = ["no_federate"]

            if global_stale:
                symbol_ambiguities.append("stale index")
            if len({d.path for d in target_defs}) > 1 or len(target_defs) > 1:
                symbol_ambiguities.append("duplicate symbol definition")
            if any(d.kind == "alias" for d in target_defs):
                symbol_ambiguities.append("alias reference")
            if any(d.kind == "method" for d in target_defs):
                symbol_ambiguities.append("ambiguous method receiver")

            combined_refs: list[tuple[CymbalReference, str]] = []
            cymbal_def_locs: set[tuple[str, int]] = set()
            try:
                refs_res = self.client.refs(resolved_root, symbol, deadline=deadline)
            except CymbalExecutionError as exc:
                if not _is_symbol_not_found_error(exc):
                    raise
                _report_add_diagnostic(report, f"cymbal refs: symbol not found ({symbol})")
                refs_res = None

            if refs_res is not None:
                for diag in refs_res.diagnostics:
                    _report_add_diagnostic(report, diag)
                if refs_res.stale:
                    symbol_ambiguities.append("stale index")
                if refs_res.ambiguous or len({d.path for d in refs_res.definitions}) > 1:
                    symbol_ambiguities.append("duplicate symbol definition")
                if refs_res.truncated:
                    symbol_truncations.append("cymbal refs truncated")
                if refs_res.definitions and not any(
                    d.path in editable_norm_set for d in refs_res.definitions
                ):
                    symbol_ambiguities.append("ambiguous symbol binding")
                cymbal_def_locs.update((d.path, d.line) for d in refs_res.definitions)
                combined_refs.extend((ref, "cymbal:refs") for ref in refs_res.references)

            if self.config.use_impact:
                try:
                    impact_res = self.client.impact(resolved_root, symbol, deadline=deadline)
                except CymbalExecutionError as exc:
                    if not _is_symbol_not_found_error(exc):
                        raise
                    _report_add_diagnostic(report, f"cymbal impact: symbol not found ({symbol})")
                    impact_res = None

                if impact_res is not None:
                    for diag in impact_res.diagnostics:
                        _report_add_diagnostic(report, diag)
                    if impact_res.stale:
                        symbol_ambiguities.append("stale index")
                    if impact_res.ambiguous or len({d.path for d in impact_res.definitions}) > 1:
                        symbol_ambiguities.append("duplicate symbol definition")
                    if impact_res.truncated:
                        symbol_truncations.append("cymbal impact truncated")
                    if impact_res.definitions and not any(
                        d.path in editable_norm_set for d in impact_res.definitions
                    ):
                        symbol_ambiguities.append("ambiguous symbol binding")
                    cymbal_def_locs.update((d.path, d.line) for d in impact_res.definitions)
                    for ref in impact_res.direct_callers:
                        combined_refs.append((ref, "cymbal:impact"))
                    for ref in impact_res.transitive_callers:
                        combined_refs.append((ref, "cymbal:impact"))

            for amb in _unique_ordered(symbol_ambiguities):
                _report_add_ambiguity(report, amb)
            for trunc in _unique_ordered(symbol_truncations):
                _report_add_truncation(report, trunc)

            for ref, source_tag in combined_refs:
                if ref.kind in _DEFINITION_KINDS:
                    continue
                if ref.path not in candidate_display_by_norm:
                    out_of_scope_filtered = True
                    continue
                if (ref.path, ref.line) in cymbal_def_locs:
                    continue
                if ref.path in editable_norm_set:
                    if any(
                        d.path == ref.path
                        and d.line <= ref.line <= max(d.line, d.end_line)
                        for d in target_defs
                    ):
                        continue
                    ed_lines = editable_lines_by_path.get(ref.path)
                    if ed_lines is not None and 1 <= ref.line <= len(ed_lines):
                        call_line = ed_lines[ref.line - 1]
                        prev_line = ed_lines[ref.line - 2] if ref.line >= 2 else ""
                        if _is_c_cpp_declaration_line(
                            ref.path, call_line, symbol, prev_line=prev_line
                        ) or _is_python_definition_line(ref.path, call_line, symbol):
                            continue

                hit_ambiguities = list(symbol_ambiguities)
                hit_truncations = list(symbol_truncations)
                hit_scopes = list(symbol_scopes)

                if ref.is_alias or ref.kind in _ALIAS_KINDS:
                    hit_ambiguities.append("alias reference")
                elif ref.kind in _IMPORT_KINDS:
                    hit_ambiguities.append("alias reference")
                if ref.transitive:
                    hit_ambiguities.append("transitive caller")
                    hit_scopes.append("transitive_impact")
                if ref.path in changed_files or any(d.path in changed_files for d in target_defs):
                    hit_scopes.append("working_tree_changed")

                candidate_hit = _CandidateHit(
                    path=ref.path,
                    display_path=candidate_display_by_norm[ref.path],
                    line=ref.line,
                    column=ref.column,
                    symbol=symbol,
                    kind=ref.kind,
                    source=source_tag,
                    snippet=ref.snippet,
                    priority=_reference_priority(ref),
                    ambiguity=_unique_ordered(hit_ambiguities),
                    truncation=_unique_ordered(hit_truncations),
                    scope=_unique_ordered(hit_scopes),
                )

                existing = hits_by_path.get(ref.path)
                if existing is None or (
                    candidate_hit.priority,
                    candidate_hit.line,
                    candidate_hit.column,
                    candidate_hit.symbol,
                ) < (
                    existing.priority,
                    existing.line,
                    existing.column,
                    existing.symbol,
                ):
                    hits_by_path[ref.path] = candidate_hit

        if out_of_scope_filtered:
            _report_add_scope(report, "candidate_scope_filtered")

        if not hits_by_path:
            return ()

        # Production-first selection: prefer production callers over test/smoke callers,
        # ranking cross-file callers strictly ahead of same-file callers.
        prod_hits = [h for h in hits_by_path.values() if not is_test_path(h.path)]
        test_hits = [h for h in hits_by_path.values() if is_test_path(h.path)]
        if prod_hits:
            selected_pool = sorted(
                prod_hits,
                key=lambda h: (1 if h.path in editable_norm_set else 0, h.path, h.line),
            )
            pool_scope = "production_only"
            if test_hits:
                _report_add_scope(report, "test_callers_deprioritized")
        else:
            selected_pool = sorted(
                test_hits,
                key=lambda h: (1 if h.path in editable_norm_set else 0, h.path, h.line),
            )
            pool_scope = "test_fallback"
        _report_add_scope(report, pool_scope)

        excerpts: list[CallerExcerpt] = []
        evidence_items: list[CallerEvidenceItem] = []
        skipped_stale_line = False
        skipped_truncations: list[str] = list(editable_truncations)
        scan_limit_note = _format_scan_limit_note(self.config.max_scan_bytes, role="candidate")

        for hit in selected_pool:
            window = read_bounded_window(
                resolved_root,
                hit.path,
                hit.line,
                context_lines=context_lines,
                max_chars=max_chars,
                max_scan_bytes=self.config.max_scan_bytes,
            )
            if window is None:
                unreadable_note = "unreadable or binary candidate file omitted"
                skipped_truncations.append(unreadable_note)
                _report_add_truncation(report, unreadable_note)
                _report_add_diagnostic(report, f"skipped unreadable or binary caller: {hit.path}")
                continue
            hit_ambiguities = list(hit.ambiguity)
            hit_truncations = list(hit.truncation)
            hit_scopes = [*hit.scope, pool_scope]

            if not window.line_exists:
                if window.hit_scan_cap:
                    skipped_truncations.append(scan_limit_note)
                    _report_record_skipped_large(report, hit.path, reason=scan_limit_note)
                    _report_add_diagnostic(
                        report,
                        f"candidate file exceeded scan limit ({self.config.max_scan_bytes} bytes): "
                        f"{hit.path}:{hit.line}",
                    )
                else:
                    skipped_stale_line = True
                    _report_add_ambiguity(report, "stale index")
                    _report_add_diagnostic(
                        report, f"cymbal reported out-of-range line {hit.path}:{hit.line}"
                    )
                continue

            start_line = window.start_line
            snippet = window.text
            center_text = window.call_line_text
            truncated_chars = window.truncated
            if window.hit_scan_cap:
                hit_truncations.append(scan_limit_note)
                _report_add_truncation(report, scan_limit_note)

            if hit.snippet and hit.snippet.strip():
                expected_text = hit.snippet.strip()
                if expected_text not in snippet and expected_text != center_text.strip():
                    hit_ambiguities.append("stale index")
            elif (
                hit.path in changed_files
                and hit.symbol not in snippet
                and "alias reference" not in hit_ambiguities
            ):
                hit_ambiguities.append("stale index")

            if self.config.use_show:
                show_res = self.client.show(
                    resolved_root,
                    f"{hit.path}:{hit.line}",
                    max_lines=max(1, context_lines * 2 + 1),
                    max_bytes=max(256, max_chars * 4),
                    verify_disk=True,
                    deadline=deadline,
                )
                if show_res.stale:
                    hit_ambiguities.append("stale index")
                if show_res.ambiguous:
                    hit_ambiguities.append("duplicate symbol definition")
                if show_res.truncated:
                    hit_truncations.append("cymbal show truncated")
                for diag in show_res.diagnostics:
                    _report_add_diagnostic(report, diag)

            if truncated_chars:
                hit_truncations.append("excerpt truncated")

            unique_amb = _unique_ordered(hit_ambiguities)
            unique_trunc = _unique_ordered(hit_truncations)
            unique_scope = _unique_ordered(hit_scopes)

            excerpts.append(
                CallerExcerpt(
                    path=hit.display_path,
                    start_line=start_line,
                    symbol=hit.symbol,
                    code=snippet,
                    limitations=(*unique_amb, *unique_trunc),
                    line=hit.line,
                    kind=hit.kind,
                    source=hit.source,
                )
            )
            evidence_items.append(
                CallerEvidenceItem(
                    path=hit.display_path,
                    line=hit.line,
                    symbol=hit.symbol,
                    kind=hit.kind,
                    source=hit.source,
                    scope=unique_scope,
                    ambiguity=unique_amb,
                    truncation=unique_trunc,
                )
            )

        if not excerpts:
            return ()

        omitted_extra = len(excerpts) > max_files
        selected_excerpts = excerpts[:max_files]
        selected_items = evidence_items[:max_files]

        if omitted_extra:
            selected_excerpts[-1] = replace(
                selected_excerpts[-1],
                limitations=_unique_ordered(
                    (*selected_excerpts[-1].limitations, "additional files omitted")
                ),
            )
            selected_items[-1] = replace(
                selected_items[-1],
                truncation=_unique_ordered(
                    (*selected_items[-1].truncation, "additional files omitted")
                ),
            )
            _report_add_truncation(report, "additional files omitted")

        matched_symbols = {e.symbol for e in selected_excerpts}
        missing_symbols = set(targets_by_symbol.keys()) - matched_symbols
        if missing_symbols:
            selected_excerpts[-1] = replace(
                selected_excerpts[-1],
                limitations=_unique_ordered(
                    (*selected_excerpts[-1].limitations, "unmatched target symbols")
                ),
            )
            selected_items[-1] = replace(
                selected_items[-1],
                truncation=_unique_ordered(
                    (*selected_items[-1].truncation, "unmatched target symbols")
                ),
            )
            _report_add_truncation(report, "unmatched target symbols")

        if skipped_stale_line:
            selected_excerpts[-1] = replace(
                selected_excerpts[-1],
                limitations=_unique_ordered((*selected_excerpts[-1].limitations, "stale index")),
            )
            selected_items[-1] = replace(
                selected_items[-1],
                ambiguity=_unique_ordered((*selected_items[-1].ambiguity, "stale index")),
            )

        unique_skipped_truncs = _unique_ordered(skipped_truncations)
        if unique_skipped_truncs:
            selected_excerpts[-1] = replace(
                selected_excerpts[-1],
                limitations=_unique_ordered(
                    (*selected_excerpts[-1].limitations, *unique_skipped_truncs)
                ),
            )
            selected_items[-1] = replace(
                selected_items[-1],
                truncation=_unique_ordered(
                    (*selected_items[-1].truncation, *unique_skipped_truncs)
                ),
            )

        for item in selected_items:
            _report_add_item(report, item)

        return tuple(selected_excerpts)


def _finder_accepts_report(finder: object) -> bool:
    find_fn = getattr(finder, "find_callers", None)
    if not callable(find_fn):
        return False
    try:
        sig = inspect.signature(find_fn)
    except (TypeError, ValueError):
        return False
    return "report" in sig.parameters or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()
    )


def _finder_source_name(finder: object) -> str:
    if isinstance(finder, PythonAstCallerFinder):
        return "builtin:python_ast"
    if isinstance(finder, TreeSitterCallerFinder):
        return "builtin:tree_sitter"
    if isinstance(finder, CymbalCallerFinder):
        return "cymbal"
    return type(finder).__name__


class ReportingCompositeCallerFinder:
    """Composite `CallerFinder` propagating reports across built-in and custom adapters."""

    def __init__(self, finders: Sequence[CallerFinder] | None = None):
        self.finders: tuple[CallerFinder, ...] = (
            tuple(finders)
            if finders is not None
            else (PythonAstCallerFinder(), TreeSitterCallerFinder())
        )

    def supports(self, path: str) -> bool:
        return any(finder.supports(path) for finder in self.finders)

    def find_callers(
        self,
        root: Path,
        message: str,
        editable: Sequence[str],
        candidates: Sequence[str],
        *,
        max_files: int = 3,
        max_chars: int = 700,
        context_lines: int = 3,
        report: CallerEvidenceReport | None = None,
    ) -> tuple[CallerExcerpt, ...]:
        if max_files <= 0 or max_chars <= 0:
            return ()
        resolved_root = Path(root).resolve()
        editable_tuple = tuple(editable)
        editable_norm_list = sorted(
            {posixpath.normpath(p.replace("\\", "/")) for p in editable_tuple if p}
        )
        editable_norm = set(editable_norm_list)
        unique_candidates = tuple(
            sorted({posixpath.normpath(c.replace("\\", "/")) for c in candidates if c})
        )
        seen_candidates = set(unique_candidates)
        prod_candidates = tuple(c for c in unique_candidates if not is_test_path(c))
        test_candidates = tuple(c for c in unique_candidates if is_test_path(c))
        implicit_same_file = tuple(
            ed for ed in editable_norm_list if ed not in seen_candidates and not is_test_path(ed)
        )
        prod_search_pool = tuple(
            sorted(
                (*prod_candidates, *implicit_same_file),
                key=lambda item: (1 if item in editable_norm else 0, item),
            )
        )
        if not editable_tuple or (
            not prod_search_pool and not test_candidates and report is None
        ):
            return ()

        pools: list[tuple[tuple[str, ...], str]] = []
        if prod_search_pool:
            pools.append((prod_search_pool, "production_only"))
        if test_candidates:
            pools.append((test_candidates, "test_fallback"))
        if not pools:
            pools.append(((), "empty_candidates"))

        for pool, pool_scope in pools:
            by_path: dict[str, tuple[CallerExcerpt, str]] = {}
            for finder in self.finders:
                supported_editable = [p for p in editable_tuple if finder.supports(p)]
                supported_candidates = [p for p in pool if finder.supports(p)]
                if not supported_editable or (not supported_candidates and report is None):
                    continue
                kwargs: dict[str, Any] = {
                    "max_files": max_files + 1,
                    "max_chars": max_chars,
                    "context_lines": context_lines,
                }
                if report is not None and _finder_accepts_report(finder):
                    kwargs["report"] = report
                found = finder.find_callers(
                    resolved_root,
                    message,
                    supported_editable,
                    supported_candidates,
                    **kwargs,
                )
                source_name = _finder_source_name(finder)
                for excerpt in found:
                    by_path.setdefault(excerpt.path, (excerpt, source_name))

            if by_path:
                ordered = sorted(
                    by_path.values(),
                    key=lambda pair: (
                        1
                        if posixpath.normpath(pair[0].path.replace("\\", "/")) in editable_norm
                        else 0,
                        pair[0].path,
                    ),
                )
                selected_pairs = ordered[:max_files]
                selected = [pair[0] for pair in selected_pairs]
                sources = [pair[1] for pair in selected_pairs]

                if len(ordered) > max_files:
                    selected[-1] = replace(
                        selected[-1],
                        limitations=_unique_ordered(
                            (*selected[-1].limitations, "additional files omitted")
                        ),
                    )
                    _report_add_truncation(report, "additional files omitted")

                if report is not None:
                    selected_keys = {(e.path, e.symbol) for e in selected}
                    raw_items = getattr(report, "items", None)
                    if isinstance(raw_items, list):
                        raw_items[:] = [
                            item for item in raw_items if (item.path, item.symbol) in selected_keys
                        ]

                    defs = getattr(report, "definitions", set())
                    missing = {name for _, name in defs} - {e.symbol for e in selected}
                    if missing:
                        selected[-1] = replace(
                            selected[-1],
                            limitations=_unique_ordered(
                                (*selected[-1].limitations, "unmatched target symbols")
                            ),
                        )
                        _report_add_truncation(report, "unmatched target symbols")

                    _report_add_scope(report, pool_scope)
                    if any(src.startswith("builtin:") for src in sources):
                        _report_add_scope(report, "first_direct_calls_only")

                    existing_item_keys = {
                        (item.path, item.symbol, item.source)
                        for item in getattr(report, "items", [])
                    }
                    for excerpt, src in zip(selected, sources):
                        if (excerpt.path, excerpt.symbol, src) in existing_item_keys:
                            continue
                        if src == "cymbal" and any(
                            item.path == excerpt.path
                            and item.symbol == excerpt.symbol
                            and item.source.startswith("cymbal")
                            for item in getattr(report, "items", [])
                        ):
                            continue
                        amb = _unique_ordered(
                            lim
                            for lim in excerpt.limitations
                            if classify_limitation(lim) == "ambiguity"
                        )
                        trunc = _unique_ordered(
                            lim
                            for lim in excerpt.limitations
                            if classify_limitation(lim) == "truncation"
                        )
                        item_scope = (
                            (pool_scope, "first_direct_calls_only")
                            if src.startswith("builtin:")
                            else (pool_scope,)
                        )
                        _report_add_item(
                            report,
                            CallerEvidenceItem(
                                path=excerpt.path,
                                line=excerpt.start_line,
                                symbol=excerpt.symbol,
                                kind="call",
                                source=src,
                                scope=item_scope,
                                ambiguity=amb,
                                truncation=trunc,
                            ),
                        )

                    represented_lims = {lim for e in selected for lim in e.limitations}
                    extra_report_lims = _unique_ordered(
                        lim
                        for lim in (
                            *getattr(report, "ambiguity", ()),
                            *getattr(report, "truncation", ()),
                            *getattr(report, "limitations", ()),
                        )
                        if lim not in represented_lims
                    )
                    if extra_report_lims:
                        selected[-1] = replace(
                            selected[-1],
                            limitations=_unique_ordered(
                                (*selected[-1].limitations, *extra_report_lims)
                            ),
                        )

                    if isinstance(raw_items, list) and raw_items:
                        extra_ambs = tuple(
                            lim
                            for lim in selected[-1].limitations
                            if classify_limitation(lim) == "ambiguity"
                        )
                        extra_truncs = tuple(
                            lim
                            for lim in selected[-1].limitations
                            if classify_limitation(lim) == "truncation"
                        )
                        if extra_ambs or extra_truncs:
                            last_path = selected[-1].path
                            for idx, item in enumerate(raw_items):
                                if item.path == last_path:
                                    raw_items[idx] = replace(
                                        item,
                                        ambiguity=_unique_ordered(
                                            (*item.ambiguity, *extra_ambs)
                                        ),
                                        truncation=_unique_ordered(
                                            (*item.truncation, *extra_truncs)
                                        ),
                                    )

                return tuple(selected)

        return ()


class FallbackCallerFinder:
    """Backend orchestrator implementing ``builtin`` (default), ``auto``, and ``cymbal`` modes.

    Contract:
        - ``builtin``: Always delegates to the built-in finder; never invokes Cymbal.
        - ``cymbal``: Always delegates to Cymbal; any ``CymbalError`` propagates directly.
        - ``auto``: Invokes Cymbal first. Only Cymbal execution, binary, version,
          timeout, output-limit, or protocol errors (``CymbalError``) trigger a
          diagnostic fallback to the built-in finder. A successful Cymbal lookup—
          including an empty (``NO_EVIDENCE``) or impaired (``INCOMPLETE``) result—
          is authoritative and is never silently unioned with built-in results.
    """

    def __init__(
        self,
        *,
        backend: CallerBackend = BACKEND_BUILTIN,
        cymbal_finder: CallerFinder | None = None,
        builtin_finder: CallerFinder | None = None,
    ):
        if backend not in VALID_CALLER_BACKENDS:
            raise ValueError(f"Unknown caller backend: {backend!r}")
        self.backend: CallerBackend = backend
        self.cymbal_finder: CallerFinder = cymbal_finder or CymbalCallerFinder()
        self.builtin_finder: CallerFinder = builtin_finder or ReportingCompositeCallerFinder()

    def supports(self, path: str) -> bool:
        if self.backend == BACKEND_BUILTIN:
            return self.builtin_finder.supports(path)
        if self.backend == BACKEND_CYMBAL:
            return self.cymbal_finder.supports(path)
        return self.cymbal_finder.supports(path) or self.builtin_finder.supports(path)

    def _invoke_finder(
        self,
        finder: CallerFinder,
        root: Path,
        message: str,
        editable: Sequence[str],
        candidates: Sequence[str],
        *,
        max_files: int,
        max_chars: int,
        context_lines: int,
        report: CallerEvidenceReport | None,
    ) -> tuple[CallerExcerpt, ...]:
        kwargs: dict[str, Any] = {
            "max_files": max_files,
            "max_chars": max_chars,
            "context_lines": context_lines,
        }
        if report is not None and _finder_accepts_report(finder):
            kwargs["report"] = report
        return finder.find_callers(
            root,
            message,
            editable,
            candidates,
            **kwargs,
        )

    def find_callers(
        self,
        root: Path,
        message: str,
        editable: Sequence[str],
        candidates: Sequence[str],
        *,
        max_files: int = 3,
        max_chars: int = 700,
        context_lines: int = 3,
        report: CallerEvidenceReport | None = None,
    ) -> tuple[CallerExcerpt, ...]:
        resolved_root = Path(root).resolve()
        if isinstance(report, CymbalEvidenceReport):
            report.backend_requested = self.backend

        if self.backend == BACKEND_BUILTIN:
            if isinstance(report, CymbalEvidenceReport):
                report.backend_used = BACKEND_BUILTIN
                report.fallback_used = False
            return self._invoke_finder(
                self.builtin_finder,
                resolved_root,
                message,
                editable,
                candidates,
                max_files=max_files,
                max_chars=max_chars,
                context_lines=context_lines,
                report=report,
            )

        if self.backend == BACKEND_CYMBAL:
            if isinstance(report, CymbalEvidenceReport):
                report.backend_used = BACKEND_CYMBAL
                report.fallback_used = False
            return self._invoke_finder(
                self.cymbal_finder,
                resolved_root,
                message,
                editable,
                candidates,
                max_files=max_files,
                max_chars=max_chars,
                context_lines=context_lines,
                report=report,
            )

        # `auto` mode: try Cymbal first; on any CymbalError, record diagnostics and fall back.
        cymbal_report = CymbalEvidenceReport(
            backend_requested=self.backend,
            backend_used=BACKEND_CYMBAL,
        )
        try:
            excerpts = self._invoke_finder(
                self.cymbal_finder,
                resolved_root,
                message,
                editable,
                candidates,
                max_files=max_files,
                max_chars=max_chars,
                context_lines=context_lines,
                report=cymbal_report,
            )
        except CymbalError as exc:
            diag = f"cymbal fallback ({exc.code}): {exc}"
            if isinstance(report, CymbalEvidenceReport):
                report.backend_used = BACKEND_BUILTIN
                report.fallback_used = True
            _report_add_diagnostic(report, diag)
            return self._invoke_finder(
                self.builtin_finder,
                resolved_root,
                message,
                editable,
                candidates,
                max_files=max_files,
                max_chars=max_chars,
                context_lines=context_lines,
                report=report,
            )

        if isinstance(report, CymbalEvidenceReport):
            report.backend_used = BACKEND_CYMBAL
            report.fallback_used = False
        if report is not None and cymbal_report.skipped_large > 0:
            if cymbal_report._skipped_large_paths:
                for large_path in sorted(cymbal_report._skipped_large_paths):
                    if hasattr(report, "_skipped_large_paths"):
                        if large_path not in report._skipped_large_paths:
                            report._skipped_large_paths.add(large_path)
                            report.skipped_large += 1
                    elif hasattr(report, "skipped_large"):
                        report.skipped_large += 1
            elif hasattr(report, "skipped_large"):
                report.skipped_large += cymbal_report.skipped_large
        for path, sym in cymbal_report.definitions:
            _report_add_definition(report, path, sym)
        for note in cymbal_report.scope:
            _report_add_scope(report, note)
        for note in cymbal_report.ambiguity:
            _report_add_ambiguity(report, note)
        for note in cymbal_report.truncation:
            _report_add_truncation(report, note)
        for note in cymbal_report.diagnostics:
            _report_add_diagnostic(report, note)
        for item in cymbal_report.items:
            _report_add_item(report, item)
        return excerpts

    def lookup(
        self,
        root: Path | str,
        message: str,
        editable: Sequence[str],
        candidates: Sequence[str],
        *,
        max_files: int = 3,
        max_chars: int = 700,
        context_lines: int = 3,
        report: CymbalEvidenceReport | None = None,
    ) -> CallerLookupResult:
        """Run caller lookup and return an immutable `CallerLookupResult`."""
        active_report = report if report is not None else CymbalEvidenceReport()
        excerpts = self.find_callers(
            Path(root),
            message,
            editable,
            candidates,
            max_files=max_files,
            max_chars=max_chars,
            context_lines=context_lines,
            report=active_report,
        )
        return active_report.to_result(excerpts)


def build_caller_finder(
    backend: CallerBackend = BACKEND_BUILTIN,
    *,
    cymbal_config: CymbalConfig | None = None,
    runner: CymbalCommandRunner | None = None,
    builtin_finders: Sequence[CallerFinder] | None = None,
) -> FallbackCallerFinder:
    """Construct a configured `FallbackCallerFinder` (defaults to `builtin`)."""
    cymbal_finder = CymbalCallerFinder(config=cymbal_config, runner=runner)
    builtin_finder = ReportingCompositeCallerFinder(finders=builtin_finders)
    return FallbackCallerFinder(
        backend=backend,
        cymbal_finder=cymbal_finder,
        builtin_finder=builtin_finder,
    )


def find_callers_with_backend(
    root: Path | str,
    message: str,
    editable: Iterable[str],
    candidates: Iterable[str],
    *,
    backend: CallerBackend = BACKEND_BUILTIN,
    max_files: int = 3,
    max_chars: int = 700,
    context_lines: int = 3,
    cymbal_config: CymbalConfig | None = None,
    runner: CymbalCommandRunner | None = None,
    report: CymbalEvidenceReport | None = None,
) -> CallerLookupResult:
    """High-level entrypoint returning an immutable `CallerLookupResult`."""
    finder = build_caller_finder(
        backend=backend,
        cymbal_config=cymbal_config,
        runner=runner,
    )
    return finder.lookup(
        Path(root),
        message,
        tuple(editable),
        tuple(candidates),
        max_files=max_files,
        max_chars=max_chars,
        context_lines=context_lines,
        report=report,
    )
