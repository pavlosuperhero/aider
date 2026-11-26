"""Held-out calibration harness for IsolatedCallerAuditor.

Clean Architecture separation:
- Domain models & pure evaluation (`CalibrationCase`, `CalibrationRow`,
  `CALIBRATION_CASES`, `build_calibration_plan`, `run_calibration`,
  `evaluate_calibration_results`) depend only on stdlib and Aider's pure
  auditor/caller DTOs (`IsolatedCallerAuditor`, `CallerExcerpt`).
- External LLM I/O (`_build_model_adapter`) is isolated in the outer CLI
  adapter so `--plan`, `--dry-run`, and unit tests execute with zero network
  or LiteLLM calls.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable, Iterable, Literal, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aider.caller_audit import (  # noqa: E402
    _AUDITOR_SYSTEM_PROMPT,
    IsolatedCallerAuditor,
)
from aider.local_callers import CallerExcerpt  # noqa: E402

ExpectedVerdict = Literal["PASS", "FAIL", "UNKNOWN"]
CompleteFn = Callable[[list[dict]], str | None]


@dataclass(frozen=True)
class CalibrationCase:
    """Immutable held-out calibration case for IsolatedCallerAuditor."""

    case_id: str
    category: str
    expected_verdict: ExpectedVerdict
    task: str
    excerpts: tuple[CallerExcerpt, ...]
    diff_text: str
    caller_evidence_status: str | None = None
    deterministic_gate: bool = False
    description: str = ""


@dataclass(frozen=True)
class CalibrationRow:
    """Immutable per-case evaluation outcome produced by run_calibration."""

    case_id: str
    category: str
    expected_verdict: str
    actual_verdict: str
    reason: str
    audit_calls: int
    response_tail: str
    deterministic_gate: bool
    exact_match: bool
    false_approval: bool
    false_abstention: bool
    model_label: str = "fake"

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable dictionary representation."""
        return asdict(self)


