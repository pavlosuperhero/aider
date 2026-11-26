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

WORD_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_WORD_RE = WORD_RE
_JS_TS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"})
_EXTRA_LANG_BY_SUFFIX = {
    ".cjs": "javascript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".hh": "cpp",
    ".ipp": "cpp",
    ".tpp": "cpp",
    ".sh": "bash",
    ".bash": "bash",
    ".bats": "bash",
}
_BASH_SHEBANG_RE = re.compile(
    r"^#!\s*(?:/usr/bin/env\s+(?:-\S+\s+)*)?(?:(?:/[^\s/]+)*/)?(?:ba|da|a|k|z)?sh(?:\s|$)"
)
C_CPP_HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx"})
_C_CPP_HEADER_SUFFIXES = C_CPP_HEADER_SUFFIXES
_C_DECLARATION_BOUNDARY_NODES = frozenset(
    {
        "compound_statement",
        "declaration",
        "field_declaration",
        "parameter_declaration",
        "parameter_list",
    }
)
LARGE_FILE_LIMITATION = "large candidate file omitted (>128 KiB)"
_LARGE_FILE_LIMITATION = LARGE_FILE_LIMITATION
_SUBTOKEN_STOPWORDS = frozenset(
    {
        "fix",
        "bug",
        "add",
        "set",
        "get",
        "run",
        "file",
        "edit",
        "only",
        "code",
        "test",
        "make",
        "use",
        "new",
        "from",
        "with",
        "when",
        "that",
        "this",
        "into",
        "also",
        "must",
        "keep",
        "return",
        "value",
        "error",
        "check",
        "handle",
        "update",
        "support",
        "ensure",
        "should",
        "change",
        "behavior",
        "function",
        "method",
        "class",
        "module",
        "input",
        "output",
        "the",
        "and",
        "for",
        "not",
        "all",
        "are",
        "was",
        "can",
        "has",
        "have",
        "does",
        "non",
    }
)
_CAMEL_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")
_RUST_HOMONYMOUS_TRAIT_METHODS = frozenset(
    {
        "fmt",
        "from",
        "try_from",
        "into",
        "default",
        "clone",
        "eq",
        "ne",
        "cmp",
        "partial_cmp",
        "hash",
        "drop",
        "deref",
        "index",
        "next",
        "read",
        "write",
        "as_ref",
        "borrow",
    }
)
_DEF_CONTAINER_NODE_TYPES = frozenset(
    {
        "function_definition",
        "function_declaration",
        "generator_function_declaration",
        "function_item",
        "method_definition",
        "method_declaration",
        "constructor_declaration",
        "method",
        "variable_declarator",
    }
)
_BASH_DEF_REGEXES = (
    re.compile(
        r"(?:^|[;\n])\s*function\s+([A-Za-z_][A-Za-z0-9_-]*)(?:\s*\(\s*\))?\s*[\{(]"
    ),
    re.compile(r"(?:^|[;\n])\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\(\s*\)\s*[\{(]"),
)
_BASH_DEF_LINE_RE = re.compile(
    r"^\s*(?:function\s+[A-Za-z_][A-Za-z0-9_-]*(?:\s*\(\s*\))?"
    r"|[A-Za-z_][A-Za-z0-9_-]*\s*\(\s*\))\s*(?:[\{(]|$)"
)
_RUST_IMPL_HEADER_RE = re.compile(
    r"\bimpl\b(?:\s*<[^>{}]*>)?\s+(?:[A-Za-z_][\w:<>'&,\s+]*?\bfor\s+)?([A-Za-z_]\w*)"
)
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
        "typeof",
        "sizeof",
        "await",
        "yield",
        "co_await",
        "co_return",
        "co_yield",
        "assert",
        "raise",
        "go",
        "defer",
        "match",
        "elif",
        "unless",
        "until",
        "when",
        "export",
        "print",
        "puts",
        "echo",
        "not",
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
_C_CALL_BODY_ANCESTORS = frozenset(
    {
        "init_declarator",
        "initializer_list",
        "compound_statement",
        "return_statement",
        "expression_statement",
        "argument_list",
        "assignment_expression",
        "field_initializer",
    }
)
_C_DECL_SPEC_PREFIX_RE = re.compile(
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
CONTROL_KEYWORDS = frozenset(
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
_CONTROL_KEYWORDS = CONTROL_KEYWORDS
DEF_REGEXES = (
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
_DEF_REGEXES = DEF_REGEXES
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
    r"|(?:import|typedef)\b)"
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
    return any(token in lowered for token in ("truncated", "omitted", "capped", "budget", "limit"))


def _norm_rel_path(path: str) -> str:
    """Normalize a relative repository path to POSIX forward slashes."""
    return Path(path.replace("\\", "/")).as_posix()


def split_identifier_tokens(identifier: str) -> set[str]:
    """Split a snake_case, kebab-case, or camelCase identifier into search tokens.

    Preconditions:
        - ``identifier`` is any string (symbol name or prompt word).
    Postconditions:
        - Returns lowercase alphanumeric sub-tokens of length >= 3 excluding
          generic task stopwords.
    """
    if not identifier:
        return set()
    tokens: set[str] = set()
    for part in re.split(r"[^A-Za-z0-9]+", identifier):
        if not part:
            continue
        for sub in _CAMEL_TOKEN_RE.findall(part):
            low = sub.lower()
            if len(low) >= 3 and low not in _SUBTOKEN_STOPWORDS:
                tokens.add(low)
    return tokens


def select_target_definitions(
    defined_symbols: Iterable[str],
    task_words: set[str],
    *,
    max_fallback: int = 6,
) -> tuple[set[str], str]:
    """Select target definitions from an editable file using tiered lexical matching.

    Preconditions:
        - ``defined_symbols`` contains candidate symbol names defined in editable files.
        - ``task_words`` contains identifier words extracted from the task prompt.
    Postconditions:
        - Returns ``(selected_symbols, tier)`` where ``tier`` is one of
          ``"exact"``, ``"subtoken"``, ``"fallback"``, or ``"none"``.
        - Tier 1 returns exact word matches if non-empty.
        - Tier 2 ranks by camelCase/snake_case sub-token overlap (bounded by ``max_fallback``).
        - Tier 3 returns up to ``max_fallback`` deterministic exported (or non-test private)
          definitions when Tier 1 and Tier 2 are both empty.
    """
    all_defs = sorted({str(sym).strip() for sym in defined_symbols if str(sym).strip()})
    if not all_defs or not task_words or max_fallback <= 0:
        return set(), "none"

    exact = {sym for sym in all_defs if sym in task_words}
    if exact:
        return exact, "exact"

    task_tokens: set[str] = set()
    for word in task_words:
        task_tokens.update(split_identifier_tokens(word))

    if task_tokens:
        scored: list[tuple[int, str]] = []
        for sym in all_defs:
            overlap = len(split_identifier_tokens(sym) & task_tokens)
            if overlap > 0:
                scored.append((-overlap, sym))
        if scored:
            scored.sort()
            return {sym for _, sym in scored[:max_fallback]}, "subtoken"

    non_test = [sym for sym in all_defs if not sym.startswith("test_")]
    exported = [sym for sym in non_test if not sym.startswith("_")]
    chosen = exported[:max_fallback] if exported else non_test[:max_fallback]
    if chosen:
        return set(chosen), "fallback"
    return set(), "none"


def is_rust_trait_method_ambiguous(
    symbol: str,
    *,
    editable_impl_types: set[str],
    call_line: str,
    enclosing_impl_type: str | None = None,
) -> bool:
    """Return True if a Rust call to a homonymous trait method has an unprovable receiver.

    Preconditions:
        - ``symbol`` is the matched Rust method/function name.
        - ``editable_impl_types`` lists the concrete target type names from editable
          ``impl`` blocks defining ``symbol`` (may be empty if unknown).
    Postconditions:
        - Returns False for non-homonymous names or calls provably targeting one of
          ``editable_impl_types``; returns True otherwise.
    """
    if symbol not in _RUST_HOMONYMOUS_TRAIT_METHODS:
        return False
    if not editable_impl_types:
        return True
    escaped_sym = re.escape(symbol)
    for typ in editable_impl_types:
        escaped_typ = re.escape(typ)
        if re.search(rf"\b{escaped_typ}(?:<[^<>]*>)?\s*::\s*{escaped_sym}\s*\(", call_line):
            return False
        if re.search(
            rf"<\s*{escaped_typ}(?:<[^<>]*>)?\s+as\b[^>]*>\s*::\s*{escaped_sym}\s*\(",
            call_line,
        ):
            return False
    if enclosing_impl_type and enclosing_impl_type in editable_impl_types:
        if re.search(rf"\b(?:self\s*\.\s*|Self\s*::\s*){escaped_sym}\s*\(", call_line):
            return False
    return True


def _strip_bash_comments_and_literals(line: str) -> str:
    """Strip Bash comments and literal quoted text while preserving ``$(...)`` substitutions."""
    out: list[str] = []
    i = 0
    n = len(line)
    in_single = False
    in_double = False
    subst_depth = 0
    while i < n:
        ch = line[i]
        if in_single:
            if ch == "'":
                in_single = False
            out.append(" ")
            i += 1
            continue
        if in_double and subst_depth == 0:
            if ch == "\\" and i + 1 < n:
                out.append("  ")
                i += 2
                continue
            if ch == '"':
                in_double = False
                out.append(" ")
                i += 1
                continue
            if ch == "$" and i + 1 < n and line[i + 1] == "(":
                subst_depth = 1
                out.append("$(")
                i += 2
                continue
            out.append(" ")
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(line[i + 1])
            i += 2
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace() or line[i - 1] in ";|&({"):
            break
        if ch == "'":
            in_single = True
            out.append(" ")
            i += 1
            continue
        if ch == '"' and subst_depth == 0:
            in_double = True
            out.append(" ")
            i += 1
            continue
        if subst_depth > 0:
            if ch == "(":
                subst_depth += 1
            elif ch == ")":
                subst_depth -= 1
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _row_in_spans(row: int, spans: Sequence[tuple[int, int]]) -> bool:
    """Return True when ``row`` falls within any inclusive ``(start, end)`` span."""
    return any(start <= row <= end for start, end in spans)


def _extract_rust_base_type_name(type_text: str) -> str | None:
    """Extract the base Rust type identifier from a type expression like ``crate::Version<'a>``."""
    head = type_text.split("<", 1)[0].strip()
    if "::" in head:
        head = head.rsplit("::", 1)[-1].strip()
    head = head.lstrip("&").strip()
    if head.startswith("mut "):
        head = head[4:].strip()
    match = re.match(r"^([A-Za-z_]\w*)$", head)
    return match.group(1) if match else None


def _extract_rust_impl_target_types(source: str) -> dict[str, set[str]]:
    """Map Rust method names inside ``impl`` blocks to their concrete target type names."""
    result: dict[str, set[str]] = {}
    try:
        warnings.simplefilter("ignore", category=FutureWarning)
        from grep_ast.tsl import get_parser

        parser = get_parser("rust")
        tree = parser.parse(source.encode("utf-8"))
    except Exception:
        tree = None

    if tree is not None:
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "impl_item":
                type_node = node.child_by_field_name("type")
                type_name = (
                    _extract_rust_base_type_name(type_node.text.decode("utf-8", errors="ignore"))
                    if type_node is not None
                    else None
                )
                body = node.child_by_field_name("body")
                if type_name and body is not None:
                    for child in body.children:
                        if child.type == "function_item":
                            fn_name = child.child_by_field_name("name")
                            if fn_name is not None:
                                sym = fn_name.text.decode("utf-8", errors="ignore")
                                if sym:
                                    result.setdefault(sym, set()).add(type_name)
            stack.extend(reversed(node.children))
        return result

    # Regex fallback when tree-sitter Rust parser is unavailable.
    current_type: str | None = None
    brace_depth = 0
    impl_depth: int | None = None
    for line in source.splitlines():
        impl_match = _RUST_IMPL_HEADER_RE.search(line)
        if impl_match and impl_depth is None:
            current_type = impl_match.group(1)
            impl_depth = brace_depth + line.count("{") - line.count("}")
        elif current_type is not None:
            fn_match = re.search(r"\bfn\s+([A-Za-z_]\w*)\s*[(<]", line)
            if fn_match:
                result.setdefault(fn_match.group(1), set()).add(current_type)
        brace_depth += line.count("{") - line.count("}")
        if impl_depth is not None and brace_depth <= max(0, impl_depth - 1):
            current_type = None
            impl_depth = None
    return result


def _find_rust_enclosing_impl_type(source: str, row: int) -> str | None:
    """Return the target type name of the innermost Rust ``impl`` enclosing 0-based ``row``."""
    try:
        warnings.simplefilter("ignore", category=FutureWarning)
        from grep_ast.tsl import get_parser

        parser = get_parser("rust")
        tree = parser.parse(source.encode("utf-8"))
    except Exception:
        tree = None

    if tree is not None:
        best_type: str | None = None
        best_span = 1 << 30
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.start_point[0] <= row <= node.end_point[0]:
                if node.type == "impl_item":
                    span = node.end_point[0] - node.start_point[0]
                    if span <= best_span:
                        type_node = node.child_by_field_name("type")
                        if type_node is not None:
                            parsed = _extract_rust_base_type_name(
                                type_node.text.decode("utf-8", errors="ignore")
                            )
                            if parsed:
                                best_type = parsed
                                best_span = span
                stack.extend(reversed(node.children))
        return best_type

    lines = source.splitlines()
    for idx in range(min(row, len(lines) - 1), -1, -1):
        match = _RUST_IMPL_HEADER_RE.search(lines[idx])
        if match:
            return match.group(1)
    return None


def _compute_brace_definition_spans(
    source: str,
    symbols: set[str],
    *,
    is_bash: bool = False,
) -> dict[str, list[tuple[int, int]]]:
    """Compute approximate 0-based ``(start_row, end_row)`` body spans for regex fallback."""
    if not symbols:
        return {}
    spans: dict[str, list[tuple[int, int]]] = {}
    lines = source.splitlines()
    n = len(lines)
    for row, line in enumerate(lines):
        matched_sym: str | None = None
        if is_bash:
            for pattern in _BASH_DEF_REGEXES:
                m = pattern.search(line)
                if m and m.group(1) in symbols:
                    matched_sym = m.group(1)
                    break
        else:
            for pattern in DEF_REGEXES:
                m = pattern.search(line)
                if m and m.group(1) in symbols:
                    matched_sym = m.group(1)
                    break
            if matched_sym is None:
                for sym in symbols:
                    m = re.search(rf"\b{re.escape(sym)}\s*\(", line)
                    prev_line = lines[row - 1] if row > 0 else ""
                    if m and _is_c_cpp_declaration_match(line, m.start(), m.end(), prev_line):
                        if "{" in line or (row + 1 < n and "{" in lines[row + 1]):
                            matched_sym = sym
                            break
        if matched_sym is None:
            continue
        depth = 0
        opened = False
        end_row = row
        for cur in range(row, n):
            cur_line = lines[cur]
            opens = cur_line.count("{")
            closes = cur_line.count("}")
            if opens:
                opened = True
            depth += opens - closes
            end_row = cur
            if opened and depth <= 0:
                break
        spans.setdefault(matched_sym, []).append((row, end_row))
    return spans


def is_c_cpp_header(path: str) -> bool:
    """Return True for C/C++ header file extensions."""
    return Path(path).suffix.lower() in C_CPP_HEADER_SUFFIXES


_is_c_cpp_header = is_c_cpp_header


def _has_ancestor_type(node, node_type: str) -> bool:
    """Return True if any ancestor of ``node`` has tree-sitter type ``node_type``."""
    cur = getattr(node, "parent", None)
    while cur is not None:
        if getattr(cur, "type", "") == node_type:
            return True
        cur = getattr(cur, "parent", None)
    return False


def has_function_definition_declarator_ancestor(node) -> bool:
    """Return True when ``node`` belongs to a C/C++ ``function_definition`` declarator.

    Walks upward through declarator wrappers (such as ``pointer_declarator`` or
    ``reference_declarator``) and stops at declaration/body boundaries so block-scoped
    ``extern`` declarations inside a function body are not mistaken for definitions.
    """
    cur = getattr(node, "parent", None)
    while cur is not None:
        cur_type = getattr(cur, "type", "")
        if cur_type == "function_definition":
            return True
        if cur_type in _C_DECLARATION_BOUNDARY_NODES:
            return False
        cur = getattr(cur, "parent", None)
    return False


_has_function_definition_declarator_ancestor = has_function_definition_declarator_ancestor


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
    skipped_large: int = 0

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
        if self.skipped_large:
            parts.append(f"skipped_large={self.skipped_large}")
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
    skipped_large: int = 0
    _skipped_large_paths: set[str] = field(default_factory=set, repr=False)

    def record_definition(self, path: str, symbol: str) -> None:
        self.definitions.add((path, symbol))

    def record_source(self, source: str) -> None:
        text = str(source).strip()
        if text and text not in self.sources:
            self.sources.append(text)

    def record_skipped_large(self, path: str = "", *, reason: str | None = None) -> None:
        text = str(path).strip()
        if text:
            if text in self._skipped_large_paths:
                return
            self._skipped_large_paths.add(text)
        self.skipped_large += 1
        self.record_truncation(_LARGE_FILE_LIMITATION if reason is None else reason)

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
        evidence_items: list[CallerEvidence] = []
        for e in excerpt_tuple:
            if isinstance(e, CallerExcerpt):
                evidence_items.append(e.to_evidence(scope=default_scope))
            else:
                lims = tuple(getattr(e, "limitations", ()))
                if not lims and getattr(e, "incomplete", False):
                    lims = ("incomplete",)
                evidence_items.append(
                    CallerEvidence(
                        path=getattr(e, "path", ""),
                        line=getattr(e, "line", 0) or getattr(e, "start_line", 1),
                        symbol=getattr(e, "symbol", ""),
                        kind=getattr(e, "kind", "call"),
                        source=getattr(e, "source", "builtin"),
                        scope=getattr(e, "scope", "") or default_scope,
                        ambiguity=tuple(getattr(e, "ambiguity", ())),
                        truncation=tuple(getattr(e, "truncation", ())),
                        limitations=lims,
                        start_line=getattr(e, "start_line", 0),
                        code=getattr(e, "code", ""),
                    )
                )
        evidence_tuple = tuple(evidence_items)

        skipped_large = max(self.skipped_large, self.scope.skipped_large)
        base_truncations = (
            *self.truncation,
            *(reason for e in evidence_tuple for reason in e.truncation),
        )
        has_large_trunc = any(
            "scan limit" in note.lower() or "large" in note.lower()
            for note in base_truncations
        )
        extra_large = (
            (_LARGE_FILE_LIMITATION,) if (skipped_large > 0 and not has_large_trunc) else ()
        )
        ambiguity = _dedupe_str(
            (*self.ambiguity, *(reason for e in evidence_tuple for reason in e.ambiguity))
        )
        truncation = _dedupe_str((*base_truncations, *extra_large))
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
        scope = (
            replace(self.scope, sources=sources, skipped_large=skipped_large)
            if sources != self.scope.sources or skipped_large != self.scope.skipped_large
            else self.scope
        )

        if not evidence_tuple:
            has_impairment = (
                skipped_large > 0 or bool(truncation) or bool(ambiguity) or bool(limitations)
            )
            status: EvidenceStatus = "INCOMPLETE" if has_impairment else "NO_EVIDENCE"
        elif ambiguity or truncation or limitations or skipped_large > 0:
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


def _read_source_checked(
    root: Path | str,
    name: str,
    *,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> tuple[str, bool]:
    """Safely read a bounded UTF-8 source file inside ``root``.

    Returns ``(text, skipped_large)`` where ``skipped_large`` is True only when
    an otherwise valid file exceeded ``max_bytes``.
    """
    if not name or max_bytes <= 0:
        return "", False
    try:
        resolved_root = Path(root).resolve()
        path = resolved_root / name
        if (
            path.is_symlink()
            or path.resolve() != path
            or not path.is_relative_to(resolved_root)
            or not path.is_file()
        ):
            return "", False
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if b"\x00" in data:
            return "", False
        if len(data) > max_bytes:
            return "", True
        return data.decode("utf-8"), False
    except (OSError, UnicodeError, ValueError):
        return "", False


def _read_python_checked(root: Path | str, name: str) -> tuple[str, bool]:
    """Bounded safe reader restricted to ``.py`` files, reporting oversized skips."""
    if Path(name).suffix != ".py":
        return "", False
    return _read_source_checked(root, name)


def read_source(root: Path | str, name: str, *, max_bytes: int = MAX_SOURCE_BYTES) -> str:
    """Safely read a bounded UTF-8 source file inside ``root``.

    Preconditions:
        - ``name`` must resolve to a regular non-symlink file inside ``root.resolve()``.
        - File size must not exceed ``max_bytes`` (default 131072 bytes).
        - File contents must be valid UTF-8.
    Postcondition:
        - Returns decoded text on success, or ``""`` if any guard fails.
    """
    text, _ = _read_source_checked(root, name, max_bytes=max_bytes)
    return text


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
        editable_norm: set[str] = set()
        own_def_spans_by_file: dict[str, dict[tuple[str, str], list[tuple[int, int]]]] = {}

        # Collect all definitions first so tiered target selection works for vague prompts.
        # Entries: (name, norm_name, module, kind, class_name, sym, start_line, end_line)
        extracted_defs: list[tuple[str, str, str, str, str, str, int, int]] = []
        skipped_large = 0
        for name in editable:
            if not self.supports(name):
                continue
            norm_ed = _norm_rel_path(name)
            editable_norm.add(norm_ed)
            source, is_large = _read_python_checked(resolved_root, name)
            if is_large:
                skipped_large += 1
                if report is not None and hasattr(report, "record_skipped_large"):
                    report.record_skipped_large(name)
                continue
            if not source:
                continue
            try:
                definitions = ast.parse(source).body
            except (SyntaxError, ValueError):
                continue
            module = _python_module_name(name)
            for node in definitions:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    extracted_defs.append(
                        (
                            name,
                            norm_ed,
                            module,
                            "direct_call",
                            "",
                            node.name,
                            node.lineno,
                            node.end_lineno or node.lineno,
                        )
                    )
                elif isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            extracted_defs.append(
                                (
                                    name,
                                    norm_ed,
                                    module,
                                    "method_call",
                                    node.name,
                                    item.name,
                                    item.lineno,
                                    item.end_lineno or item.lineno,
                                )
                            )

        selected_names, _tier = select_target_definitions(
            (entry[5] for entry in extracted_defs), words
        )
        if not selected_names:
            return ()

        for name, norm_ed, module, def_kind, cls_name, sym, start_line, end_line in extracted_defs:
            if sym not in selected_names:
                continue
            if report is not None:
                report.record_definition(name, sym)
            if def_kind == "direct_call":
                target_funcs.add((module, sym))
            else:
                target_methods.add((module, cls_name, sym))
            target_modules.add(module)
            symbol_def_count[sym] = symbol_def_count.get(sym, 0) + 1
            own_def_spans_by_file.setdefault(norm_ed, {}).setdefault((def_kind, sym), []).append(
                (start_line, end_line)
            )

        if not target_funcs and not target_methods:
            return ()

        if report is not None and any(count > 1 for count in symbol_def_count.values()):
            report.record_ambiguity("duplicate symbol definition")

        target_symbols = {sym for _, sym in target_funcs} | {m for _, _, m in target_methods}
        excerpts: list[CallerExcerpt] = []

        ordered_candidates = sorted(
            candidates,
            key=lambda item: (1 if _norm_rel_path(item) in editable_norm else 0, item),
        )
        for name in ordered_candidates:
            if not self.supports(name):
                continue
            source, is_large = _read_python_checked(resolved_root, name)
            if is_large:
                skipped_large += 1
                if report is not None and hasattr(report, "record_skipped_large"):
                    report.record_skipped_large(name)
                continue
            if not source or not any(symbol in source for symbol in target_symbols):
                continue
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                continue

            norm_cand = _norm_rel_path(name)
            is_same_file = norm_cand in editable_norm
            cand_mod = _python_module_name(name)

            func_aliases: dict[str, str] = {}
            module_aliases: dict[str, str] = {}
            unrelated_module_aliases: set[str] = set()
            allowed_method_names: set[str] = set()

            if is_same_file:
                for mod, sym in target_funcs:
                    if mod == cand_mod:
                        func_aliases[sym] = sym
                for mod, _, meth in target_methods:
                    if mod == cand_mod:
                        allowed_method_names.add(meth)

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
                excluded_spans=own_def_spans_by_file.get(norm_cand),
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

        if skipped_large > 0 and excerpts:
            excerpts[-1] = replace(
                excerpts[-1],
                truncation=_dedupe_str((*excerpts[-1].truncation, _LARGE_FILE_LIMITATION)),
            )

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
        excluded_spans: dict[tuple[str, str], list[tuple[int, int]]] | None = None,
    ) -> list[tuple[ast.Call, str, str]]:
        spans = excluded_spans or {}
        matches: list[tuple[int, int, ast.Call, str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in func_aliases:
                sym = func_aliases[node.func.id]
                if not _row_in_spans(node.lineno, spans.get(("direct_call", sym), ())):
                    matches.append((node.lineno, node.col_offset, node, sym, "direct_call"))
                continue
            if isinstance(node.func, ast.Attribute):
                owner = _dotted_name(node.func.value)
                attr = node.func.attr
                if owner in module_aliases and (module_aliases[owner], attr) in target_funcs:
                    if not _row_in_spans(node.lineno, spans.get(("direct_call", attr), ())):
                        matches.append((node.lineno, node.col_offset, node, attr, "direct_call"))
                    continue
                if attr in allowed_method_names and owner not in unrelated_module_aliases:
                    if not _row_in_spans(node.lineno, spans.get(("method_call", attr), ())):
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


def resolve_lang(path: str, source: str | None = None) -> str | None:
    """Resolve tree-sitter language name from file path suffix or shell shebang.

    Preconditions:
        - ``path`` is a relative or absolute filename.
        - ``source`` is optional file text inspected only when the path has no mapped language.
    Postconditions:
        - Returns a tree-sitter language name (e.g. ``"bash"``, ``"rust"``, ``"c"``) or ``None``.
    """
    try:
        from grep_ast import filename_to_lang

        lang = filename_to_lang(path)
        if lang:
            return lang
    except Exception:
        pass
    by_suffix = _EXTRA_LANG_BY_SUFFIX.get(Path(path).suffix.lower())
    if by_suffix:
        return by_suffix
    if source:
        first_line = source.splitlines()[0] if source.splitlines() else ""
        if _BASH_SHEBANG_RE.match(first_line):
            return "bash"
    return None


_resolve_lang = resolve_lang


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


def _is_c_declaration_node(node) -> bool:
    """Return True when a tree-sitter node sits inside a C/C++ prototype/declaration."""
    cur = node
    while cur is not None:
        node_type = getattr(cur, "type", "")
        if node_type == "function_declarator":
            return True
        if node_type in _C_CALL_BODY_ANCESTORS:
            return False
        parent = getattr(cur, "parent", None)
        if parent is not None and getattr(parent, "type", "") == "field_declaration":
            default_val = parent.child_by_field_name("default_value")
            if default_val is not None and default_val == cur:
                return False
        if node_type in ("declaration", "field_declaration"):
            return True
        cur = parent
    return False


def _is_c_cpp_declaration_match(
    line: str,
    match_start: int,
    match_end: int,
    prev_line: str = "",
) -> bool:
    """Return True if a regex ``name(`` hit is a C/C++ prototype or definition header."""
    before = line[:match_start]
    same_line_stmt_reset = "{" in before or ";" in before
    stmt_before = before.rsplit("{", 1)[-1].rsplit(";", 1)[-1].lstrip()
    stripped_before = stmt_before.strip()

    if stripped_before:
        if any(ch in stripped_before for ch in ("=", ",", "?", "(", ")", "[", "]", ".", "->")):
            return False
        if _C_DECL_SPEC_PREFIX_RE.match(stmt_before):
            first_word_match = _WORD_RE.search(stripped_before)
            first_word = first_word_match.group(0) if first_word_match else ""
            if first_word and first_word not in _C_NON_DECL_KEYWORDS:
                return True
        return False

    after_paren = line[match_end:]
    if _C_VOID_PARAMS_RE.match(after_paren):
        return True
    first_param = _C_FIRST_PARAM_WORD_RE.match(after_paren)
    if first_param and first_param.group(1) in _C_PARAM_TYPE_KEYWORDS:
        return True

    if not same_line_stmt_reset and prev_line:
        prev_stripped = prev_line.strip()
        if prev_stripped and _C_RETURN_TYPE_LINE_RE.match(prev_stripped):
            prev_word_match = _WORD_RE.search(prev_stripped)
            prev_word = prev_word_match.group(0) if prev_word_match else ""
            if prev_word and prev_word not in _C_NON_DECL_KEYWORDS:
                return True
    return False


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
        skipped_large = 0
        editable_norm: set[str] = set()
        per_file_defs: list[tuple[str, str, str, set[str]]] = []
        for name in editable:
            if not self.supports(name):
                continue
            norm_ed = _norm_rel_path(name)
            editable_norm.add(norm_ed)
            source, is_large = _read_source_checked(resolved_root, name)
            if is_large:
                skipped_large += 1
                if report is not None and hasattr(report, "record_skipped_large"):
                    report.record_skipped_large(name)
                continue
            if not source:
                continue
            defined = self._extract_definitions(name, source)
            if defined:
                per_file_defs.append((name, norm_ed, source, defined))

        all_defined: set[str] = set()
        for _, _, _, defined in per_file_defs:
            all_defined.update(defined)

        selected_names, _tier = select_target_definitions(all_defined, words)
        if not selected_names:
            return ()

        targets: dict[str, set[str]] = {}
        targets_norm: dict[str, set[str]] = {}
        own_def_spans_by_file: dict[str, dict[str, list[tuple[int, int]]]] = {}
        rust_editable_symbols: set[str] = set()
        rust_impl_types: dict[str, set[str]] = {}

        for name, norm_ed, source, defined in per_file_defs:
            chosen = defined & selected_names
            if not chosen:
                continue
            for sym in sorted(chosen):
                if report is not None:
                    report.record_definition(name, sym)
                targets.setdefault(sym, set()).add(name)
                targets_norm.setdefault(sym, set()).add(norm_ed)
            spans = self._extract_definition_spans(name, source, chosen)
            if spans:
                dest = own_def_spans_by_file.setdefault(norm_ed, {})
                for sym, sym_spans in spans.items():
                    dest.setdefault(sym, []).extend(sym_spans)
            if resolve_lang(name, source) == "rust":
                rust_editable_symbols.update(chosen)
                extracted_impls = _extract_rust_impl_target_types(source)
                for sym in chosen:
                    if sym in extracted_impls:
                        rust_impl_types.setdefault(sym, set()).update(extracted_impls[sym])

        if not targets:
            return ()

        if report is not None and any(len(paths) > 1 for paths in targets.values()):
            report.record_ambiguity("duplicate symbol definition")

        excerpts: list[CallerExcerpt] = []
        ordered_candidates = sorted(
            candidates,
            key=lambda item: (1 if _norm_rel_path(item) in editable_norm else 0, item),
        )
        for name in ordered_candidates:
            if not self.supports(name):
                continue
            source, is_large = _read_source_checked(resolved_root, name)
            if is_large:
                skipped_large += 1
                if report is not None and hasattr(report, "record_skipped_large"):
                    report.record_skipped_large(name)
                continue
            if not source:
                continue

            norm_cand = _norm_rel_path(name)
            call_names, ns_aliases = self._resolve_candidate_bindings(name, source, targets)
            if not call_names and not ns_aliases:
                continue

            search_tokens = set(call_names) | {
                sym for syms in ns_aliases.values() for sym in syms
            }
            if not any(tok in source for tok in search_tokens):
                continue

            own_spans = own_def_spans_by_file.get(norm_cand)
            matches, kind = self._find_call_lines(
                name, source, call_names, ns_aliases, own_def_spans=own_spans
            )
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
                is_same_file_target = norm_cand in targets_norm.get(symbol, set())
                named, namespaces = _parse_js_imports(source)
                specifiers = [spec for imported, _, spec in named if imported == symbol]
                specifiers.extend(spec for _, spec in namespaces)
                resolved = is_same_file_target or any(
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

            if (
                resolve_lang(name, source) == "rust"
                and symbol in rust_editable_symbols
                and symbol in _RUST_HOMONYMOUS_TRAIT_METHODS
            ):
                enclosing_impl = _find_rust_enclosing_impl_type(source, lineno)
                call_line = lines[lineno] if 0 <= lineno < len(lines) else ""
                if is_rust_trait_method_ambiguous(
                    symbol,
                    editable_impl_types=rust_impl_types.get(symbol, set()),
                    call_line=call_line,
                    enclosing_impl_type=enclosing_impl,
                ):
                    ambiguity.append("ambiguous trait method receiver")

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

        if skipped_large > 0 and excerpts:
            excerpts[-1] = replace(
                excerpts[-1],
                truncation=_dedupe_str((*excerpts[-1].truncation, _LARGE_FILE_LIMITATION)),
            )

        return tuple(excerpts)

    def _extract_definitions(self, path: str, source: str) -> set[str]:
        defined: set[str] = set()
        lang = resolve_lang(path, source)
        if lang:
            defined.update(self._extract_ts_definitions(lang, source, path=path))
        if lang == "bash":
            for pattern in _BASH_DEF_REGEXES:
                for match in pattern.finditer(source):
                    name = match.group(1)
                    if name and name not in CONTROL_KEYWORDS:
                        defined.add(name)
            return defined
        # Augment with regex fallback (handles arrow functions in TS and unsupported languages).
        for pattern in DEF_REGEXES:
            for match in pattern.finditer(source):
                name = match.group(1)
                if name and name not in CONTROL_KEYWORDS:
                    defined.add(name)
        return defined

    def _extract_definition_spans(
        self,
        path: str,
        source: str,
        symbols: set[str],
    ) -> dict[str, list[tuple[int, int]]]:
        """Return 0-based inclusive ``(start_row, end_row)`` spans of definitions for ``symbols``."""
        if not symbols:
            return {}
        lang = resolve_lang(path, source)
        spans: dict[str, list[tuple[int, int]]] = {}
        if lang:
            spans = self._extract_ts_definition_spans(lang, source, symbols, path=path)
        if not spans:
            spans = _compute_brace_definition_spans(source, symbols, is_bash=(lang == "bash"))
        return spans

    def _extract_ts_definition_spans(
        self,
        lang: str,
        source: str,
        symbols: set[str],
        *,
        path: str = "",
    ) -> dict[str, list[tuple[int, int]]]:
        try:
            warnings.simplefilter("ignore", category=FutureWarning)
            from grep_ast.tsl import get_language, get_parser
            from tree_sitter import Query

            language = get_language(lang)
            parser = get_parser(lang)
            tree = parser.parse(source.encode("utf-8"))
        except Exception:
            return {}

        spans: dict[str, list[tuple[int, int]]] = {}
        scm_path = _get_scm_path(lang)
        is_c_cpp_non_header = (
            lang in ("c", "cpp") and bool(path) and not is_c_cpp_header(path)
        )
        if scm_path is not None:
            try:
                query = Query(language, scm_path.read_text(encoding="utf-8"))
                captures = _run_ts_captures(query, tree.root_node)
            except Exception:
                captures = []
            for node, tag in captures:
                if not tag.startswith("name.definition."):
                    continue
                if lang == "bash" and tag != "name.definition.function":
                    continue
                if is_c_cpp_non_header and tag in (
                    "name.definition.function",
                    "name.definition.method",
                ):
                    if not has_function_definition_declarator_ancestor(node):
                        continue
                sym = node.text.decode("utf-8", errors="ignore")
                if sym not in symbols:
                    continue
                cur = node
                container = None
                while cur is not None:
                    if getattr(cur, "type", "") in _DEF_CONTAINER_NODE_TYPES:
                        container = cur
                        break
                    cur = getattr(cur, "parent", None)
                if container is not None:
                    spans.setdefault(sym, []).append(
                        (container.start_point[0], container.end_point[0])
                    )

        # Fallback AST scan for arrow functions / function definitions not covered by scm.
        missing = symbols - set(spans)
        if missing:
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                if node.type in _DEF_CONTAINER_NODE_TYPES:
                    name_node = node.child_by_field_name("name")
                    if name_node is None and node.type == "function_definition":
                        decl = node.child_by_field_name("declarator")
                        while decl is not None and decl.type not in _IDENT_NODE_TYPES:
                            inner = decl.child_by_field_name("declarator")
                            if inner is None:
                                break
                            decl = inner
                        name_node = decl
                    if name_node is not None:
                        sym = name_node.text.decode("utf-8", errors="ignore")
                        if sym in missing:
                            spans.setdefault(sym, []).append(
                                (node.start_point[0], node.end_point[0])
                            )
                stack.extend(reversed(node.children))
        return spans

    def _extract_ts_definitions(self, lang: str, source: str, *, path: str = "") -> set[str]:
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

        is_c_cpp_non_header = (
            lang in ("c", "cpp") and bool(path) and not is_c_cpp_header(path)
        )
        is_cpp_header = lang == "cpp" and bool(path) and is_c_cpp_header(path)
        defined: set[str] = set()
        for node, tag in captures:
            if tag.startswith("name.definition."):
                if lang == "bash" and tag != "name.definition.function":
                    continue
                if is_c_cpp_non_header and tag in (
                    "name.definition.function",
                    "name.definition.method",
                ):
                    if not has_function_definition_declarator_ancestor(node):
                        continue
                elif (
                    is_cpp_header
                    and tag in ("name.definition.function", "name.definition.method")
                    and not has_function_definition_declarator_ancestor(node)
                    and _has_ancestor_type(node, "compound_statement")
                ):
                    continue
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

        norm_cand = _norm_rel_path(candidate_path)
        named_imports, ns_imports = _parse_js_imports(source)
        if not named_imports and not ns_imports:
            return {sym: sym for sym in targets}, {}

        call_names: dict[str, str] = {}
        ns_aliases: dict[str, set[str]] = {}
        imported_from_matching: set[str] = set()
        imported_from_other: set[str] = set()

        for sym, editable_paths in targets.items():
            if any(_norm_rel_path(ed) == norm_cand for ed in editable_paths):
                call_names[sym] = sym
                imported_from_matching.add(sym)

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
        *,
        own_def_spans: dict[str, list[tuple[int, int]]] | None = None,
    ) -> tuple[list[tuple[int, str]], str]:
        lang = resolve_lang(path, source)
        if lang:
            matches = self._find_ts_call_lines(
                lang, source, call_names, ns_aliases, own_def_spans=own_def_spans
            )
            if matches:
                return matches, "direct_call"
        if lang == "bash":
            return (
                self._find_bash_regex_call_lines(
                    source, call_names, own_def_spans=own_def_spans
                ),
                "regex_call",
            )
        return (
            self._find_regex_call_lines(
                source, call_names, ns_aliases, own_def_spans=own_def_spans
            ),
            "regex_call",
        )

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
        *,
        own_def_spans: dict[str, list[tuple[int, int]]] | None = None,
    ) -> list[tuple[int, str]]:
        spans = own_def_spans or {}
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
                if _is_c_declaration_node(node):
                    continue
                called = node.text.decode("utf-8", errors="ignore")
                row, col = node.start_point
                if called in call_names:
                    sym = call_names[called]
                    if not _row_in_spans(row, spans.get(sym, ())):
                        matches.append((row, col, sym))
                    continue
                if ns_aliases and node.parent is not None:
                    owner_node = node.parent.child_by_field_name("object")
                    owner = (
                        owner_node.text.decode("utf-8", errors="ignore")
                        if owner_node is not None
                        else None
                    )
                    if owner in ns_aliases and called in ns_aliases[owner]:
                        if not _row_in_spans(row, spans.get(called, ())):
                            matches.append((row, col, called))
            if matches:
                matches.sort(key=lambda item: (item[0], item[1]))
                return [(row, sym) for row, _, sym in matches]

        # Path 2: Tree-sitter AST walk for call nodes (e.g. TypeScript, Java, C#, Rust, Bash).
        ast_matches: list[tuple[int, int, str]] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "function_declarator":
                continue
            if lang == "bash" and node.type == "command":
                cmd_name = node.child_by_field_name("name")
                if cmd_name is not None:
                    called = cmd_name.text.decode("utf-8", errors="ignore")
                    row, col = cmd_name.start_point
                    if called in call_names:
                        sym = call_names[called]
                        if not _row_in_spans(row, spans.get(sym, ())):
                            ast_matches.append((row, col, sym))
            elif node.type in _CALL_NODE_TYPES:
                if not _is_c_declaration_node(node):
                    owner, called = _extract_callee_name(node)
                    row, col = node.start_point
                    if called and called in call_names:
                        sym = call_names[called]
                        if not _row_in_spans(row, spans.get(sym, ())):
                            ast_matches.append((row, col, sym))
                    elif owner and called and owner in ns_aliases and called in ns_aliases[owner]:
                        if not _row_in_spans(row, spans.get(called, ())):
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
    def _find_bash_regex_call_lines(
        source: str,
        call_names: dict[str, str],
        *,
        own_def_spans: dict[str, list[tuple[int, int]]] | None = None,
    ) -> list[tuple[int, str]]:
        """Regex fallback for Bash command calls skipping comments, definitions, assignments, and literals."""
        if not call_names:
            return []
        spans = own_def_spans or {}
        patterns: list[tuple[re.Pattern[str], str]] = [
            (
                re.compile(
                    r"(?:^|[;|&(`{]|!|\b(?:if|elif|then|else|do|while|until|time|command|builtin)\b)"
                    r"\s*(?:\$\(\s*)?"
                    rf"({re.escape(local)})"
                    r"(?=$|[\s;|&)`<>])"
                ),
                sym,
            )
            for local, sym in sorted(call_names.items())
        ]
        matches: list[tuple[int, str]] = []
        for row, raw_line in enumerate(source.splitlines()):
            if _BASH_DEF_LINE_RE.match(raw_line):
                continue
            cleaned = _strip_bash_comments_and_literals(raw_line)
            if not cleaned.strip():
                continue
            for pattern, sym in patterns:
                if _row_in_spans(row, spans.get(sym, ())):
                    continue
                if pattern.search(cleaned):
                    matches.append((row, sym))
                    break
        return matches

    @staticmethod
    def _find_regex_call_lines(
        source: str,
        call_names: dict[str, str],
        ns_aliases: dict[str, set[str]],
        *,
        own_def_spans: dict[str, list[tuple[int, int]]] | None = None,
    ) -> list[tuple[int, str]]:
        spans = own_def_spans or {}
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
        prev_nonempty = ""
        for row, line in enumerate(source.splitlines()):
            stripped = line.strip()
            if _DECL_LINE_RE.match(line):
                if stripped and not stripped.startswith(("//", "/*", "*", "#")):
                    prev_nonempty = stripped
                continue
            matched_line = False
            for pattern, sym in patterns:
                if _row_in_spans(row, spans.get(sym, ())):
                    continue
                for match in pattern.finditer(line):
                    if _is_c_cpp_declaration_match(
                        line, match.start(), match.end(), prev_nonempty
                    ):
                        continue
                    matches.append((row, sym))
                    matched_line = True
                    break
                if matched_line:
                    break
            if stripped:
                prev_nonempty = stripped
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


def is_test_path(path: str) -> bool:
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


_is_test_path = is_test_path


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
        editable_norm_list = sorted({_norm_rel_path(p) for p in editable_tuple if p})
        editable_norm = set(editable_norm_list)

        seen_candidates: set[str] = set()
        unique_candidates_list: list[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            norm = _norm_rel_path(candidate)
            if norm not in seen_candidates:
                seen_candidates.add(norm)
                unique_candidates_list.append(norm)
        unique_candidates = tuple(sorted(unique_candidates_list))
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
        total_candidates = (
            len(unique_candidates) if unique_candidates else len(implicit_same_file)
        )
        prod_candidate_count = (
            len(prod_candidates) if unique_candidates else len(implicit_same_file)
        )

        if max_files <= 0 or max_chars <= 0:
            active_report.record_truncation("budget cap zero")
            active_report.scope = CallerScope(
                pool="none",
                editable=editable_tuple,
                candidates_total=total_candidates,
                candidates_searched=0,
                production_candidates=prod_candidate_count,
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
                candidates_total=total_candidates,
                candidates_searched=0,
                production_candidates=prod_candidate_count,
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

        # Prefer real production callers (cross-file first, then same-file); only fall back
        # to test/smoke files when no production caller matches.
        named_pools: list[tuple[ScopePool, tuple[str, ...], int]] = []
        if prod_search_pool:
            counted = len(prod_candidates) if unique_candidates else len(implicit_same_file)
            named_pools.append(("production", prod_search_pool, counted))
        if test_candidates:
            named_pools.append(("tests", test_candidates, len(test_candidates)))
        if not named_pools:
            named_pools.append(("none", (), 0))

        searched_count = 0
        for pool_name, pool, pool_counted in named_pools:
            searched_count += pool_counted
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
                    by_key.values(),
                    key=lambda item: (
                        1 if _norm_rel_path(item.path) in editable_norm else 0,
                        item.path,
                        item.line,
                        item.symbol,
                    ),
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
                    candidates_total=total_candidates,
                    candidates_searched=searched_count,
                    production_candidates=prod_candidate_count,
                    test_candidates=len(test_candidates),
                    omitted_test_candidates=omitted_tests,
                    matched_files=len(distinct_paths),
                    selected_files=len(allowed_paths),
                    max_files=max_files,
                    max_chars=max_chars,
                    context_lines=context_lines,
                    sources=tuple(active_report.sources),
                    skipped_large=active_report.skipped_large,
                )
                result = active_report.to_result(tuple(selected))
                active_report.result = result
                return result

        if prod_candidates and test_candidates:
            final_pool: ScopePool = "production+tests"
        elif prod_candidates or implicit_same_file:
            final_pool = "production"
        elif test_candidates:
            final_pool = "tests"
        else:
            final_pool = "none"

        active_report.scope = CallerScope(
            pool=final_pool,
            editable=editable_tuple,
            candidates_total=total_candidates,
            candidates_searched=searched_count,
            production_candidates=prod_candidate_count,
            test_candidates=len(test_candidates),
            omitted_test_candidates=0,
            matched_files=0,
            selected_files=0,
            max_files=max_files,
            max_chars=max_chars,
            context_lines=context_lines,
            sources=tuple(active_report.sources),
            skipped_large=active_report.skipped_large,
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
