"""Language-agnostic bounded local caller lookup and prompt context formatting.

Architecture:
- Boundary DTOs & formatting policy (`CallerExcerpt`, `CallerEvidence`, `CallerScope`,
  `CallerEvidenceResult`, `CallerEvidenceReport`, `format_caller_context`) stay pure
  and independent of parser implementations.
- Parser adapters (`PythonAstCallerFinder`, `TreeSitterCallerFinder`, or injected
  external adapters such as Cymbal) implement the `CallerFinder` protocol and are
  orchestrated by `CompositeCallerFinder`.
- Safe file reading (`read_source`, `read_python`) enforces explicit path, symlink,
  size, and UTF-8 contracts before any parser inspects file contents.
"""

from __future__ import annotations

import ast
import inspect
import posixpath
import re
import warnings
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Iterable, Literal, Protocol, Sequence

DEFAULT_CALLER_HINT = (
    "Check how the caller uses the return value before choosing the implementation."
)

MAX_SOURCE_BYTES = 131072

CONTRACT_CALLER_INSTRUCTIONS = (
    "Read-only caller snapshot for this task.\n"
    "Before outputting any edit block, complete these steps in order:\n"
    "1. Write a CALLER_CONTRACT: section quoting each non-test caller line with its filename, "
    "and explicitly state the semantic meaning of every variable the caller unpacks or assigns "
    "(variable names, element order, types, units, prefixes, and null/sentinel checks).\n"
    "2. Write a TRACE: section using a realistic, asymmetric domain input (use real-world "
    "values with distinct meanings rather than generic placeholders like 'A B' or 'x y') "
    "plus 1 edge case. "
    "Step through your proposed implementation line by line, substitute the returned value into "
    "the caller's exact variable names and final expression, and verify that EVERY caller variable "
    "holds the value its name and downstream operations require (for example, that unpacked "
    "variables are not swapped, numeric values use the caller's unit, and return types or "
    "missing-value sentinels match the caller's checks). If your draft puts values in the wrong "
    "order, unit, or type, fix the logic before writing the edit.\n"
    "3. Output the edit block implementing the described contract."
)

_WORD_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_JS_TS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"})
_EXTRA_LANG_BY_SUFFIX = {
    ".cjs": "javascript",
    ".mts": "typescript",
    ".cts": "typescript",
}
_CALL_NODE_TYPES = frozenset(
    {
        "call_expression",
        "method_invocation",
        "invocation_expression",
        "call",
    }
)
_IDENT_NODE_TYPES = frozenset(
    {
        "identifier",
        "property_identifier",
        "field_identifier",
        "type_identifier",
        "scoped_identifier",
    }
)
_CONTROL_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "new",
        "typeof",
        "sizeof",
        "else",
        "do",
        "case",
        "function",
        "func",
        "def",
        "fn",
        "class",
        "import",
        "export",
        "require",
    }
)
_DEF_REGEXES = (
    re.compile(
        r"(?:^|[\s;])(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s+"
        r"([A-Za-z_$]\w*)\s*[(<]"
    ),
    re.compile(
        r"(?:^|[\s;])(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$]\w*)\s*"
        r"(?::[^=]+)?=\s*(?:async\s+)?(?:<[^>]+>\s*)?"
        r"(?:\([^)]*\)|[A-Za-z_$]\w*)(?:\s*:\s*[^=]+)?\s*=>"
    ),
    re.compile(
        r"(?:^|[\s;])(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$]\w*)\s*"
        r"=\s*(?:async\s+)?function\b"
    ),
    re.compile(r"(?:^|[\s;])func\s+(?:\([^)]+\)\s+)?([A-Za-z_]\w*)\s*[\[(]"),
    re.compile(r"(?:^|[\s;])(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*[(<]"),
    re.compile(r"(?:^|[\s;])def\s+(?:self\.)?([A-Za-z_]\w*)"),
    re.compile(
        r"(?:^|[\s;])(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$]\w*)\b"
    ),
)
_JS_NAMED_IMPORT_RE = re.compile(
    r"\bimport\s+(?!type\b)(?:[A-Za-z_$]\w*\s*,\s*)?\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
_JS_DEFAULT_IMPORT_RE = re.compile(
    r"\bimport\s+(?!type\b)([A-Za-z_$]\w*)\s*(?:,\s*\{[^}]*\}\s*)?from\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
_JS_NS_IMPORT_RE = re.compile(
    r"\bimport\s+\*\s+as\s+([A-Za-z_$]\w*)\s+from\s*['\"]([^'\"]+)['\"]"
)
_JS_DESTRUCTURED_REQUIRE_RE = re.compile(
    r"\b(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
    re.DOTALL,
)
_JS_DEFAULT_REQUIRE_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$]\w*)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)"
)
_DECL_LINE_RE = re.compile(
    r"^\s*(?:(?://|/\*|\*|#)"
    r"|(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function|func|fn|def|class)\b"
    r"|import\b)"
)


EvidenceStatus = Literal["NO_EVIDENCE", "INCOMPLETE", "COMPLETE_WITHIN_SCOPE"]
ScopePool = Literal["production", "tests", "production+tests", "none"]