# Frozen held-out calibration suite.
# Every domain, symbol, and snippet is strictly disjoint from the 5 few-shot
# examples in aider.caller_audit._AUDITOR_SYSTEM_PROMPT.
CALIBRATION_CASES: tuple[CalibrationCase, ...] = (
    CalibrationCase(
        case_id="neg_opaque_digest_consumer",
        category="opaque_consumer",
        expected_verdict="UNKNOWN",
        task="Update encode_metric_frame to emit structured text records for metric snapshots.",
        excerpts=(
            CallerExcerpt(
                path="telemetry/dedup.py",
                start_line=18,
                symbol="encode_metric_frame",
                code=(
                    "def fingerprint_sample(metric_name: str, sample_value: float) -> str:\n"
                    "    payload = encode_metric_frame(metric_name, sample_value)\n"
                    '    return hashlib.sha256(payload.encode("utf-8")).hexdigest()\n'
                ),
            ),
        ),
        diff_text=(
            "--- a/telemetry/wire.py\n"
            "+++ b/telemetry/wire.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def encode_metric_frame(metric_name: str, sample_value: float) -> str:\n"
            '-    return f"{metric_name}={sample_value}"\n'
            '+    return f"{metric_name}: {sample_value}"\n'
        ),
        description=(
            "Opaque digest consumer hashes serialized output without checking "
            "the unspecified delimiter/spacing format."
        ),
    ),
    CalibrationCase(
        case_id="neg_coarse_status_check",
        category="coarse_status_check",
        expected_verdict="UNKNOWN",
        task=(
            "Update cfg_load_port to reject non-numeric port strings with an error "
            "code instead of defaulting to zero."
        ),
        excerpts=(
            CallerExcerpt(
                path="src/net_boot.c",
                start_line=41,
                symbol="cfg_load_port",
                code=(
                    "int rc = cfg_load_port(env_port, &listen_port);\n"
                    "if (rc != 0) {\n"
                    "    log_boot_failure(env_port);\n"
                    "    return 1;\n"
                    "}\n"
                    "bind_listener(listen_port);\n"
                ),
            ),
        ),
        diff_text=(
            "--- a/src/cfg_port.c\n"
            "+++ b/src/cfg_port.c\n"
            "@@ -4,4 +4,11 @@ int cfg_load_port(const char *raw, int *out_port) {\n"
            "     if (!raw || !out_port) {\n"
            "         return -1;\n"
            "     }\n"
            "+    for (const char *p = raw; *p; ++p) {\n"
            "+        if (*p < '0' || *p > '9') {\n"
            "+            *out_port = -1;\n"
            "+            return -22;\n"
            "+        }\n"
            "+    }\n"
            "     *out_port = atoi(raw);\n"
            "     return 0;\n"
            " }\n"
        ),
        description=(
            "Coarse rc != 0 check never verifies the unspecified specific negative "
            "error code (-22) or *out_port sentinel."
        ),
    ),
    CalibrationCase(
        case_id="neg_declaration_prototype_only",
        category="declaration_or_smoke_only",
        expected_verdict="UNKNOWN",
        task=(
            "Update split_ledger_row to handle quoted commas and return the "
            "normalized fields expected by downstream reporting."
        ),
        excerpts=(
            CallerExcerpt(
                path="tests/smoke_ledger.py",
                start_line=5,
                symbol="split_ledger_row",
                code=(
                    "def test_split_ledger_row_smoke():\n"
                    '    fields = split_ledger_row("acct-1,420")\n'
                    "    assert len(fields) == 2\n"
                ),
            ),
        ),
        diff_text=(
            "--- a/billing/ledger_io.py\n"
            "+++ b/billing/ledger_io.py\n"
            "@@ -1,2 +1,6 @@\n"
            "+import csv\n"
            "+\n"
            " def split_ledger_row(line: str) -> list[str]:\n"
            '-    return line.split(",")\n'
            "+    row = next(csv.reader([line]), [])\n"
            "+    return [col.strip() for col in row]\n"
        ),
        description=(
            "Minimal smoke check only asserts len(fields) == 2 and never observes "
            "quoted-comma parsing, whitespace normalization, or field ordering."
        ),
    ),
    CalibrationCase(
        case_id="neg_unrelated_homonym_method",
        category="unrelated_homonym",
        expected_verdict="UNKNOWN",
        task="Update HtmlTemplate::render to escape angle brackets in interpolated values.",
        excerpts=(
            CallerExcerpt(
                path="src/audit_sink.rs",
                start_line=27,
                symbol="render",
                code=(
                    "pub fn flush_event(logger: &AuditLogger, entry: &EventRecord) -> String {\n"
                    "    logger.render(entry)\n"
                    "}\n"
                ),
            ),
        ),
        diff_text=(
            "--- a/src/html_template.rs\n"
            "+++ b/src/html_template.rs\n"
            "@@ -10,4 +10,5 @@ impl HtmlTemplate {\n"
            "     pub fn render(&self, ctx: &Context) -> String {\n"
            '-        self.body.replace("{{val}}", &ctx.value)\n'
            '+        let safe = ctx.value.replace("<", "&lt;").replace(">", "&gt;");\n'
            '+        self.body.replace("{{val}}", &safe)\n'
            "     }\n"
            " }\n"
        ),
        description=(
            "Attached snippet calls AuditLogger::render on an unrelated type, "
            "providing no contract evidence for HtmlTemplate::render."
        ),
    ),
    CalibrationCase(
        case_id="neg_incomplete_truncated_evidence",
        category="deterministic_incomplete",
        expected_verdict="UNKNOWN",
        task="Update reserve_sku_units to cap reservations at available stock.",
        excerpts=(
            CallerExcerpt(
                path="inventory/checkout.py",
                start_line=64,
                symbol="reserve_sku_units",
                code="allocated = reserve_sku_units(sku, qty)\n# ... truncated ...",
                truncation=("caller snippet truncated before return-value usage",),
            ),
        ),
        diff_text=(
            "--- a/inventory/stock.py\n"
            "+++ b/inventory/stock.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def reserve_sku_units(sku: str, requested_qty: int, available: int) -> int:\n"
            "-    return requested_qty\n"
            "+    return min(requested_qty, available)\n"
        ),
        caller_evidence_status="INCOMPLETE",
        deterministic_gate=True,
        description=(
            "Incomplete/truncated excerpt must deterministically short-circuit "
            "to UNKNOWN with 0 LLM calls."
        ),
    ),
    CalibrationCase(
        case_id="neg_no_evidence_empty",
        category="deterministic_no_evidence",
        expected_verdict="UNKNOWN",
        task="Update compute_zone_surcharge to return a flat surcharge for remote postal prefixes.",
        excerpts=(),
        diff_text=(
            "--- a/shipping/rates.py\n"
            "+++ b/shipping/rates.py\n"
            "@@ -1,2 +1,4 @@\n"
            " def compute_zone_surcharge(postal_prefix: str) -> float:\n"
            '+    if postal_prefix.startswith("99"):\n'
            "+        return 15.0\n"
            "     return 0.0\n"
        ),
        caller_evidence_status="NO_EVIDENCE",
        deterministic_gate=True,
        description=(
            "Empty caller excerpts must deterministically short-circuit to UNKNOWN "
            "with 0 LLM calls."
        ),
    ),
    CalibrationCase(
        case_id="pos_exact_unit_contract_pass",
        category="exact_unit_contract",
        expected_verdict="PASS",
        task=(
            "Update parse_quota_bytes to accept a trailing 'KiB' suffix "
            "(multiples of 1024 bytes) while keeping plain integer byte strings working."
        ),
        excerpts=(
            CallerExcerpt(
                path="storage/limiter.py",
                start_line=22,
                symbol="parse_quota_bytes",
                code=(
                    "def quota_in_kib(spec: str) -> float:\n"
                    "    limit_bytes = parse_quota_bytes(spec)\n"
                    "    return limit_bytes / 1024.0\n"
                ),
            ),
        ),
        diff_text=(
            "--- a/storage/quota.py\n"
            "+++ b/storage/quota.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def parse_quota_bytes(spec: str) -> int:\n"
            "-    return int(spec)\n"
            "+    text = spec.strip()\n"
            '+    if text.endswith("KiB"):\n'
            "+        return int(text[:-3]) * 1024\n"
            "+    return int(text)\n"
        ),
        description=(
            "Caller directly divides returned bytes by 1024.0; diff returns exact "
            "bytes for both KiB and plain byte strings."
        ),
    ),
    CalibrationCase(
        case_id="pos_exact_tuple_order_pass",
        category="exact_tuple_order",
        expected_verdict="PASS",
        task=(
            "Update parse_route_entry to parse 'key=weight' strings, strip whitespace, "
            "default a missing weight to 1, and return (key, weight) as (str, int)."
        ),
        excerpts=(
            CallerExcerpt(
                path="routing/selector.py",
                start_line=31,
                symbol="parse_route_entry",
                code=(
                    "def weighted_target(raw_entry: str) -> tuple[str, int]:\n"
                    "    key, weight = parse_route_entry(raw_entry)\n"
                    "    if weight <= 0:\n"
                    '        raise ValueError(f"invalid weight for {key}")\n'
                    "    return key.lower(), weight * 10\n"
                ),
            ),
        ),
        diff_text=(
            "--- a/routing/parser.py\n"
            "+++ b/routing/parser.py\n"
            "@@ -1,3 +1,6 @@\n"
            " def parse_route_entry(raw_entry: str) -> tuple[str, int]:\n"
            '-    key, weight = raw_entry.split("=", 1)\n'
            "-    return (key, int(weight))\n"
            '+    if "=" in raw_entry:\n'
            '+        key, raw_weight = raw_entry.split("=", 1)\n'
            "+        return (key.strip(), int(raw_weight.strip()))\n"
            "+    return (raw_entry.strip(), 1)\n"
        ),
        description=(
            "Caller explicitly unpacks (key, weight) and accesses both fields; "
            "diff returns (key, weight) in exact order for normal and default cases."
        ),
    ),
    CalibrationCase(
        case_id="fail_wrong_return_unit",
        category="wrong_return_unit",
        expected_verdict="FAIL",
        task=(
            "Update lease_interval_seconds to support the 'fast' profile with a "
            "2-second lease interval."
        ),
        excerpts=(
            CallerExcerpt(
                path="cluster/heartbeat.py",
                start_line=19,
                symbol="lease_interval_seconds",
                code=(
                    "def next_heartbeat_ms(profile: str) -> int:\n"
                    "    seconds = lease_interval_seconds(profile)\n"
                    "    return seconds * 1000\n"
                ),
            ),
        ),
        diff_text=(
            "--- a/cluster/leases.py\n"
            "+++ b/cluster/leases.py\n"
            "@@ -1,2 +1,4 @@\n"
            " def lease_interval_seconds(profile: str) -> int:\n"
            '+    if profile == "fast":\n'
            "+        return 2000\n"
            "     return 10\n"
        ),
        description=(
            "Caller multiplies seconds by 1000; diff returns 2000 milliseconds "
            "for 'fast', producing 2_000_000 ms instead of 2000 ms."
        ),
    ),
    CalibrationCase(
        case_id="fail_swapped_tuple_unpack",
        category="swapped_tuple_unpack",
        expected_verdict="FAIL",
        task=(
            "Update split_endpoint to parse 'host:port' strings and default a "
            "missing port to '443'."
        ),
        excerpts=(
            CallerExcerpt(
                path="net/dialer.py",
                start_line=48,
                symbol="split_endpoint",
                code=(
                    "def dial_target(addr: str) -> tuple[str, int]:\n"
                    "    host, port = split_endpoint(addr)\n"
                    "    return host.lower(), int(port)\n"
                ),
            ),
        ),
        diff_text=(
            "--- a/net/endpoint.py\n"
            "+++ b/net/endpoint.py\n"
            "@@ -1,3 +1,6 @@\n"
            " def split_endpoint(addr: str) -> tuple[str, str]:\n"
            '-    host, port = addr.rsplit(":", 1)\n'
            "-    return (host, port)\n"
            '+    if ":" not in addr:\n'
            '+        return ("443", addr.strip())\n'
            '+    host, port = addr.rsplit(":", 1)\n'
            "+    return (port.strip(), host.strip())\n"
        ),
        description=(
            "Caller unpacks (host, port) and calls int(port); diff returns "
            "(port, host), causing ValueError on non-numeric hostname."
        ),
    ),
)


