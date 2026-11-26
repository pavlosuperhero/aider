"""Contract-first response hook and isolated CoVe caller-contract auditor.

Pure policy module (Clean Architecture): imports only stdlib and
aider.hooks.ResponseHookResult. Has no dependency on Coder, litellm,
tree_sitter, git, or aider.local_callers.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Callable, Iterable, Literal, Protocol

from aider.hooks import ResponseHookResult

_VERDICT_RE = re.compile(
    r"(?:(?:\d+[.)]|[-*])\s+)?(\*{0,2})VERDICT:[ \t]*(PASS|FAIL|UNKNOWN)\1(?::[ \t]*([^\n]+))?",
    re.I,
)
_VERDICT_DECLARATION = re.compile(r"^\s*(?:(?:\d+[.)]|[-*])\s+)?\**VERDICT\s*:", re.I)
_MAX_PATCH_CHARS = 16000
_CONTRACT_RE = re.compile(r"\bCALLER_CONTRACT\s*:", re.IGNORECASE)
_TRACE_RE = re.compile(r"\bTRACE\s*:", re.IGNORECASE)
_MAX_REASON_CHARS = 600
_MAX_SUPPLEMENTARY_ITEMS = 8
_MAX_SUPPLEMENTARY_ITEM_CHARS = 600
_MAX_SUPPLEMENTARY_TOTAL_CHARS = 2400

_AUDITOR_SYSTEM_PROMPT = """\
You are an independent caller-contract verifier. You receive ONLY the task \
summary, the caller snippets, optional supplementary evidence, and the proposed \
diff (with no prior conversation history).

Evaluate strictly whether the proposed diff satisfies every caller's \
contract. Judge only against the task and the attached caller snippets; \
supplementary evidence (such as project facts or read-range excerpts) is \
auxiliary context only and never replaces missing or incomplete caller snippets.

Evidence sufficiency rule (calibration):
- Emit `VERDICT: PASS` ONLY when every requirement of the task is either \
explicitly specified in the task or directly verified by attached caller \
excerpts that show the relevant arguments and explicitly check, unpack, or \
branch on the exact return value, sentinel, formatting rule, unit, side \
effect, or error condition affected by the diff, and your trace verifies every \
observed caller expectation.
- `VERDICT: UNKNOWN: <reason>` is REQUIRED whenever ANY of the following holds:
  (a) the task leaves an expected sentinel value, formatting rule, unit, or \
repository convention unspecified AND the attached caller snippets do not \
explicitly check, unpack, or branch on that exact convention (for example, a \
caller that only interpolates the return value or checks a broad condition \
like `< 0` without constraining the exact sentinel);
  (b) the only attached excerpts are minimal smoke tests or declarations that \
do not exercise the changed edge case;
  (c) the attached caller excerpts do not inspect the return value, error \
condition, or relevant arguments of the changed symbol (for example, a bare \
call with no arguments, a header declaration/prototype only, or a call site \
that omits how the changed behavior is consumed).
- Abstain with `VERDICT: UNKNOWN: <reason>` instead of guessing that an \
arbitrary default satisfies unshown callers to emit `PASS`.

