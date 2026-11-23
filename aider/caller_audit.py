"""Contract-first response hook and isolated CoVe caller-contract auditor.

Pure policy module (Clean Architecture): imports only stdlib and
aider.hooks.ResponseHookResult. Has no dependency on Coder, litellm,
tree_sitter, git, or aider.local_callers.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Callable, Iterable, Protocol

from aider.hooks import ResponseHookResult

_VERDICT_RE = re.compile(r"VERDICT\s*:\s*\*{0,2}(PASS|FAIL)\*{0,2}\b", re.IGNORECASE)
_CONTRACT_RE = re.compile(r"\bCALLER_CONTRACT\s*:", re.IGNORECASE)
_TRACE_RE = re.compile(r"\bTRACE\s*:", re.IGNORECASE)
_REASON_PREFIX_RE = re.compile(r"^[\s:\-\u2013\u2014*]+")
_MAX_REASON_CHARS = 600

_AUDITOR_SYSTEM_PROMPT = """\
You are an independent caller-contract verifier. You receive ONLY the task \
summary, the caller snippets, and the updated implementation (with no prior \
conversation history).

Evaluate strictly whether the UPDATED implementation satisfies every caller's \
contract. Do NOT assume any previous buggy behavior should be preserved; judge \
only against the task and the callers.

Complete these 4 steps in order:
1. CALLER_EXPECTS: Quote each caller line and state the exact semantic role of \
every variable assigned or unpacked from the call, plus how downstream lines use \
it (element order, types, units, prefixes/formatting, and null/sentinel checks).
2. TRACE_REALISTIC: Pick 1 realistic, standard-format, asymmetric input matching \
the task and parameter names (never use symmetric placeholders like 'A B' or \
[1, 1]) and 1 edge case. Execute the EXACT updated implementation line by line \
to compute its literal return value, then substitute that return value into the \
caller's exact variable names AND evaluate the caller's final output expression.
3. MISMATCH_CHECK: Verify that every variable bound in the caller actually holds \
what its variable name and downstream expression require (e.g., unpacked items \
are not swapped, numeric values use the caller's expected unit, types and \
null/sentinel checks match, and the caller's final output is valid).
4. End with one final line:
VERDICT: PASS
or, ONLY if you proved a concrete input where a caller variable or caller result \
is wrong:
VERDICT: FAIL: <input -> actual return -> wrong caller variable/result -> required fix>"""


class CallerExcerptLike(Protocol):
    path: str
    start_line: int
    symbol: str
    code: str


@dataclass(frozen=True)
class AuditVerdict:
    passed: bool
    reason: str
    raw_response: str


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
    """Parse an auditor model response into an AuditVerdict (fail-open)."""
    if raw is None or not raw.strip():
        return AuditVerdict(
            passed=True,
            reason="empty audit response",
            raw_response=raw or "",
        )

    matches = list(_VERDICT_RE.finditer(raw))
    if not matches:
        return AuditVerdict(
            passed=True,
            reason="no verdict token found",
            raw_response=raw,
        )

    last = matches[-1]
    token = last.group(1).upper()
    tail = _REASON_PREFIX_RE.sub("", raw[last.end() :]).strip()

    if token == "FAIL":
        reason = tail[:_MAX_REASON_CHARS] if tail else raw.strip()[:_MAX_REASON_CHARS]
        return AuditVerdict(passed=False, reason=reason, raw_response=raw)

    reason = tail[:_MAX_REASON_CHARS] if tail else "PASS"
    return AuditVerdict(passed=True, reason=reason, raw_response=raw)


class CallerContractHook:
    """Pre-apply response hook enforcing CALLER_CONTRACT + TRACE before edits."""

    def __init__(self, *, enabled: bool = False, max_rejections: int = 2):
        self.enabled = bool(enabled)
        self.max_rejections = max_rejections
        self._excerpts: tuple = ()
        self.rejections: int = 0
        self._satisfied: bool = False

    def begin_turn(self, excerpts: Iterable[CallerExcerptLike]) -> None:
        self._excerpts = tuple(excerpts or ())
        self.rejections = 0
        self._satisfied = False

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
        if (
            not self.enabled
            or self._satisfied
            or not self._excerpts
            or self.rejections >= self.max_rejections
        ):
            return None
        if not content:
            return None
        if "<<<<<<< SEARCH" not in content and "```" not in content:
            return None

        has_contract = bool(_CONTRACT_RE.search(content))
        has_trace = bool(_TRACE_RE.search(content))

        pre_edit = content.split("<<<<<<< SEARCH", 1)[0] if "<<<<<<< SEARCH" in content else content
        refs = _excerpt_references(self._excerpts)
        has_caller_ref = any(ref in pre_edit for ref in refs)

        if has_contract and has_trace and has_caller_ref:
            self._satisfied = True
            return None

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
        if updated_files:
            file_blocks = [
                f"### {path}\n{code[:4000]}"
                for path, code in sorted(updated_files.items())
                if code
            ]
            if file_blocks:
                sections.append("## Updated implementation\n" + "\n\n".join(file_blocks))
        elif diff_text:
            sections.append(f"## Proposed diff\n{diff_text}")
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
    ) -> AuditVerdict | None:
        has_code = bool(updated_files) or bool(diff_text and diff_text.strip())
        if not self._excerpts or not has_code or self.audits_run >= self.max_audits:
            return None

        self.audits_run += 1
        messages = self.build_messages(diff_text, task=task, updated_files=updated_files)
        try:
            raw = self._complete_fn(messages)
        except Exception as exc:
            if io is not None:
                io.tool_warning(f"Local caller audit failed ({exc}); continuing.")
            return AuditVerdict(
                passed=True,
                reason=f"audit error: {exc}",
                raw_response="",
            )

        verdict = parse_audit_verdict(raw)
        if io is not None:
            if verdict.passed:
                io.tool_output("Local caller audit: PASS")
            else:
                io.tool_output(f"Local caller audit: FAIL - {verdict.reason}")
        return verdict

    def build_continuation(self, verdict: AuditVerdict) -> str:
        return (
            "An independent caller-contract audit found a mismatch between your "
            "proposed edit and the attached caller snippet(s):\n\n"
            f"{verdict.reason}\n\n"
            "Please revise the implementation so that every caller works as expected "
            "for both normal and edge-case inputs."
        )
