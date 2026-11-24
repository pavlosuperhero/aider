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

_AUDITOR_SYSTEM_PROMPT = """\
You are an independent caller-contract verifier. You receive ONLY the task \
summary, the caller snippets, and the proposed diff (with no prior \
conversation history).

Evaluate strictly whether the proposed diff satisfies every caller's \
contract. Do NOT assume any previous buggy behavior should be preserved; judge \
only against the task and the callers.

Complete these 4 steps in order:
1. CALLER_EXPECTS: Quote each caller line and state the exact semantic role of \
every variable assigned or unpacked from the call, plus how downstream lines use \
it (element order, types, units, prefixes/formatting, and null/sentinel checks).
2. TRACE_REALISTIC: Pick 1 realistic, standard-format, asymmetric input matching \
the task and parameter names (never use symmetric placeholders like 'A B' or \
[1, 1]) and 1 edge case. Trace the EXACT changed behavior in the proposed diff \
line by line to compute its literal return value, then substitute that return \
value into the caller's exact variable names AND evaluate the caller's final \
output expression.
3. MISMATCH_CHECK: Verify that every variable bound in the caller actually holds \
what its variable name and downstream expression require (e.g., unpacked items \
are not swapped, numeric values use the caller's expected unit, types and \
null/sentinel checks match, and the caller's final output is valid).
4. Output your verdict on a separate final line in plain text, without numbering or Markdown.
For a verified match output:
VERDICT: PASS
If evidence is insufficient output:
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
        return str(formatter())
    path = getattr(excerpt, "path", "")
    start_line = getattr(excerpt, "start_line", 1)
    code = getattr(excerpt, "code", "")
    return f"{path}:{start_line}\n{code}"


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

    def __init__(self, *, enabled: bool = False, max_rejections: int = 2, advisory=False):
        self.advisory = advisory
        self.enabled = bool(enabled)
        self.max_rejections = max_rejections
        self._excerpts: tuple = ()
        self.rejections: int = 0
        self.format_present = False

    def begin_turn(self, excerpts: Iterable[CallerExcerptLike]) -> None:
        self._excerpts = tuple(excerpts or ())
        self.rejections = 0
        self.format_present = False

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
        if not self.enabled or not content:
            return None
        edit_marker = re.search(
            r"<<<<<<< SEARCH|^\*\*\* (?:Begin Patch|Update File:|Add File:|Delete File:)",
            content,
            re.M,
        ) or re.search(r"```", content)
        if not edit_marker:
            return None
        if not self._excerpts:
            if io:
                io.tool_output("Local caller contract check: NO_EVIDENCE")
            return None if self.advisory else ResponseHookResult()

        pre_edit = content[: edit_marker.start()]
        contract = _CONTRACT_RE.search(pre_edit)
        trace = _TRACE_RE.search(pre_edit, contract.end()) if contract else None
        if trace and any(ref in pre_edit for ref in _excerpt_references(self._excerpts)):
            self.format_present = True
            if io:
                io.tool_output("Local caller contract format present; semantics unverified")
            return None

        if self.rejections >= self.max_rejections:
            if io:
                io.tool_output("Local caller contract check: ENFORCEMENT_EXHAUSTED")
            return None if self.advisory else ResponseHookResult()

        self.rejections += 1
        if io is not None:
            io.tool_output("Local caller contract check: missing contract/trace")

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

        continuation = (
            "Before providing your edit block, verify the caller contract explicitly."
            f"{caller_note}\n"
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
        self.audits_run: int = 0

    def begin_turn(self, excerpts: Iterable[CallerExcerptLike]) -> None:
        self._excerpts = tuple(excerpts or ())
        self.audits_run = 0

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
        if not self._excerpts:
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