Complete these 4 steps in order:
1. CALLER_EXPECTS: Quote each caller line and state the exact semantic role of \
every variable assigned or unpacked from the call, plus how downstream lines use \
it (element order, types, units, prefixes/formatting, and null/sentinel/error \
checks). Explicitly note whether the task specifies the exact convention or \
whether the snippet actually observes, asserts, or branches on the changed \
edge case, return value, error condition, or relevant arguments.
2. TRACE_REALISTIC: Pick 1 realistic, standard-format, asymmetric input matching \
the task and parameter names (avoid symmetric placeholders like 'A B' or \
[1, 1]) and 1 edge case. Trace the EXACT changed behavior in the proposed diff \
line by line to compute its literal return value and error/side-effect state, \
then substitute that result into the caller's exact variable names AND evaluate \
the caller's final output expression.
3. MISMATCH_CHECK: Verify that every variable bound in the caller actually holds \
what its variable name and downstream expression require (e.g., unpacked items \
are not swapped, numeric values use the caller's expected unit, types and \
null/sentinel/error checks match, and the caller's final output is valid). If \
the task leaves a sentinel, formatting rule, unit, or repository convention \
unspecified and the snippet does not explicitly check, unpack, or branch on \
that exact convention—or if only minimal smoke tests or declarations are \
attached without exercising the changed edge case—record that evidence is \
insufficient for PASS.
4. Output your verdict on a separate final line in plain text, without numbering or Markdown.
For a verified match where the task and caller snippets directly establish and \
observe every changed behavior and edge case output:
VERDICT: PASS
Whenever evidence is insufficient, a task convention is unspecified and \
unverified by callers, or attached excerpts are minimal smoke tests or do not \
inspect the affected return value, error condition, or relevant arguments output:
VERDICT: UNKNOWN: <reason>
ONLY if you proved a concrete input where a caller variable or caller result \
is wrong output:
VERDICT: FAIL: <input -> actual return -> wrong caller variable/result -> required fix>"""


class CallerExcerptLike(Protocol):
    path: str
    start_line: int
    symbol: str
    code: str


@dataclass(frozen=True)
class ProvenanceEvidence:
    """Bounded, provenance-labeled supplementary evidence for hooks and audits."""

    kind: str
    source: str
    content: str
    truncated: bool = False

    def format(self) -> str:
        suffix = " [TRUNCATED]" if self.truncated else ""
        header = f"[provenance={self.kind} source={self.source}]{suffix}"
        if self.content:
            return f"{header}\n{self.content}"
        return header


@dataclass(frozen=True)
class AuditVerdict:
    status: Literal["PASS", "FAIL", "UNKNOWN", "ERROR"]
    reason: str
    raw_response: str = ""

    @property
    def passed(self):
        return self.status == "PASS"


def _format_excerpt(excerpt: CallerExcerptLike) -> str:
    formatter = getattr(excerpt, "format", None)
    if callable(formatter):
        rendered = str(formatter())
    else:
        path = getattr(excerpt, "path", "")
        start_line = getattr(excerpt, "start_line", 1)
        code = getattr(excerpt, "code", "")
        rendered = f"{path}:{start_line}\n{code}"
    provenance = (
        getattr(excerpt, "provenance", None) or getattr(excerpt, "backend", None) or ""
    )
    provenance = str(provenance).strip()
    if (
        provenance
        and f"provenance={provenance}" not in rendered
        and f"[{provenance}]" not in rendered
    ):
        first, sep, rest = rendered.partition("\n")
        rendered = f"{first} [provenance={provenance}]{sep}{rest}"
    return rendered


def _coerce_evidence_item(item, *, default_kind: str) -> ProvenanceEvidence | None:
    if item is None:
        return None
    if isinstance(item, ProvenanceEvidence):
        kind = (item.kind or default_kind).strip() or default_kind
        source = (item.source or "unknown").strip() or "unknown"
        content = (item.content or "").strip()
        if not content:
            return None
        return ProvenanceEvidence(
            kind=kind,
            source=source,
            content=content,
            truncated=bool(item.truncated),
        )
    if isinstance(item, dict):
        kind = (
            str(item.get("kind") or item.get("provenance") or default_kind).strip()
            or default_kind
        )
        source = str(
            item.get("source")
            or item.get("path")
            or item.get("key")
            or item.get("id")
            or "unknown"
        ).strip()
        content = str(
            item.get("content")
            or item.get("statement")
            or item.get("value")
            or item.get("text")
            or item.get("code")
            or ""
        ).strip()
        truncated = bool(item.get("truncated", False))
        if not content:
            return None
        return ProvenanceEvidence(
            kind=kind,
            source=source or "unknown",
            content=content,
            truncated=truncated,
        )
    if isinstance(item, str):
        content = item.strip()
        if not content:
            return None
        return ProvenanceEvidence(kind=default_kind, source="inline", content=content)

    raw_kind = getattr(item, "kind", None)
    raw_prov = getattr(item, "provenance", None)
    kind = (
        str(raw_kind or (raw_prov if isinstance(raw_prov, str) else None) or default_kind).strip()
        or default_kind
    )
    path = getattr(item, "path", None)
    start_line = getattr(item, "start_line", None)
    end_line = getattr(item, "end_line", None)
    raw_source = getattr(item, "source", None)
    if path and start_line is not None and start_line > 0:
        line_span = f"{start_line}-{end_line}" if end_line is not None else str(start_line)
        loc = f"{path}:{line_span}"
        if (
            isinstance(raw_source, str)
            and raw_source.strip()
            and raw_source.strip() not in ("native", "builtin")
        ):
            source = raw_source.strip() if ":" in raw_source else f"{loc} [{raw_source.strip()}]"
        else:
            source = loc
    else:
        source = (
            str(
                raw_source
                or getattr(item, "fact_id", None)
                or getattr(item, "key", None)
                or getattr(item, "id", None)
                or path
                or "unknown"
            ).strip()
            or "unknown"
        )
    content = str(
        getattr(item, "content", None)
        or getattr(item, "statement", None)
        or getattr(item, "value", None)
        or getattr(item, "text", None)
        or getattr(item, "numbered_code", None)
        or getattr(item, "code", None)
        or ""
    ).strip()
    if not content:
        lines = getattr(item, "lines", None)
        if isinstance(lines, (list, tuple)):
            content = "\n".join(str(line) for line in lines).strip()
    truncated = bool(getattr(item, "truncated", False))
    if not content:
        return None
    return ProvenanceEvidence(kind=kind, source=source, content=content, truncated=truncated)


def normalize_supplementary_evidence(
    items: Iterable = (),
    *,
    default_kind: str = "evidence",
    max_items: int = _MAX_SUPPLEMENTARY_ITEMS,
    max_item_chars: int = _MAX_SUPPLEMENTARY_ITEM_CHARS,
    max_total_chars: int = _MAX_SUPPLEMENTARY_TOTAL_CHARS,
) -> tuple[ProvenanceEvidence, ...]:
    """Bound and label supplementary evidence with explicit provenance."""
    if max_items <= 0 or max_item_chars <= 0 or max_total_chars <= 0:
        return ()
    result: list[ProvenanceEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    used_chars = 0
    for raw in items or ():
        if len(result) >= max_items:
            break
        coerced = _coerce_evidence_item(raw, default_kind=default_kind)
        if coerced is None:
            continue
        content = coerced.content
        truncated = coerced.truncated
        if len(content) > max_item_chars:
            content = content[:max_item_chars].rstrip()
            truncated = True
        dedup_key = (coerced.kind, coerced.source, content)
        if dedup_key in seen:
            continue
        candidate = ProvenanceEvidence(
            kind=coerced.kind,
            source=coerced.source,
            content=content,
            truncated=truncated,
        )
        formatted = candidate.format()
        sep_chars = 2 if result else 0
        remaining = max_total_chars - used_chars - sep_chars
        if remaining <= 0:
            break
        if len(formatted) > remaining:
            header = f"[provenance={candidate.kind} source={candidate.source}] [TRUNCATED]\n"
            avail = remaining - len(header)
            if avail < 16:
                break
            candidate = ProvenanceEvidence(
                kind=candidate.kind,
                source=candidate.source,
                content=candidate.content[:avail].rstrip(),
                truncated=True,
            )
            formatted = candidate.format()
        seen.add(dedup_key)
        result.append(candidate)
        used_chars += sep_chars + len(formatted)
    return tuple(result)


def _combine_supplementary(
    supplementary_evidence: Iterable = (),
    facts: Iterable = (),
    read_ranges: Iterable = (),
) -> tuple[ProvenanceEvidence, ...]:
    combined: list[ProvenanceEvidence] = []
    combined.extend(
        normalize_supplementary_evidence(supplementary_evidence, default_kind="evidence")
    )
    combined.extend(normalize_supplementary_evidence(facts, default_kind="project-fact"))
    combined.extend(normalize_supplementary_evidence(read_ranges, default_kind="read-range"))
    return normalize_supplementary_evidence(combined)


def _excerpt_references(excerpts: Iterable[CallerExcerptLike]) -> list[str]:
    refs: list[str] = []
    for excerpt in excerpts:
        path = (getattr(excerpt, "path", "") or "").strip()
        symbol = (getattr(excerpt, "symbol", "") or "").strip()
        if path:
            refs.append(path)
            basename = PurePosixPath(path.replace("\\", "/")).name
            if basename and basename != path:
                refs.append(basename)
        if symbol:
            refs.append(symbol)
    return refs


def parse_audit_verdict(raw: str | None) -> AuditVerdict:
    """Require one final, unambiguous verdict line."""
    raw = raw or ""
    lines = raw.strip().splitlines()
    candidates = [line for line in lines if _VERDICT_DECLARATION.match(line)]
    match = _VERDICT_RE.fullmatch(lines[-1].strip()) if lines else None
    if len(candidates) != 1 or not match:
        return AuditVerdict("UNKNOWN", "Missing or malformed final verdict", raw[-2000:])
    _, status, reason = match.groups()
    status = status.upper()
    reason = (reason or "").strip()[:_MAX_REASON_CHARS]
    if status == "PASS" and reason:
        return AuditVerdict("UNKNOWN", "Qualified PASS verdict", raw[-2000:])
    return AuditVerdict(status, reason or status, raw[-2000:])


class CallerContractHook:
    """Check the format of the editor's explanation before applying a proposal."""

    _EDIT_MARKERS = {
        "patch": r"^\*\*\* (?:Begin Patch|Update File:|Add File:|Delete File:)",
        "udiff": r"^```diff[^\n]*\n--- [^\n]+\n\+\+\+ ",
        "whole": r"^(?:```|<(?:source|code|pre|codeblock|sourcecode)>)",
    }
    _SEARCH_MARKER = r"^<{5,9} SEARCH>?[ \t]*$"

    def __init__(
        self, *, enabled: bool = False, max_rejections: int = 2, advisory=False, edit_format="diff"
    ):
        self.advisory = advisory
        self.enabled = bool(enabled)
        self.max_rejections = max_rejections
        if edit_format == "editor-whole":
            edit_format = "whole"
        self._edit_marker = re.compile(
            self._EDIT_MARKERS.get(edit_format, self._SEARCH_MARKER), re.M
        )
        self._excerpts: tuple = ()
        self._search_error = False
        self._supplementary_evidence: tuple[ProvenanceEvidence, ...] = ()
        self.rejections: int = 0
        self.format_present = False
        self.blocked: bool = False

    def begin_turn(
        self,
        excerpts: Iterable[CallerExcerptLike],
        *,
        supplementary_evidence: Iterable = (),
        facts: Iterable = (),
        read_ranges: Iterable = (),
        search_error: bool = False,
    ) -> None:
        self._excerpts = tuple(excerpts or ())
        self._search_error = search_error
        self._supplementary_evidence = _combine_supplementary(
            supplementary_evidence=supplementary_evidence,
            facts=facts,
            read_ranges=read_ranges,
        )
        self.rejections = 0
        self.format_present = False
        self.blocked = False

    def set_supplementary_evidence(
        self,
        supplementary_evidence: Iterable = (),
        *,
        facts: Iterable = (),
        read_ranges: Iterable = (),
    ) -> None:
        """Update bounded supplementary evidence without altering caller excerpts."""
        self._supplementary_evidence = _combine_supplementary(
            supplementary_evidence=supplementary_evidence,
            facts=facts,
            read_ranges=read_ranges,
        )

    @property
    def supplementary_evidence(self) -> tuple[ProvenanceEvidence, ...]:
        return self._supplementary_evidence

    @property
    def has_evidence(self) -> bool:
        return bool(self._excerpts)

    @property
    def attached_paths(self) -> frozenset[str]:
        return frozenset(
            path
            for excerpt in self._excerpts
            if (path := (getattr(excerpt, "path", "") or "").strip())
        )

    def __call__(
        self,
        content: str,
        *,
        token_count=None,
        io=None,
    ) -> ResponseHookResult | None:
        self.format_present = False
        self.blocked = False
        if not self.enabled or not content:
            return None
        edit_marker = self._edit_marker.search(content)
        if not edit_marker:
            return None
        if self._search_error:
            if io:
                io.tool_output("Local caller contract check: SEARCH_ERROR (pass-through)")
            return None
        # Without caller excerpts (NO_EVIDENCE), pass through so valid edits are not discarded.
        # Supplementary evidence (facts/read-ranges) still never satisfies caller checks.
        if not self._excerpts:
            if io:
                io.tool_output("Local caller contract check: NO_EVIDENCE (pass-through)")
            return None

        pre_edit = content[: edit_marker.start()]
        contract = _CONTRACT_RE.search(pre_edit)
        trace = _TRACE_RE.search(pre_edit, contract.end()) if contract else None
        if trace and any(ref in pre_edit for ref in _excerpt_references(self._excerpts)):
            self.format_present = True
            if io:
                io.tool_output("Local caller contract format present; semantics unverified")
            return None

        if self.rejections >= self.max_rejections:
            if self.advisory:
                if io:
                    io.tool_output("Local caller contract check: ENFORCEMENT_EXHAUSTED")
                return None
            self.blocked = True
            if io:
                io.tool_output("Local caller contract check: BLOCKED (enforcement exhausted)")
            return ResponseHookResult()

        self.rejections += 1
        self.blocked = True
        if io is not None:
            io.tool_output("Local caller contract check: BLOCKED (missing contract/trace)")

        caller_labels = ", ".join(
            sorted(
                {
                    f"{getattr(e, 'path', '')} ({getattr(e, 'symbol', '')})".strip()
                    for e in self._excerpts
                    if getattr(e, "path", "") or getattr(e, "symbol", "")
                }
            )
        )
        caller_note = f" Attached callers: {caller_labels}." if caller_labels else ""
        supplementary_note = ""
        if self._supplementary_evidence:
            sup_labels = ", ".join(
                f"{item.kind}:{item.source}" for item in self._supplementary_evidence[:4]
            )
            supplementary_note = (
                f" Supplementary context ({sup_labels}) is read-only and does not replace "
                "caller verification."
            )

        continuation = (
            "Before providing your edit block, verify the caller contract explicitly."
            f"{caller_note}{supplementary_note}\n"
            "Write these two short sections before your edit:\n"
            "1. `CALLER_CONTRACT:` — name the caller file or symbol, quote the exact "
            "caller line(s) from the attached snapshot, and state the expected return "
            "shape, unpacking order, types, units, or edge-case behavior.\n"
            "2. `TRACE:` — step through 2 concrete inputs (a normal case and an edge "
            "case) from your modified implementation into the caller line.\n"
            "Then provide your edit block."
        )
        return ResponseHookResult(continuation=continuation)