def _dedupe_str(items: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen[text] = None
    return tuple(seen)


def _is_ambiguity_reason(reason: str) -> bool:
    lowered = reason.lower()
    return any(token in lowered for token in ("ambiguous", "duplicate", "alias", "stale"))


def _is_truncation_reason(reason: str) -> bool:
    lowered = reason.lower()
    return any(token in lowered for token in ("truncated", "omitted", "capped", "budget"))


def _supports_report_arg(finder: CallerFinder) -> bool:
    """Return True if ``finder.find_callers`` accepts a ``report`` keyword argument."""
    try:
        sig = inspect.signature(finder.find_callers)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "report" and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _finder_source(finder: CallerFinder) -> str:
    raw = getattr(finder, "source", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return type(finder).__name__


@dataclass(frozen=True)
class CallerScope:
    """Immutable description of the candidate pool and bounds searched."""

    pool: ScopePool = "none"
    editable: tuple[str, ...] = ()
    candidates_total: int = 0
    candidates_searched: int = 0
    production_candidates: int = 0
    test_candidates: int = 0
    omitted_test_candidates: int = 0
    matched_files: int = 0
    selected_files: int = 0
    max_files: int = 3
    max_chars: int = 700
    context_lines: int = 3
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "editable", tuple(self.editable))
        object.__setattr__(self, "sources", _dedupe_str(self.sources))

    def describe(self) -> str:
        parts = [
            f"pool={self.pool}",
            f"searched={self.candidates_searched}/{self.candidates_total}",
        ]
        if self.omitted_test_candidates:
            parts.append(f"omitted_tests={self.omitted_test_candidates}")
        if self.matched_files or self.selected_files:
            parts.append(f"files={self.selected_files}/{self.matched_files}")
        parts.append(f"max_files={self.max_files}")
        if self.sources:
            parts.append(f"sources={','.join(self.sources)}")
        return "; ".join(parts)


@dataclass(frozen=True)
class CallerEvidence:
    """Immutable typed record describing a single scoped caller evidence item."""

    path: str
    line: int
    symbol: str
    kind: str = "call"
    source: str = "builtin"
    scope: str = ""
    ambiguity: tuple[str, ...] = ()
    truncation: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    start_line: int = 0
    code: str = ""

    def __post_init__(self) -> None:
        resolved_start = self.start_line if self.start_line > 0 else self.line
        object.__setattr__(self, "start_line", resolved_start)

        raw_lims = _dedupe_str(self.limitations)
        amb = _dedupe_str(
            (*self.ambiguity, *(item for item in raw_lims if _is_ambiguity_reason(item)))
        )
        trunc = _dedupe_str(
            (*self.truncation, *(item for item in raw_lims if _is_truncation_reason(item)))
        )
        merged_lims = _dedupe_str((*raw_lims, *amb, *trunc))

        object.__setattr__(self, "ambiguity", amb)
        object.__setattr__(self, "truncation", trunc)
        object.__setattr__(self, "limitations", merged_lims)

    @property
    def incomplete(self) -> bool:
        return bool(self.limitations or self.ambiguity or self.truncation)

    def format(self) -> str:
        """Render the caller evidence with its 1-based file:line header."""
        marker = f" [INCOMPLETE: {', '.join(self.limitations)}]" if self.limitations else ""
        if self.code:
            return f"{self.path}:{self.start_line}{marker}\n{self.code}"
        return f"{self.path}:{self.line}{marker}"


@dataclass(frozen=True)
class CallerExcerpt:
    """Immutable boundary DTO describing a single local caller snapshot."""

    path: str
    start_line: int
    symbol: str
    code: str
    limitations: tuple[str, ...] = ()
    line: int = 0
    kind: str = "call"
    source: str = "builtin"
    scope: str = ""
    ambiguity: tuple[str, ...] = ()
    truncation: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        resolved_line = self.line if self.line > 0 else self.start_line
        object.__setattr__(self, "line", resolved_line)

        raw_lims = _dedupe_str(self.limitations)
        amb = _dedupe_str(
            (*self.ambiguity, *(item for item in raw_lims if _is_ambiguity_reason(item)))
        )
        trunc = _dedupe_str(
            (*self.truncation, *(item for item in raw_lims if _is_truncation_reason(item)))
        )
        merged_lims = _dedupe_str((*raw_lims, *amb, *trunc))

        object.__setattr__(self, "ambiguity", amb)
        object.__setattr__(self, "truncation", trunc)
        object.__setattr__(self, "limitations", merged_lims)

    @property
    def incomplete(self) -> bool:
        return bool(self.limitations or self.ambiguity or self.truncation)

    def to_evidence(self, *, scope: str | None = None) -> CallerEvidence:
        """Convert this excerpt into an immutable typed ``CallerEvidence`` record."""
        return CallerEvidence(
            path=self.path,
            line=self.line,
            symbol=self.symbol,
            kind=self.kind,
            source=self.source,
            scope=self.scope or (scope or ""),
            ambiguity=self.ambiguity,
            truncation=self.truncation,
            limitations=self.limitations,
            start_line=self.start_line,
            code=self.code,
        )

    def format(self) -> str:
        """Render the caller excerpt with its 1-based file:line header."""
        marker = f" [INCOMPLETE: {', '.join(self.limitations)}]" if self.limitations else ""
        return f"{self.path}:{self.start_line}{marker}\n{self.code}"


@dataclass(frozen=True)
class CallerEvidenceResult:
    """Immutable typed aggregate result of scoped caller discovery."""

    status: EvidenceStatus
    evidence: tuple[CallerEvidence, ...] = ()
    excerpts: tuple[CallerExcerpt, ...] = ()
    definitions: tuple[tuple[str, str], ...] = ()
    scope: CallerScope = field(default_factory=CallerScope)
    ambiguity: tuple[str, ...] = ()
    truncation: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ("NO_EVIDENCE", "INCOMPLETE", "COMPLETE_WITHIN_SCOPE"):
            raise ValueError(f"Unknown caller evidence status: {self.status!r}")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "excerpts", tuple(self.excerpts))
        object.__setattr__(self, "definitions", tuple(sorted(set(self.definitions))))

        raw_lims = _dedupe_str(self.limitations)
        amb = _dedupe_str(
            (*self.ambiguity, *(item for item in raw_lims if _is_ambiguity_reason(item)))
        )
        trunc = _dedupe_str(
            (*self.truncation, *(item for item in raw_lims if _is_truncation_reason(item)))
        )
        merged_lims = _dedupe_str((*raw_lims, *amb, *trunc))

        object.__setattr__(self, "ambiguity", amb)
        object.__setattr__(self, "truncation", trunc)
        object.__setattr__(self, "limitations", merged_lims)
        object.__setattr__(self, "sources", _dedupe_str(self.sources))

    @property
    def incomplete(self) -> bool:
        return self.status == "INCOMPLETE"

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence)

    def describe(self) -> str:
        status_label = (
            "COMPLETE_WITHIN_SCOPE (SELECTED)"
            if self.status == "COMPLETE_WITHIN_SCOPE"
            else self.status
        )
        targets = ", ".join(f"{path}:{symbol}" for path, symbol in self.definitions[:8]) or "none"
        if len(self.definitions) > 8:
            targets += ", [additional definitions omitted]"
        if self.excerpts:
            sources_text = ", ".join(e.format().splitlines()[0] for e in self.excerpts)
        elif self.evidence:
            sources_text = ", ".join(e.format().splitlines()[0] for e in self.evidence)
        else:
            sources_text = "none"
        parts = [
            f"{status_label} ({self.scope.describe()}).",
            f"Definitions: {targets}.",
            f"Sources: {sources_text}",
        ]
        if self.ambiguity:
            parts.append(f"Ambiguity: {', '.join(self.ambiguity)}")
        if self.truncation:
            parts.append(f"Truncation: {', '.join(self.truncation)}")
        extra_lims = tuple(
            item
            for item in self.limitations
            if item not in self.ambiguity and item not in self.truncation
        )
        if extra_lims:
            parts.append(f"Limitations: {', '.join(extra_lims)}")
        return " ".join(parts)