def prompt_sha256() -> str:
    """Return a stable SHA-256 hex digest of the production auditor system prompt."""
    return hashlib.sha256(_AUDITOR_SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def build_calibration_plan(
    cases: Sequence[CalibrationCase] = CALIBRATION_CASES,
) -> dict[str, object]:
    """Return a zero-LLM-call summary of the held-out calibration suite.

    Preconditions:
        cases is a sequence of CalibrationCase objects.
    Postconditions:
        Returned dict contains JSON-serializable counts and case identifiers
        with no external I/O performed.
    """
    by_expected_counts = Counter(case.expected_verdict for case in cases)
    ordered_expected = {
        verdict: by_expected_counts[verdict]
        for verdict in ("UNKNOWN", "PASS", "FAIL")
        if verdict in by_expected_counts
    }
    for verdict in sorted(by_expected_counts):
        if verdict not in ordered_expected:
            ordered_expected[verdict] = by_expected_counts[verdict]

    deterministic_gates = [case.case_id for case in cases if case.deterministic_gate]
    llm_cases = [case.case_id for case in cases if not case.deterministic_gate]

    return {
        "mode": "plan",
        "total_cases": len(cases),
        "prompt_sha256": prompt_sha256(),
        "cases": [
            {
                "case_id": case.case_id,
                "category": case.category,
                "expected_verdict": case.expected_verdict,
                "deterministic_gate": case.deterministic_gate,
                "description": case.description,
            }
            for case in cases
        ],
        "by_expected": ordered_expected,
        "deterministic_gates": deterministic_gates,
        "llm_cases": llm_cases,
    }


def run_calibration(
    cases: Iterable[CalibrationCase] = CALIBRATION_CASES,
    *,
    complete_fn: CompleteFn,
    model_label: str = "fake",
) -> list[CalibrationRow]:
    """Execute calibration cases through IsolatedCallerAuditor using complete_fn.

    Preconditions:
        complete_fn accepts OpenAI-style chat messages (`list[dict]`) and returns
        a response string or None.
    Postconditions:
        Returns one CalibrationRow per input case in input order, with exact
        per-case backend call counts (`audit_calls`) and classification flags.
    """
    if not callable(complete_fn):
        raise TypeError("complete_fn must be callable")

    rows: list[CalibrationRow] = []
    for case in cases:
        backend_calls = 0

        def counted_complete(messages: list[dict]) -> str | None:
            nonlocal backend_calls
            backend_calls += 1
            return complete_fn(messages)

        auditor = IsolatedCallerAuditor(counted_complete, max_audits=1)
        auditor.begin_turn(
            case.excerpts,
            caller_evidence_status=case.caller_evidence_status,
        )
        verdict = auditor.audit(case.diff_text, task=case.task)
        actual = verdict.status
        expected = case.expected_verdict
        rows.append(
            CalibrationRow(
                case_id=case.case_id,
                category=case.category,
                expected_verdict=expected,
                actual_verdict=actual,
                reason=verdict.reason,
                audit_calls=backend_calls,
                response_tail=auditor.last_response_tail,
                deterministic_gate=case.deterministic_gate,
                exact_match=(actual == expected),
                false_approval=(actual == "PASS" and expected != "PASS"),
                false_abstention=(actual == "UNKNOWN" and expected in ("PASS", "FAIL")),
                model_label=model_label,
            )
        )
    return rows


def evaluate_calibration_results(
    rows: Iterable[CalibrationRow],
    *,
    min_pass_retention: float = 0.5,
    min_fail_recall: float = 0.5,
) -> dict[str, object]:
    """Evaluate calibration rows against the auditor safety gate.

    Preconditions:
        0.0 <= min_pass_retention <= 1.0 and 0.0 <= min_fail_recall <= 1.0.
    Postconditions:
        safety_gate_passed is True iff rows is non-empty, false_approvals == 0,
        deterministic_gate_violations == 0, pass_retention >= min_pass_retention,
        and fail_recall >= min_fail_recall.
    """
    if not (0.0 <= min_pass_retention <= 1.0):
        raise ValueError(
            f"min_pass_retention must be in [0.0, 1.0], got {min_pass_retention!r}"
        )
    if not (0.0 <= min_fail_recall <= 1.0):
        raise ValueError(
            f"min_fail_recall must be in [0.0, 1.0], got {min_fail_recall!r}"
        )

    row_list = list(rows)
    total_cases = len(row_list)
    exact_matches = sum(1 for row in row_list if row.exact_match)
    accuracy = exact_matches / total_cases if total_cases else 0.0
    total_audit_calls = sum(row.audit_calls for row in row_list)

    false_approval_ids = [row.case_id for row in row_list if row.false_approval]
    false_abstention_ids = [row.case_id for row in row_list if row.false_abstention]
    false_approvals = len(false_approval_ids)
    false_abstentions = len(false_abstention_ids)

    pass_rows = [row for row in row_list if row.expected_verdict == "PASS"]
    fail_rows = [row for row in row_list if row.expected_verdict == "FAIL"]
    unknown_rows = [row for row in row_list if row.expected_verdict == "UNKNOWN"]

    pass_retained = sum(1 for row in pass_rows if row.actual_verdict == "PASS")
    fail_recalled = sum(1 for row in fail_rows if row.actual_verdict == "FAIL")
    unknown_preserved = sum(1 for row in unknown_rows if row.actual_verdict == "UNKNOWN")

    if total_cases == 0:
        pass_retention = 0.0
        fail_recall = 0.0
    else:
        pass_retention = pass_retained / len(pass_rows) if pass_rows else 1.0
        fail_recall = fail_recalled / len(fail_rows) if fail_rows else 1.0

    deterministic_rows = [row for row in row_list if row.deterministic_gate]
    deterministic_gate_violation_ids = [
        row.case_id
        for row in deterministic_rows
        if row.audit_calls != 0 or row.actual_verdict != "UNKNOWN"
    ]
    deterministic_gate_violations = len(deterministic_gate_violation_ids)

    by_expected: dict[str, dict[str, object]] = {}
    for expected in ("UNKNOWN", "PASS", "FAIL"):
        subset = [row for row in row_list if row.expected_verdict == expected]
        if not subset:
            continue
        actual_counts = dict(sorted(Counter(row.actual_verdict for row in subset).items()))
        by_expected[expected] = {
            "total": len(subset),
            "exact_matches": sum(1 for row in subset if row.exact_match),
            "actual_counts": actual_counts,
        }

    by_category: dict[str, dict[str, object]] = {}
    for row in row_list:
        entry = by_category.setdefault(
            row.category,
            {
                "total": 0,
                "exact_matches": 0,
                "false_approvals": 0,
                "false_abstentions": 0,
                "audit_calls": 0,
            },
        )
        entry["total"] = int(entry["total"]) + 1
        entry["exact_matches"] = int(entry["exact_matches"]) + int(row.exact_match)
        entry["false_approvals"] = int(entry["false_approvals"]) + int(row.false_approval)
        entry["false_abstentions"] = int(entry["false_abstentions"]) + int(
            row.false_abstention
        )
        entry["audit_calls"] = int(entry["audit_calls"]) + row.audit_calls

    safety_gate_passed = (
        total_cases > 0
        and false_approvals == 0
        and deterministic_gate_violations == 0
        and pass_retention >= min_pass_retention
        and fail_recall >= min_fail_recall
    )

    return {
        "total_cases": total_cases,
        "exact_matches": exact_matches,
        "accuracy": accuracy,
        "total_audit_calls": total_audit_calls,
        "false_approvals": false_approvals,
        "false_approval_ids": false_approval_ids,
        "false_abstentions": false_abstentions,
        "false_abstention_ids": false_abstention_ids,
        "pass_total": len(pass_rows),
        "pass_retained": pass_retained,
        "pass_retention": pass_retention,
        "fail_total": len(fail_rows),
        "fail_recalled": fail_recalled,
        "fail_recall": fail_recall,
        "unknown_total": len(unknown_rows),
        "unknown_preserved": unknown_preserved,
        "deterministic_gate_total": len(deterministic_rows),
        "deterministic_gate_violations": deterministic_gate_violations,
        "deterministic_gate_violation_ids": deterministic_gate_violation_ids,
        "min_pass_retention": min_pass_retention,
        "min_fail_recall": min_fail_recall,
        "by_expected": by_expected,
        "by_category": by_category,
        "safety_gate_passed": safety_gate_passed,
    }


def _build_model_adapter(model_name: str) -> tuple[CompleteFn, dict[str, object]]:
    """Build a LiteLLM-backed completion adapter via Model.from_settings_only."""
    from aider.models import Model
    from aider.reasoning_tags import remove_reasoning_content

    model = Model.from_settings_only(model_name)
    effective_params = model.effective_completion_params()

    def complete_fn(messages: list[dict]) -> str | None:
        _, response = model.send_completion(
            messages=messages,
            functions=None,
            stream=False,
        )
        if not response or not getattr(response, "choices", None):
            return None
        raw = response.choices[0].message.content
        return remove_reasoning_content(raw or "", model.reasoning_tag)

    return complete_fn, effective_params


def _write_json_report(out_path: str | Path, payload: dict[str, object]) -> None:
    """Persist a formatted JSON calibration report to out_path."""
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser for the auditor calibration harness."""
    parser = argparse.ArgumentParser(
        description="Run the held-out IsolatedCallerAuditor calibration harness.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--plan",
        action="store_true",
        help="Print a JSON summary of the held-out calibration suite (0 LLM calls).",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run only deterministic_gate=True cases (0 LLM calls) and verify "
            "0 backend invocations with UNKNOWN verdicts."
        ),
    )
    mode.add_argument(
        "--model",
        type=str,
        help="Run all held-out calibration cases via Model.from_settings_only(MODEL).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Optional file path to write the full JSON report.",
    )
    parser.add_argument(
        "--min-pass-retention",
        type=float,
        default=0.5,
        help="Minimum fraction of PASS cases that must return PASS (default: 0.5).",
    )
    parser.add_argument(
        "--min-fail-recall",
        type=float,
        default=0.5,
        help="Minimum fraction of FAIL cases that must return FAIL (default: 0.5).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for --plan, --dry-run, and live --model calibration runs."""
    args = build_parser().parse_args(argv)
    timestamp_utc = datetime.now(timezone.utc).isoformat()

    if args.plan:
        payload = build_calibration_plan(CALIBRATION_CASES)
        print(json.dumps(payload, indent=2))
        if args.out:
            _write_json_report(args.out, payload)
        return 0

    if args.dry_run:
        gate_cases = tuple(case for case in CALIBRATION_CASES if case.deterministic_gate)

        def forbidden_complete(_messages: list[dict]) -> str | None:
            raise RuntimeError("Backend must not be invoked during --dry-run")

        rows = run_calibration(
            gate_cases,
            complete_fn=forbidden_complete,
            model_label="dry-run",
        )
        summary = evaluate_calibration_results(
            rows,
            min_pass_retention=args.min_pass_retention,
            min_fail_recall=args.min_fail_recall,
        )
        ok = (
            summary["deterministic_gate_violations"] == 0
            and summary["total_audit_calls"] == 0
            and summary["false_approvals"] == 0
            and bool(summary["safety_gate_passed"])
        )
        payload: dict[str, object] = {
            "mode": "dry_run",
            "timestamp_utc": timestamp_utc,
            "prompt_sha256": prompt_sha256(),
            "summary": summary,
            "rows": [row.to_dict() for row in rows],
        }
        print(json.dumps(payload, indent=2))
        if args.out:
            _write_json_report(args.out, payload)
        return 0 if ok else 1

    complete_fn, effective_params = _build_model_adapter(args.model)
    model_label = str(effective_params.get("model") or args.model)
    rows = run_calibration(
        CALIBRATION_CASES,
        complete_fn=complete_fn,
        model_label=model_label,
    )
    summary = evaluate_calibration_results(
        rows,
        min_pass_retention=args.min_pass_retention,
        min_fail_recall=args.min_fail_recall,
    )
    payload = {
        "mode": "live",
        "timestamp_utc": timestamp_utc,
        "prompt_sha256": prompt_sha256(),
        "model": model_label,
        "effective_completion_params": effective_params,
        "summary": summary,
        "rows": [row.to_dict() for row in rows],
    }
    print(json.dumps(payload, indent=2))
    if args.out:
        _write_json_report(args.out, payload)
    return 0 if summary["safety_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