class IsolatedCallerAuditor:
    """Decoupled Chain-of-Verification caller-contract auditor."""

    def __init__(
        self,
        complete_fn: Callable[[list[dict]], str | None],
        *,
        max_audits: int = 1,
    ):
        self._complete_fn = complete_fn
        self.max_audits = max_audits
        self._excerpts: tuple = ()
        self._search_error = False
        self._supplementary_evidence: tuple[ProvenanceEvidence, ...] = ()
        self.audits_run: int = 0

    def begin_turn(
        self,
        excerpts: Iterable[CallerExcerptLike],
        *,
        supplementary_evidence: Iterable = (),
        facts: Iterable = (),
        read_ranges: Iterable = (),
        search_error: bool = False,
    ) -> None:
        self._excerpts = tuple(excerpts or ())
        self._search_error = search_error
        self._supplementary_evidence = _combine_supplementary(
            supplementary_evidence=supplementary_evidence,
            facts=facts,
            read_ranges=read_ranges,
        )
        self.audits_run = 0

    def set_supplementary_evidence(
        self,
        supplementary_evidence: Iterable = (),
        *,
        facts: Iterable = (),
        read_ranges: Iterable = (),
    ) -> None:
        """Update bounded supplementary evidence without altering caller excerpts."""
        self._supplementary_evidence = _combine_supplementary(
            supplementary_evidence=supplementary_evidence,
            facts=facts,
            read_ranges=read_ranges,
        )

    @property
    def supplementary_evidence(self) -> tuple[ProvenanceEvidence, ...]:
        return self._supplementary_evidence

    def build_messages(
        self,
        diff_text: str,
        *,
        task: str = "",
        updated_files: dict[str, str] | None = None,
    ) -> list[dict]:
        caller_section = "\n\n".join(_format_excerpt(excerpt) for excerpt in self._excerpts)
        sections: list[str] = []
        if task and task.strip():
            sections.append(f"## Task\n{task.strip()}")
        sections.append(f"## Caller snippets\n{caller_section}")
        if self._supplementary_evidence:
            supplementary_section = "\n\n".join(
                item.format() for item in self._supplementary_evidence
            )
            sections.append(
                "## Supplementary evidence (non-authoritative; cannot replace caller snippets)\n"
                f"{supplementary_section}"
            )
        if diff_text:
            patch = diff_text[:_MAX_PATCH_CHARS]
            if len(diff_text) > _MAX_PATCH_CHARS:
                patch += "\n[remaining patch omitted]"
            sections.append(f"## Proposed diff\n{patch}")
        user_content = "\n\n".join(sections)
        return [
            {"role": "system", "content": _AUDITOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def audit(
        self,
        diff_text: str,
        *,
        task: str = "",
        updated_files: dict[str, str] | None = None,
        io=None,
    ) -> AuditVerdict:
        # Supplementary evidence (facts/read-ranges) NEVER substitutes for missing
        # or incomplete caller excerpts.
        if self._search_error:
            verdict = AuditVerdict("ERROR", "SEARCH_ERROR")
        elif not self._excerpts:
            verdict = AuditVerdict("UNKNOWN", "NO_EVIDENCE")
        elif not diff_text.strip() or len(diff_text) > _MAX_PATCH_CHARS:
            verdict = AuditVerdict("UNKNOWN", "INCOMPLETE: patch missing or truncated")
        elif any(getattr(e, "incomplete", False) for e in self._excerpts):
            verdict = AuditVerdict("UNKNOWN", "INCOMPLETE: caller evidence")
        elif self.audits_run >= self.max_audits:
            verdict = AuditVerdict("UNKNOWN", "Audit budget exhausted")
        else:
            self.audits_run += 1
            messages = self.build_messages(diff_text, task=task)
            try:
                verdict = parse_audit_verdict(self._complete_fn(messages))
            except Exception as exc:
                verdict = AuditVerdict("ERROR", str(exc)[:_MAX_REASON_CHARS])
        if io is not None:
            detail = "" if verdict.passed else f" - {verdict.reason}"
            io.tool_output(f"Local caller audit: {verdict.status}{detail}")
        return verdict

    def build_continuation(self, verdict: AuditVerdict) -> str:
        return (
            f"The caller audit returned {verdict.status}. The edits remain in the working tree.\n"
            f"{verdict.reason}\n\n"
            "Please revise the implementation so that every caller works as expected "
            "for both normal and edge-case inputs."
        )