@dataclass
class CallerEvidenceReport:
    """Mutable collector passed through caller finders to build a typed evidence result."""

    definitions: set[tuple[str, str]] = field(default_factory=set)
    ambiguity: list[str] = field(default_factory=list)
    truncation: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    scope: CallerScope = field(default_factory=CallerScope)
    result: CallerEvidenceResult | None = None

    def record_definition(self, path: str, symbol: str) -> None:
        self.definitions.add((path, symbol))

    def record_source(self, source: str) -> None:
        text = str(source).strip()
        if text and text not in self.sources:
            self.sources.append(text)

    def record_ambiguity(self, reason: str) -> None:
        text = str(reason).strip()
        if not text:
            return
        if text not in self.ambiguity:
            self.ambiguity.append(text)
        if text not in self.limitations:
            self.limitations.append(text)

    def record_truncation(self, reason: str) -> None:
        text = str(reason).strip()
        if not text:
            return
        if text not in self.truncation:
            self.truncation.append(text)
        if text not in self.limitations:
            self.limitations.append(text)

    def record_limitation(self, reason: str) -> None:
        text = str(reason).strip()
        if not text:
            return
        if text not in self.limitations:
            self.limitations.append(text)
        if _is_ambiguity_reason(text) and text not in self.ambiguity:
            self.ambiguity.append(text)
        if _is_truncation_reason(text) and text not in self.truncation:
            self.truncation.append(text)

    def to_result(self, excerpts: Sequence[CallerExcerpt] = ()) -> CallerEvidenceResult:
        """Freeze collected report state and excerpts into an immutable typed result."""
        excerpt_tuple = tuple(excerpts)
        default_scope = self.scope.pool if self.scope.pool != "none" else ""
        evidence_tuple = tuple(e.to_evidence(scope=default_scope) for e in excerpt_tuple)

        ambiguity = _dedupe_str(
            (*self.ambiguity, *(reason for e in evidence_tuple for reason in e.ambiguity))
        )
        truncation = _dedupe_str(
            (*self.truncation, *(reason for e in evidence_tuple for reason in e.truncation))
        )
        limitations = _dedupe_str(
            (
                *self.limitations,
                *(reason for e in evidence_tuple for reason in e.limitations),
                *ambiguity,
                *truncation,
            )
        )
        sources = _dedupe_str(
            (*self.sources, *(e.source for e in evidence_tuple if e.source), *self.scope.sources)
        )
        scope = replace(self.scope, sources=sources) if sources != self.scope.sources else self.scope

        if not evidence_tuple:
            status: EvidenceStatus = "NO_EVIDENCE"
        elif ambiguity or truncation or limitations:
            status = "INCOMPLETE"
        else:
            status = "COMPLETE_WITHIN_SCOPE"

        return CallerEvidenceResult(
            status=status,
            evidence=evidence_tuple,
            excerpts=excerpt_tuple,
            definitions=tuple(sorted(self.definitions)),
            scope=scope,
            ambiguity=ambiguity,
            truncation=truncation,
            limitations=limitations,
            sources=sources,
        )

    def describe(self, excerpts: Sequence[CallerExcerpt] | None = None) -> str:
        if excerpts is None and self.result is not None:
            return self.result.describe()
        if (
            excerpts is not None
            and self.result is not None
            and tuple(excerpts) == self.result.excerpts
        ):
            return self.result.describe()
        return self.to_result(excerpts or ()).describe()


class CallerFinder(Protocol):
    """Port interface for language-specific local caller finder adapters."""

    def supports(self, path: str) -> bool:
        """Return True if this finder handles files with the given relative path."""
        ...

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
        """Return bounded caller excerpts for editable symbols referenced in message."""
        ...


def read_source(root: Path | str, name: str, *, max_bytes: int = MAX_SOURCE_BYTES) -> str:
    """Safely read a bounded UTF-8 source file inside ``root``.

    Preconditions:
        - ``name`` must resolve to a regular non-symlink file inside ``root.resolve()``.
        - File size must not exceed ``max_bytes`` (default 131072 bytes).
        - File contents must be valid UTF-8.
    Postcondition:
        - Returns decoded text on success, or ``""`` if any guard fails.
    """
    if not name or max_bytes <= 0:
        return ""
    try:
        resolved_root = Path(root).resolve()
        path = resolved_root / name
        if (
            path.is_symlink()
            or path.resolve() != path
            or not path.is_relative_to(resolved_root)
            or not path.is_file()
        ):
            return ""
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        return data.decode("utf-8") if len(data) <= max_bytes else ""
    except (OSError, UnicodeError, ValueError):
        return ""


def read_python(root: Path | str, name: str) -> str:
    """Backwards-compatible safe reader restricted to ``.py`` files."""
    if Path(name).suffix != ".py":
        return ""
    return read_source(root, name)


def _message_words(message: str) -> set[str]:
    return set(_WORD_RE.findall(message or ""))


def _python_module_name(rel_path: str) -> str:
    parts = list(Path(rel_path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _python_package_parts(rel_path: str) -> tuple[str, ...]:
    path = Path(rel_path)
    if path.stem == "__init__":
        return path.parent.parts
    return path.parent.parts


def _resolve_python_import_from(candidate_path: str, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module
    pkg_parts = _python_package_parts(candidate_path)
    up = node.level - 1
    if up > len(pkg_parts):
        return None
    base = pkg_parts if up == 0 else pkg_parts[:-up]
    if node.module:
        return ".".join((*base, node.module)) if base else node.module
    return ".".join(base)


def _dotted_name(expr: ast.AST) -> str | None:
    parts: list[str] = []
    cur = expr
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


class PythonAstCallerFinder:
    """AST-based caller finder for Python modules and methods."""

    source: str = "python_ast"

    def supports(self, path: str) -> bool:
        return Path(path).suffix == ".py"

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
        words = _message_words(message)
        if not words:
            return ()
        ctx = max(0, context_lines)

        target_funcs: set[tuple[str, str]] = set()
        # (module, class_name, method_name)
        target_methods: set[tuple[str, str, str]] = set()
        target_modules: set[str] = set()
        symbol_def_count: dict[str, int] = {}

        for name in editable:
            if not self.supports(name):
                continue
            source = read_python(resolved_root, name)
            if not source:
                continue
            try:
                definitions = ast.parse(source).body
            except (SyntaxError, ValueError):
                continue
            module = _python_module_name(name)
            for node in definitions:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in words:
                        if report is not None:
                            report.record_definition(name, node.name)
                        target_funcs.add((module, node.name))
                        target_modules.add(module)
                        symbol_def_count[node.name] = symbol_def_count.get(node.name, 0) + 1
                elif isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if (
                            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and item.name in words
                        ):
                            if report is not None:
                                report.record_definition(name, item.name)
                            target_methods.add((module, node.name, item.name))
                            target_modules.add(module)
                            symbol_def_count[item.name] = symbol_def_count.get(item.name, 0) + 1

        if not target_funcs and not target_methods:
            return ()

        if report is not None and any(count > 1 for count in symbol_def_count.values()):
            report.record_ambiguity("duplicate symbol definition")

        target_symbols = {sym for _, sym in target_funcs} | {m for _, _, m in target_methods}
        excerpts: list[CallerExcerpt] = []

        for name in sorted(candidates):
            if not self.supports(name):
                continue
            source = read_python(resolved_root, name)
            if not source or not any(symbol in source for symbol in target_symbols):
                continue
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                continue

            func_aliases: dict[str, str] = {}
            module_aliases: dict[str, str] = {}
            unrelated_module_aliases: set[str] = set()
            allowed_method_names: set[str] = set()

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        bound = alias.asname or alias.name
                        if alias.name in target_modules:
                            module_aliases[bound] = alias.name
                            for mod, _, meth in target_methods:
                                if mod == alias.name:
                                    allowed_method_names.add(meth)
                        else:
                            unrelated_module_aliases.add(bound)
                elif isinstance(node, ast.ImportFrom):
                    resolved_mod = _resolve_python_import_from(name, node)
                    if resolved_mod is None:
                        continue
                    for alias in node.names:
                        if alias.name == "*":
                            if resolved_mod in target_modules:
                                for mod, sym in target_funcs:
                                    if mod == resolved_mod:
                                        func_aliases[sym] = sym
                                for mod, _, meth in target_methods:
                                    if mod == resolved_mod:
                                        allowed_method_names.add(meth)
                            continue

                        bound = alias.asname or alias.name
                        if (resolved_mod, alias.name) in target_funcs:
                            func_aliases[bound] = alias.name

                        # Importing a class defined in an editable module enables its methods.
                        for mod, cls_name, meth in target_methods:
                            if mod == resolved_mod and cls_name == alias.name:
                                allowed_method_names.add(meth)

                        # Submodule import: `from . import api` or `from pkg import api`.
                        submod = f"{resolved_mod}.{alias.name}" if resolved_mod else alias.name
                        if submod in target_modules:
                            module_aliases[bound] = submod
                            for mod, _, meth in target_methods:
                                if mod == submod:
                                    allowed_method_names.add(meth)
                        elif (
                            resolved_mod not in target_modules
                            and (resolved_mod, alias.name) not in target_funcs
                        ):
                            # Track unrelated imports to avoid false attribute matches.
                            if not any(
                                mod == resolved_mod and cls_name == alias.name
                                for mod, cls_name, _ in target_methods
                            ):
                                unrelated_module_aliases.add(bound)

            if not func_aliases and not module_aliases and not allowed_method_names:
                continue

            matched_calls = self._find_calls(
                tree,
                func_aliases=func_aliases,
                module_aliases=module_aliases,
                target_funcs=target_funcs,
                allowed_method_names=allowed_method_names,
                unrelated_module_aliases=unrelated_module_aliases,
            )
            if not matched_calls:
                continue

            call_node, symbol, kind = matched_calls[0]
            lines = source.splitlines()
            start = max(0, call_node.lineno - ctx)
            end_line = min(len(lines), call_node.lineno + ctx)
            snippet = "\n".join(lines[start : call_node.lineno + ctx])
            ambiguity: list[str] = []
            truncation: list[str] = []
            if len(snippet) > max_chars:
                truncation.append("excerpt truncated")
            if call_node.end_lineno and call_node.end_lineno > call_node.lineno + ctx:
                truncation.append("multiline call truncated")
            if any(
                other.lineno > end_line or (other.end_lineno or other.lineno) > end_line
                for other, _, _ in matched_calls[1:]
            ):
                truncation.append("additional calls in file omitted")
            if kind == "method_call":
                ambiguity.append("ambiguous method receiver")
            if symbol_def_count.get(symbol, 0) > 1:
                ambiguity.append("duplicate symbol definition")
            excerpts.append(
                CallerExcerpt(
                    path=name,
                    start_line=start + 1,
                    symbol=symbol,
                    code=snippet[:max_chars],
                    line=call_node.lineno,
                    kind=kind,
                    source=self.source,
                    ambiguity=tuple(ambiguity),
                    truncation=tuple(truncation),
                )
            )
            if len(excerpts) >= max_files:
                break

        return tuple(excerpts)

    @staticmethod
    def _find_calls(
        tree: ast.AST,
        *,
        func_aliases: dict[str, str],
        module_aliases: dict[str, str],
        target_funcs: set[tuple[str, str]],
        allowed_method_names: set[str],
        unrelated_module_aliases: set[str],
    ) -> list[tuple[ast.Call, str, str]]:
        matches: list[tuple[int, int, ast.Call, str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in func_aliases:
                matches.append(
                    (node.lineno, node.col_offset, node, func_aliases[node.func.id], "direct_call")
                )
                continue
            if isinstance(node.func, ast.Attribute):
                owner = _dotted_name(node.func.value)
                attr = node.func.attr
                if owner in module_aliases and (module_aliases[owner], attr) in target_funcs:
                    matches.append((node.lineno, node.col_offset, node, attr, "direct_call"))
                    continue
                if attr in allowed_method_names and owner not in unrelated_module_aliases:
                    matches.append((node.lineno, node.col_offset, node, attr, "method_call"))
        matches.sort(key=lambda item: (item[0], item[1]))
        return [(call_node, symbol, kind) for _, _, call_node, symbol, kind in matches]

    @classmethod
    def _find_first_call(
        cls,
        tree: ast.AST,
        *,
        func_aliases: dict[str, str],
        module_aliases: dict[str, str],
        target_funcs: set[tuple[str, str]],
        allowed_method_names: set[str],
        unrelated_module_aliases: set[str],
    ) -> tuple[ast.Call, str] | None:
        matches = cls._find_calls(
            tree,
            func_aliases=func_aliases,
            module_aliases=module_aliases,
            target_funcs=target_funcs,
            allowed_method_names=allowed_method_names,
            unrelated_module_aliases=unrelated_module_aliases,
        )
        if not matches:
            return None
        call_node, symbol, _ = matches[0]
        return call_node, symbol


def _resolve_lang(path: str) -> str | None:
    try:
        from grep_ast import filename_to_lang

        lang = filename_to_lang(path)
        if lang:
            return lang
    except Exception:
        pass
    return _EXTRA_LANG_BY_SUFFIX.get(Path(path).suffix.lower())


def _get_scm_path(lang: str):
    try:
        warnings.simplefilter("ignore", category=FutureWarning)
        from grep_ast.tsl import USING_TSL_PACK
    except Exception:
        USING_TSL_PACK = False

    if USING_TSL_PACK:
        try:
            path = resources.files("aider").joinpath(
                "queries", "tree-sitter-language-pack", f"{lang}-tags.scm"
            )
            if path.exists():
                return path
        except (KeyError, OSError, FileNotFoundError):
            pass

    try:
        path = resources.files("aider").joinpath(
            "queries", "tree-sitter-languages", f"{lang}-tags.scm"
        )
        if path.exists():
            return path
    except (KeyError, OSError, FileNotFoundError):
        pass
    return None


def _run_ts_captures(query, root_node) -> list[tuple[object, str]]:
    if hasattr(query, "captures"):
        raw = query.captures(root_node)
    else:
        from tree_sitter import QueryCursor

        raw = QueryCursor(query).captures(root_node)

    results: list[tuple[object, str]] = []
    if isinstance(raw, dict):
        for tag, nodes in raw.items():
            for node in nodes:
                results.append((node, tag))
    else:
        for node, tag in raw:
            results.append((node, tag))
    return results


def _strip_js_ext(path_str: str) -> str:
    p = Path(path_str)
    if p.suffix.lower() in _JS_TS_SUFFIXES:
        return p.with_suffix("").as_posix()
    return p.as_posix()


def _js_specifier_matches_editable(
    specifier: str,
    candidate_path: str,
    editable_path: str,
) -> bool:
    editable_no_ext = _strip_js_ext(editable_path)
    editable_stem = Path(editable_no_ext).name
    spec_clean = specifier.split("?", 1)[0].split("#", 1)[0]
    if spec_clean.startswith("."):
        cand_dir = Path(candidate_path).parent.as_posix()
        joined = posixpath.normpath(posixpath.join(cand_dir, spec_clean))
        if joined.startswith("./"):
            joined = joined[2:]
        joined_no_ext = _strip_js_ext(joined)
        if joined_no_ext == editable_no_ext:
            return True
        # Handle directory index imports (`./api` -> `api/index.js`).
        if editable_stem == "index" and Path(editable_no_ext).parent.as_posix() == joined_no_ext:
            return True
        return False

    spec_no_ext = _strip_js_ext(spec_clean)
    spec_stem = Path(spec_no_ext).name
    return spec_no_ext == editable_no_ext or spec_stem == editable_stem


def _parse_js_imports(source: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """Extract JS/TS named imports/requires and namespace/default bindings.

    Returns:
        - named: list of ``(original_name, local_name, specifier)``
        - namespaces: list of ``(local_name, specifier)``
    """
    named: list[tuple[str, str, str]] = []
    namespaces: list[tuple[str, str]] = []

    for match in _JS_NAMED_IMPORT_RE.finditer(source):
        block, specifier = match.group(1), match.group(2)
        for part in block.split(","):
            item = part.strip()
            if not item or item.startswith("type "):
                continue
            alias_match = re.match(r"^([A-Za-z_$]\w*)(?:\s+as\s+([A-Za-z_$]\w*))?$", item)
            if alias_match:
                orig = alias_match.group(1)
                local = alias_match.group(2) or orig
                named.append((orig, local, specifier))

    for match in _JS_DEFAULT_IMPORT_RE.finditer(source):
        local, specifier = match.group(1), match.group(2)
        if local != "type":
            named.append((local, local, specifier))
            namespaces.append((local, specifier))

    for match in _JS_NS_IMPORT_RE.finditer(source):
        namespaces.append((match.group(1), match.group(2)))

    for match in _JS_DESTRUCTURED_REQUIRE_RE.finditer(source):
        block, specifier = match.group(1), match.group(2)
        for part in block.split(","):
            item = part.strip()
            if not item:
                continue
            alias_match = re.match(r"^([A-Za-z_$]\w*)(?:\s*:\s*([A-Za-z_$]\w*))?$", item)
            if alias_match:
                orig = alias_match.group(1)
                local = alias_match.group(2) or orig
                named.append((orig, local, specifier))

    for match in _JS_DEFAULT_REQUIRE_RE.finditer(source):
        local, specifier = match.group(1), match.group(2)
        named.append((local, local, specifier))
        namespaces.append((local, specifier))

    return named, namespaces


def _extract_callee_name(call_node) -> tuple[str | None, str | None]:
    """Return ``(owner_text, callee_name)`` from a tree-sitter call AST node."""
    func = call_node.child_by_field_name("function")
    if func is None:
        func = call_node.child_by_field_name("name")
        if func is not None:
            obj = call_node.child_by_field_name("object")
            owner = obj.text.decode("utf-8", errors="ignore") if obj is not None else None
            return owner, func.text.decode("utf-8", errors="ignore")
        return None, None

    if func.type in _IDENT_NODE_TYPES:
        text = func.text.decode("utf-8", errors="ignore")
        if "::" in text:
            owner, _, name = text.rpartition("::")
            return owner, name
        return None, text

    # Member / selector expressions (`obj.fn`, `pkg.Fn`, `api::fn`).
    for prop_field in ("property", "field", "name"):
        prop = func.child_by_field_name(prop_field)
        if prop is not None:
            owner_node = (
                func.child_by_field_name("object")
                or func.child_by_field_name("operand")
                or func.child_by_field_name("expression")
            )
            owner = owner_node.text.decode("utf-8", errors="ignore") if owner_node else None
            return owner, prop.text.decode("utf-8", errors="ignore")

    # Fallback: scan direct children for last identifier-like token.
    ident_children = [c for c in func.children if c.type in _IDENT_NODE_TYPES]
    if ident_children:
        return None, ident_children[-1].text.decode("utf-8", errors="ignore")
    return None, None


class TreeSitterCallerFinder:
    """Language-agnostic caller finder backed by tree-sitter queries with regex fallback."""

    source: str = "tree_sitter"

    def supports(self, path: str) -> bool:
        return Path(path).suffix != ".py"

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
        words = _message_words(message)
        if not words:
            return ()
        ctx = max(0, context_lines)

        # Map symbol -> set of editable paths defining it.
        targets: dict[str, set[str]] = {}
        for name in editable:
            if not self.supports(name):
                continue
            source = read_source(resolved_root, name)
            if not source or not any(word in source for word in words):
                continue
            defined = self._extract_definitions(name, source)
            for sym in defined:
                if sym in words:
                    if report is not None:
                        report.record_definition(name, sym)
                    targets.setdefault(sym, set()).add(name)

        if not targets:
            return ()

        if report is not None and any(len(paths) > 1 for paths in targets.values()):
            report.record_ambiguity("duplicate symbol definition")

        excerpts: list[CallerExcerpt] = []
        for name in sorted(candidates):
            if not self.supports(name):
                continue
            source = read_source(resolved_root, name)
            if not source:
                continue

            call_names, ns_aliases = self._resolve_candidate_bindings(name, source, targets)
            if not call_names and not ns_aliases:
                continue

            search_tokens = set(call_names) | {
                sym for syms in ns_aliases.values() for sym in syms
            }
            if not any(tok in source for tok in search_tokens):
                continue

            matches, kind = self._find_call_lines(name, source, call_names, ns_aliases)
            if not matches:
                continue

            lineno, symbol = matches[0]
            lines = source.splitlines()
            start = max(0, lineno - ctx)
            end_row = min(len(lines) - 1, lineno + ctx)
            snippet = "\n".join(lines[start : lineno + ctx + 1])
            ambiguity: list[str] = []
            truncation: list[str] = []
            if len(snippet) > max_chars:
                truncation.append("excerpt truncated")
            if any(other_row > end_row for other_row, _ in matches[1:]):
                truncation.append("additional calls in file omitted")
            is_js_ts = Path(name).suffix.lower() in _JS_TS_SUFFIXES
            if is_js_ts:
                named, namespaces = _parse_js_imports(source)
                specifiers = [spec for imported, _, spec in named if imported == symbol]
                specifiers.extend(spec for _, spec in namespaces)
                resolved = any(
                    _js_specifier_matches_editable(spec, name, target)
                    for spec in specifiers
                    for target in targets[symbol]
                )
                if len(targets[symbol]) != 1 or not resolved:
                    ambiguity.append("ambiguous symbol binding")
                if len(targets[symbol]) > 1:
                    ambiguity.append("duplicate symbol definition")
            elif len(targets[symbol]) != 1:
                ambiguity.append("ambiguous symbol binding")
                if len(targets[symbol]) > 1:
                    ambiguity.append("duplicate symbol definition")
            excerpts.append(
                CallerExcerpt(
                    path=name,
                    start_line=start + 1,
                    symbol=symbol,
                    code=snippet[:max_chars],
                    line=lineno + 1,
                    kind=kind,
                    source=self.source,
                    ambiguity=tuple(ambiguity),
                    truncation=tuple(truncation),
                )
            )
            if len(excerpts) >= max_files:
                break

        return tuple(excerpts)

    def _extract_definitions(self, path: str, source: str) -> set[str]:
        defined: set[str] = set()
        lang = _resolve_lang(path)
        if lang:
            defined.update(self._extract_ts_definitions(lang, source))
        # Augment with regex fallback (handles arrow functions in TS and unsupported languages).
        for pattern in _DEF_REGEXES:
            for match in pattern.finditer(source):
                name = match.group(1)
                if name and name not in _CONTROL_KEYWORDS:
                    defined.add(name)
        return defined

    def _extract_ts_definitions(self, lang: str, source: str) -> set[str]:
        scm_path = _get_scm_path(lang)
        if scm_path is None:
            return set()
        try:
            warnings.simplefilter("ignore", category=FutureWarning)
            from grep_ast.tsl import get_language, get_parser
            from tree_sitter import Query

            language = get_language(lang)
            parser = get_parser(lang)
            tree = parser.parse(source.encode("utf-8"))
            query = Query(language, scm_path.read_text(encoding="utf-8"))
            captures = _run_ts_captures(query, tree.root_node)
        except Exception:
            return set()

        defined: set[str] = set()
        for node, tag in captures:
            if tag.startswith("name.definition."):
                text = node.text.decode("utf-8", errors="ignore")
                if text:
                    defined.add(text)
        return defined

    def _resolve_candidate_bindings(
        self,
        candidate_path: str,
        source: str,
        targets: dict[str, set[str]],
    ) -> tuple[dict[str, str], dict[str, set[str]]]:
        """Compute allowed local call names and namespace aliases for a candidate file."""
        is_js_ts = Path(candidate_path).suffix.lower() in _JS_TS_SUFFIXES
        if not is_js_ts:
            return {sym: sym for sym in targets}, {}

        named_imports, ns_imports = _parse_js_imports(source)
        if not named_imports and not ns_imports:
            return {sym: sym for sym in targets}, {}

        call_names: dict[str, str] = {}
        ns_aliases: dict[str, set[str]] = {}
        imported_from_matching: set[str] = set()
        imported_from_other: set[str] = set()

        for orig, local, specifier in named_imports:
            if orig not in targets:
                continue
            editable_paths = targets[orig]
            if any(
                _js_specifier_matches_editable(specifier, candidate_path, ed)
                for ed in editable_paths
            ):
                call_names[local] = orig
                imported_from_matching.add(orig)
            else:
                imported_from_other.add(orig)

        for ns_name, specifier in ns_imports:
            for sym, editable_paths in targets.items():
                if any(
                    _js_specifier_matches_editable(specifier, candidate_path, ed)
                    for ed in editable_paths
                ):
                    ns_aliases.setdefault(ns_name, set()).add(sym)
                    imported_from_matching.add(sym)

        # Allow unimported global/ambient symbols unless shadowed by an import from another module.
        for sym in targets:
            if sym not in imported_from_matching and sym not in imported_from_other:
                call_names[sym] = sym

        return call_names, ns_aliases

    def _find_call_lines(
        self,
        path: str,
        source: str,
        call_names: dict[str, str],
        ns_aliases: dict[str, set[str]],
    ) -> tuple[list[tuple[int, str]], str]:
        lang = _resolve_lang(path)
        if lang:
            matches = self._find_ts_call_lines(lang, source, call_names, ns_aliases)
            if matches:
                return matches, "direct_call"
        return self._find_regex_call_lines(source, call_names, ns_aliases), "regex_call"

    def _find_first_call_line(
        self,
        path: str,
        source: str,
        call_names: dict[str, str],
        ns_aliases: dict[str, set[str]],
    ) -> tuple[int, str] | None:
        matches, _ = self._find_call_lines(path, source, call_names, ns_aliases)
        return matches[0] if matches else None

    def _find_ts_call_lines(
        self,
        lang: str,
        source: str,
        call_names: dict[str, str],
        ns_aliases: dict[str, set[str]],
    ) -> list[tuple[int, str]]:
        try:
            warnings.simplefilter("ignore", category=FutureWarning)
            from grep_ast.tsl import get_language, get_parser
            from tree_sitter import Query

            language = get_language(lang)
            parser = get_parser(lang)
            tree = parser.parse(source.encode("utf-8"))
        except Exception:
            return []

        scm_path = _get_scm_path(lang)
        scm_text = ""
        if scm_path is not None:
            try:
                scm_text = scm_path.read_text(encoding="utf-8")
            except OSError:
                scm_text = ""

        # Path 1: Tag query captures `name.reference.call`.
        if scm_text and "name.reference.call" in scm_text:
            try:
                captures = _run_ts_captures(Query(language, scm_text), tree.root_node)
            except Exception:
                captures = []
            matches: list[tuple[int, int, str]] = []
            for node, tag in captures:
                if tag not in ("name.reference.call", "name.reference.class"):
                    continue
                called = node.text.decode("utf-8", errors="ignore")
                row, col = node.start_point
                if called in call_names:
                    matches.append((row, col, call_names[called]))
                    continue
                if ns_aliases and node.parent is not None:
                    owner_node = node.parent.child_by_field_name("object")
                    owner = (
                        owner_node.text.decode("utf-8", errors="ignore")
                        if owner_node is not None
                        else None
                    )
                    if owner in ns_aliases and called in ns_aliases[owner]:
                        matches.append((row, col, called))
            if matches:
                matches.sort(key=lambda item: (item[0], item[1]))
                return [(row, sym) for row, _, sym in matches]

        # Path 2: Tree-sitter AST walk for call nodes (e.g. TypeScript, Java, C#).
        ast_matches: list[tuple[int, int, str]] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in _CALL_NODE_TYPES:
                owner, called = _extract_callee_name(node)
                row, col = node.start_point
                if called and called in call_names:
                    ast_matches.append((row, col, call_names[called]))
                elif owner and called and owner in ns_aliases and called in ns_aliases[owner]:
                    ast_matches.append((row, col, called))
            stack.extend(reversed(node.children))

        if ast_matches:
            ast_matches.sort(key=lambda item: (item[0], item[1]))
            return [(row, sym) for row, _, sym in ast_matches]
        return []

    def _find_ts_call_line(
        self,
        lang: str,
        source: str,
        call_names: dict[str, str],
        ns_aliases: dict[str, set[str]],
    ) -> tuple[int, str] | None:
        matches = self._find_ts_call_lines(lang, source, call_names, ns_aliases)
        return matches[0] if matches else None

    @staticmethod
    def _find_regex_call_lines(
        source: str,
        call_names: dict[str, str],
        ns_aliases: dict[str, set[str]],
    ) -> list[tuple[int, str]]:
        patterns: list[tuple[re.Pattern[str], str]] = [
            (re.compile(rf"\b{re.escape(local)}\s*\("), sym)
            for local, sym in sorted(call_names.items())
        ]
        for ns, syms in sorted(ns_aliases.items()):
            for sym in sorted(syms):
                patterns.append(
                    (re.compile(rf"\b{re.escape(ns)}\s*\.\s*{re.escape(sym)}\s*\("), sym)
                )

        if not patterns:
            return []

        matches: list[tuple[int, str]] = []
        for row, line in enumerate(source.splitlines()):
            if _DECL_LINE_RE.match(line):
                continue
            for pattern, sym in patterns:
                if pattern.search(line):
                    matches.append((row, sym))
                    break
        return matches

    @classmethod
    def _find_regex_call_line(
        cls,
        source: str,
        call_names: dict[str, str],
        ns_aliases: dict[str, set[str]],
    ) -> tuple[int, str] | None:
        matches = cls._find_regex_call_lines(source, call_names, ns_aliases)
        return matches[0] if matches else None


_TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs"})


def _is_test_path(path: str) -> bool:
    """Return True for test or smoke harness files (deprioritized vs production callers)."""
    p = Path(path.replace("\\", "/"))
    if any(part.lower() in _TEST_DIR_NAMES for part in p.parts[:-1]):
        return True
    name = p.name.lower()
    stem = p.stem.lower()
    return (
        stem.startswith("test_")
        or stem.endswith("_test")
        or stem in ("smoke", "conftest")
        or stem.startswith("smoke_")
        or ".test." in name
        or ".spec." in name
    )


class CompositeCallerFinder:
    """Orchestrates pluggable `CallerFinder` adapters with deduplication and budgeting."""

    def __init__(self, finders: Sequence[CallerFinder] | None = None):
        self.finders: tuple[CallerFinder, ...] = (
            tuple(finders)
            if finders is not None
            else (PythonAstCallerFinder(), TreeSitterCallerFinder())
        )

    def supports(self, path: str) -> bool:
        return any(finder.supports(path) for finder in self.finders)

    def discover_callers(
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
    ) -> CallerEvidenceResult:
        active_report = report if report is not None else CallerEvidenceReport()
        editable_tuple = tuple(editable)
        editable_norm = {Path(p.replace("\\", "/")).as_posix() for p in editable_tuple if p}

        seen_candidates: set[str] = set()
        unique_candidates_list: list[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            norm = Path(candidate.replace("\\", "/")).as_posix()
            if norm not in editable_norm and norm not in seen_candidates:
                seen_candidates.add(norm)
                unique_candidates_list.append(norm)
        unique_candidates = tuple(sorted(unique_candidates_list))
        prod_candidates = tuple(c for c in unique_candidates if not _is_test_path(c))
        test_candidates = tuple(c for c in unique_candidates if _is_test_path(c))

        if max_files <= 0 or max_chars <= 0:
            active_report.record_truncation("budget cap zero")
            active_report.scope = CallerScope(
                pool="none",
                editable=editable_tuple,
                candidates_total=len(unique_candidates),
                candidates_searched=0,
                production_candidates=len(prod_candidates),
                test_candidates=len(test_candidates),
                omitted_test_candidates=len(test_candidates),
                matched_files=0,
                selected_files=0,
                max_files=max_files,
                max_chars=max_chars,
                context_lines=context_lines,
                sources=tuple(active_report.sources),
            )
            result = active_report.to_result(())
            active_report.result = result
            return result

        if not editable_tuple:
            active_report.scope = CallerScope(
                pool="none",
                editable=(),
                candidates_total=len(unique_candidates),
                candidates_searched=0,
                production_candidates=len(prod_candidates),
                test_candidates=len(test_candidates),
                omitted_test_candidates=0,
                matched_files=0,
                selected_files=0,
                max_files=max_files,
                max_chars=max_chars,
                context_lines=context_lines,
                sources=tuple(active_report.sources),
            )
            result = active_report.to_result(())
            active_report.result = result
            return result

        resolved_root = Path(root).resolve()

        # Prefer real production callers; only fall back to test/smoke files when no
        # production caller matches (prevents weak smoke assertions from diluting contracts).
        named_pools: list[tuple[ScopePool, tuple[str, ...]]] = []
        if prod_candidates:
            named_pools.append(("production", prod_candidates))
        if test_candidates:
            named_pools.append(("tests", test_candidates))
        if not named_pools:
            named_pools.append(("none", ()))

        searched_count = 0
        for pool_name, pool in named_pools:
            searched_count += len(pool)
            by_key: dict[tuple[str, int, str], CallerExcerpt] = {}
            for finder in self.finders:
                supported_editable = [p for p in editable_tuple if finder.supports(p)]
                supported_candidates = [p for p in pool if finder.supports(p)]
                if not supported_editable:
                    continue
                finder_source = _finder_source(finder)
                active_report.record_source(finder_source)

                before_amb = set(active_report.ambiguity)
                before_trunc = set(active_report.truncation)
                before_lim = set(active_report.limitations)

                kwargs: dict[str, object] = {
                    "max_files": max_files + 1,
                    "max_chars": max_chars,
                    "context_lines": context_lines,
                }
                if _supports_report_arg(finder):
                    kwargs["report"] = active_report

                found = finder.find_callers(
                    resolved_root,
                    message,
                    supported_editable,
                    supported_candidates,
                    **kwargs,
                )

                added_amb = tuple(x for x in active_report.ambiguity if x not in before_amb)
                added_trunc = tuple(x for x in active_report.truncation if x not in before_trunc)
                added_lim = tuple(x for x in active_report.limitations if x not in before_lim)

                for raw_excerpt in found:
                    source_name = (
                        raw_excerpt.source
                        if raw_excerpt.source and raw_excerpt.source != "builtin"
                        else finder_source
                    )
                    active_report.record_source(source_name)
                    enriched = replace(
                        raw_excerpt,
                        source=source_name,
                        scope=raw_excerpt.scope or pool_name,
                        ambiguity=_dedupe_str((*raw_excerpt.ambiguity, *added_amb)),
                        truncation=_dedupe_str((*raw_excerpt.truncation, *added_trunc)),
                        limitations=_dedupe_str((*raw_excerpt.limitations, *added_lim)),
                    )
                    key = (enriched.path, enriched.line, enriched.symbol)
                    existing = by_key.get(key)
                    if existing is None:
                        by_key[key] = enriched
                    else:
                        by_key[key] = replace(
                            existing,
                            ambiguity=_dedupe_str((*existing.ambiguity, *enriched.ambiguity)),
                            truncation=_dedupe_str((*existing.truncation, *enriched.truncation)),
                            limitations=_dedupe_str((*existing.limitations, *enriched.limitations)),
                        )

            if by_key:
                ordered = sorted(
                    by_key.values(), key=lambda item: (item.path, item.line, item.symbol)
                )
                distinct_paths = tuple(dict.fromkeys(item.path for item in ordered))
                allowed_paths = set(distinct_paths[:max_files])
                selected = [item for item in ordered if item.path in allowed_paths]

                if len(distinct_paths) > max_files:
                    active_report.record_truncation("additional files omitted")
                    selected[-1] = replace(
                        selected[-1],
                        truncation=(*selected[-1].truncation, "additional files omitted"),
                    )

                missing = {name for _, name in active_report.definitions} - {
                    e.symbol for e in selected
                }
                if missing:
                    active_report.record_limitation("unmatched target symbols")
                    selected[-1] = replace(
                        selected[-1],
                        limitations=(*selected[-1].limitations, "unmatched target symbols"),
                    )

                represented_amb = {item for e in selected for item in e.ambiguity}
                represented_trunc = {item for e in selected for item in e.truncation}
                represented_lim = {item for e in selected for item in e.limitations}
                extra_amb = tuple(x for x in active_report.ambiguity if x not in represented_amb)
                extra_trunc = tuple(
                    x for x in active_report.truncation if x not in represented_trunc
                )
                extra_lim = tuple(x for x in active_report.limitations if x not in represented_lim)
                if extra_amb or extra_trunc or extra_lim:
                    selected[-1] = replace(
                        selected[-1],
                        ambiguity=(*selected[-1].ambiguity, *extra_amb),
                        truncation=(*selected[-1].truncation, *extra_trunc),
                        limitations=(*selected[-1].limitations, *extra_lim),
                    )

                omitted_tests = len(test_candidates) if pool_name == "production" else 0
                active_report.scope = CallerScope(
                    pool=pool_name,
                    editable=editable_tuple,
                    candidates_total=len(unique_candidates),
                    candidates_searched=searched_count,
                    production_candidates=len(prod_candidates),
                    test_candidates=len(test_candidates),
                    omitted_test_candidates=omitted_tests,
                    matched_files=len(distinct_paths),
                    selected_files=len(allowed_paths),
                    max_files=max_files,
                    max_chars=max_chars,
                    context_lines=context_lines,
                    sources=tuple(active_report.sources),
                )
                result = active_report.to_result(tuple(selected))
                active_report.result = result
                return result

        if prod_candidates and test_candidates:
            final_pool: ScopePool = "production+tests"
        elif prod_candidates:
            final_pool = "production"
        elif test_candidates:
            final_pool = "tests"
        else:
            final_pool = "none"

        active_report.scope = CallerScope(
            pool=final_pool,
            editable=editable_tuple,
            candidates_total=len(unique_candidates),
            candidates_searched=searched_count,
            production_candidates=len(prod_candidates),
            test_candidates=len(test_candidates),
            omitted_test_candidates=0,
            matched_files=0,
            selected_files=0,
            max_files=max_files,
            max_chars=max_chars,
            context_lines=context_lines,
            sources=tuple(active_report.sources),
        )
        result = active_report.to_result(())
        active_report.result = result
        return result

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
        return self.discover_callers(
            root,
            message,
            editable,
            candidates,
            max_files=max_files,
            max_chars=max_chars,
            context_lines=context_lines,
            report=report,
        ).excerpts


_DEFAULT_FINDER = CompositeCallerFinder()


def discover_related_callers(
    root: Path | str,
    message: str,
    editable: Iterable[str],
    candidates: Iterable[str],
    *,
    max_files: int = 3,
    max_chars: int = 700,
    context_lines: int = 3,
    report: CallerEvidenceReport | None = None,
    finder: CallerFinder | None = None,
) -> CallerEvidenceResult:
    """Discover bounded local caller evidence and return an immutable scoped result."""
    target_finder = _DEFAULT_FINDER if finder is None else finder
    resolved_root = Path(root)
    editable_tuple = tuple(editable)
    candidates_tuple = tuple(candidates)

    if isinstance(target_finder, CompositeCallerFinder):
        return target_finder.discover_callers(
            resolved_root,
            message,
            editable_tuple,
            candidates_tuple,
            max_files=max_files,
            max_chars=max_chars,
            context_lines=context_lines,
            report=report,
        )

    discover_fn = getattr(target_finder, "discover_callers", None)
    if callable(discover_fn):
        return discover_fn(
            resolved_root,
            message,
            editable_tuple,
            candidates_tuple,
            max_files=max_files,
            max_chars=max_chars,
            context_lines=context_lines,
            report=report,
        )

    return CompositeCallerFinder((target_finder,)).discover_callers(
        resolved_root,
        message,
        editable_tuple,
        candidates_tuple,
        max_files=max_files,
        max_chars=max_chars,
        context_lines=context_lines,
        report=report,
    )


def find_related_callers(
    root: Path | str,
    message: str,
    editable: Iterable[str],
    candidates: Iterable[str],
    *,
    max_files: int = 3,
    max_chars: int = 700,
    context_lines: int = 3,
    report: CallerEvidenceReport | None = None,
    finder: CallerFinder | None = None,
) -> tuple[CallerExcerpt, ...]:
    """Find bounded local caller excerpts across Python and non-Python source files."""
    return discover_related_callers(
        root,
        message,
        editable,
        candidates,
        max_files=max_files,
        max_chars=max_chars,
        context_lines=context_lines,
        report=report,
        finder=finder,
    ).excerpts


def related_callers(
    root: Path | str,
    message: str,
    editable: Iterable[str],
    candidates: Iterable[str],
    *,
    finder: CallerFinder | None = None,
) -> str:
    """Backwards-compatible wrapper returning formatted caller excerpts joined by blank lines."""
    excerpts = find_related_callers(
        root,
        message,
        editable,
        candidates,
        context_lines=3,
        finder=finder,
    )
    return "\n\n".join(f"{e.path}:{e.start_line}\n{e.code}" for e in excerpts)


def format_caller_context(
    excerpts: Iterable[CallerExcerpt] | CallerEvidenceResult,
    *,
    hint: str | None = None,
    mode: str = "passive",
) -> str:
    """Format caller excerpts according to the selected prompting policy.

    Preconditions:
        - ``mode`` must be ``"passive"`` or ``"contract"``; otherwise raises ``ValueError``.
    Postconditions:
        - Returns ``""`` when ``excerpts`` is empty.
        - Otherwise returns a read-only caller context block formatted for ``mode``.
    """
    if mode not in ("passive", "contract"):
        raise ValueError(f"Unknown caller context mode: {mode!r}")

    items = excerpts.excerpts if isinstance(excerpts, CallerEvidenceResult) else tuple(excerpts)
    if not items:
        return ""

    body = "\n\n".join(item.format() for item in items)
    if mode == "passive":
        hint_text = DEFAULT_CALLER_HINT if hint is None else hint
        heading = "Read-only caller snapshot for this task."
        if hint_text.strip():
            heading += " " + hint_text.strip()
        return f"{heading}\n{body}"

    header = CONTRACT_CALLER_INSTRUCTIONS
    if hint and hint.strip():
        header = f"{header}\n{hint.strip()}"
    return f"{header}\n{body}"
