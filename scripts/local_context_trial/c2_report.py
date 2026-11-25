"""Stratified statistical report generator for Issue #5 Section C2 replay trials.

Loads a frozen replay corpus manifest, verifies its canonical ``manifest_sha256``
preregistration digest, ingests Stage 1 (5 arms) and Stage 2 (one-factor-at-a-time
ablations) JSONL outputs plus ``.meta.json`` sidecars for one or more models, and
renders a rigorous stratified Markdown (and optional JSON) evaluation report.

Methodological grounding:
- Strict train/dev vs. unseen test separation (``ir-textbook``, p. 320,
  ``069dc983a616649ad3adc765``).
- Stratified heterogeneous evaluation & reproducibility (``beir-guide``,
  lines 414-438, ``90905b05c622807ee5f8a78d``).
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import heldout  # noqa: E402
import replay_corpus  # noqa: E402

STAGE1_ARM_ORDER: tuple[str, ...] = (
    "baseline",
    "passive",
    "contract",
    "audit",
    "contract+audit",
)
STAGE2_FACTOR_ORDER: tuple[str, ...] = (
    "cymbal",
    "read_range",
    "facts",
)
STAGE2_FACTOR_LABELS: dict[str, str] = {
    "cymbal": "cymbal (backend=cymbal)",
    "read_range": "read-range=on",
    "facts": "facts=on",
}
CATEGORY_ORDER: tuple[str, ...] = (
    "direct_caller",
    "indirect_caller",
    "control",
)
LANGUAGE_ORDER: tuple[str, ...] = (
    "python",
    "javascript",
    "typescript",
    "cpp",
    "bash",
    "c",
    "rust",
)

DEFAULT_CONTROL_MARGIN = 0.10
DEFAULT_ALPHA = 0.05
_Z_95 = 1.959963984540054


class ManifestVerificationError(ValueError):
    """Raised when a frozen replay manifest fails integrity or freeze verification."""


@dataclass(frozen=True)
class ModelRunSpec:
    """Input JSONL specification for a single model evaluation run."""

    label: str
    stage1_paths: tuple[Path, ...] = ()
    stage2_paths: tuple[Path, ...] = ()


# ---------------------------------------------------------------------------
# Statistical helpers (exact & standard stdlib-only implementations)
# ---------------------------------------------------------------------------


def wilson_ci(
    successes: int,
    total: int,
    *,
    z: float = _Z_95,
) -> tuple[float | None, float | None]:
    """Compute the two-sided Wilson score confidence interval for ``successes / total``."""
    if total < 0 or successes < 0 or successes > total:
        raise ValueError(f"Invalid binomial counts: successes={successes}, total={total}")
    if total == 0:
        return None, None
    p = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denom
    radicand = max(0.0, (p * (1.0 - p)) / total + z2 / (4.0 * total * total))
    margin = (z * math.sqrt(radicand)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def exact_mcnemar_p(wins: int, losses: int) -> float:
    """Return the exact two-sided McNemar / binomial sign-test p-value on discordant pairs."""
    if wins < 0 or losses < 0:
        raise ValueError(f"Discordant counts must be non-negative: wins={wins}, losses={losses}")
    return heldout.sign_test(wins, losses)


def newcombe_paired_ci(
    both_pass: int,
    wins: int,
    losses: int,
    both_fail: int,
    *,
    z: float = _Z_95,
) -> tuple[float | None, float | None, float | None]:
    """Return ``(delta, ci_low, ci_high)`` via Newcombe (1998) Method 10 paired 95% CI.

    Contingency cells for ``(treatment, reference)`` pairs:
      - ``both_pass``: (1, 1)
      - ``wins``:      (1, 0)
      - ``losses``:    (0, 1)
      - ``both_fail``: (0, 0)
    """
    if min(both_pass, wins, losses, both_fail) < 0:
        raise ValueError(
            "Paired contingency cells must be non-negative: "
            f"a={both_pass}, b={wins}, c={losses}, d={both_fail}"
        )
    n = both_pass + wins + losses + both_fail
    if n == 0:
        return None, None, None

    a = both_pass
    b = wins
    c = losses
    d = both_fail
    p1 = (a + b) / n
    p2 = (a + c) / n
    delta = p1 - p2

    l1, u1 = wilson_ci(a + b, n, z=z)
    l2, u2 = wilson_ci(a + c, n, z=z)
    assert l1 is not None and u1 is not None and l2 is not None and u2 is not None

    denom_sq = (a + b) * (c + d) * (a + c) * (b + d)
    if denom_sq == 0:
        phi = 0.0
    else:
        cross = a * d - b * c
        if cross > 0:
            phi = max(cross - n / 2.0, 0.0) / math.sqrt(denom_sq)
        else:
            phi = cross / math.sqrt(denom_sq)

    dl1 = p1 - l1
    du2 = u2 - p2
    du1 = u1 - p1
    dl2 = p2 - l2

    rad_low = max(0.0, dl1 * dl1 - 2.0 * phi * dl1 * du2 + du2 * du2)
    rad_high = max(0.0, du1 * du1 - 2.0 * phi * du1 * dl2 + dl2 * dl2)

    ci_low = max(-1.0, delta - math.sqrt(rad_low))
    ci_high = min(1.0, delta + math.sqrt(rad_high))
    return delta, ci_low, ci_high


def _binom_cdf(k: int, n: int, p: float) -> float:
    """Compute P(X <= k) for X ~ Binomial(n, p)."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    q = 1.0 - p
    return sum(math.comb(n, i) * (p**i) * (q ** (n - i)) for i in range(k + 1))


def exact_binomial_upper_bound(k: int, n: int, *, alpha: float = DEFAULT_ALPHA) -> float | None:
    """Return the exact one-sided ``(1 - alpha)`` upper confidence bound for ``k / n``."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"Invalid binomial counts: k={k}, n={n}")
    if n == 0:
        return None
    if k == n:
        return 1.0
    if k == 0:
        return 1.0 - (alpha ** (1.0 / n))
    lo, hi = k / n, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def clopper_pearson_ci(
    k: int,
    n: int,
    *,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[float | None, float | None]:
    """Return the exact two-sided ``(1 - alpha)`` Clopper-Pearson interval for ``k / n``."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"Invalid binomial counts: k={k}, n={n}")
    if n == 0:
        return None, None

    half_alpha = alpha / 2.0
    if k == 0:
        lower = 0.0
    else:
        lo, hi = 0.0, k / n
        for _ in range(80):
            mid = (lo + hi) / 2.0
            upper_tail = 1.0 - _binom_cdf(k - 1, n, mid)
            if upper_tail < half_alpha:
                lo = mid
            else:
                hi = mid
        lower = (lo + hi) / 2.0

    upper = exact_binomial_upper_bound(k, n, alpha=half_alpha)
    assert upper is not None
    return lower, upper


def paired_numeric_delta(
    records: Sequence[tuple[float | None, float | None, bool, bool]],
) -> dict[str, Any]:
    """Compute matched-pair numeric deltas while tracking missing and timeout counts.

    Each element of ``records`` is ``(treat_val, ref_val, treat_timeout, ref_timeout)``.
    """
    n_total = len(records)
    diffs: list[float] = []
    t_vals: list[float] = []
    r_vals: list[float] = []
    n_missing_t = 0
    n_missing_r = 0
    n_missing_either = 0
    n_timeout_t = 0
    n_timeout_r = 0
    n_timeout_either = 0

    for t_val, r_val, t_to, r_to in records:
        if t_to:
            n_timeout_t += 1
        if r_to:
            n_timeout_r += 1
        if t_to or r_to:
            n_timeout_either += 1
        if t_val is None:
            n_missing_t += 1
        if r_val is None:
            n_missing_r += 1
        if t_val is None or r_val is None:
            n_missing_either += 1
            continue
        tf = float(t_val)
        rf = float(r_val)
        t_vals.append(tf)
        r_vals.append(rf)
        diffs.append(tf - rf)

    n_matched = len(diffs)
    if n_matched == 0:
        return {
            "n_total_pairs": n_total,
            "n_matched_pairs": 0,
            "n_missing_treatment": n_missing_t,
            "n_missing_reference": n_missing_r,
            "n_missing_either": n_missing_either,
            "n_timeout_treatment": n_timeout_t,
            "n_timeout_reference": n_timeout_r,
            "n_timeout_either": n_timeout_either,
            "treatment_mean": None,
            "reference_mean": None,
            "mean_diff": None,
            "ci_low": None,
            "ci_high": None,
            "delta_pct": None,
        }

    mean_diff, ci_low, ci_high = heldout.paired_mean_diff_ci(diffs)
    t_sum = sum(t_vals)
    r_sum = sum(r_vals)
    delta_pct = ((t_sum - r_sum) / r_sum) * 100.0 if r_sum != 0.0 else None
    return {
        "n_total_pairs": n_total,
        "n_matched_pairs": n_matched,
        "n_missing_treatment": n_missing_t,
        "n_missing_reference": n_missing_r,
        "n_missing_either": n_missing_either,
        "n_timeout_treatment": n_timeout_t,
        "n_timeout_reference": n_timeout_r,
        "n_timeout_either": n_timeout_either,
        "treatment_mean": t_sum / n_matched,
        "reference_mean": r_sum / n_matched,
        "mean_diff": mean_diff,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "delta_pct": delta_pct,
    }


# ---------------------------------------------------------------------------
# Manifest verification & JSONL / sidecar loading
# ---------------------------------------------------------------------------


def _parse_preregistered_notes(notes: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for chunk in (notes or "").split(";"):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        parsed[key.strip()] = val.strip()
    return parsed


def load_and_verify_manifest(
    manifest_path: str | Path,
    *,
    require_frozen: bool = True,
) -> dict[str, Any]:
    """Load a replay manifest, verify ``manifest_sha256``, and extract case strata."""
    path = Path(manifest_path).resolve()
    manifest, _base_dir = replay_corpus.load_manifest(path)
    computed_sha = replay_corpus.compute_manifest_digest(manifest)
    prereg = manifest.preregistration
    declared_sha = prereg.manifest_sha256

    if declared_sha is not None and declared_sha != computed_sha:
        raise ManifestVerificationError(
            "Frozen manifest_sha256 mismatch: "
            f"declared={declared_sha}, computed={computed_sha}"
        )
    if require_frozen:
        if not prereg.frozen:
            raise ManifestVerificationError(
                f"Manifest {path} is not frozen (preregistration.frozen=false)"
            )
        if declared_sha is None:
            raise ManifestVerificationError(
                f"Manifest {path} is frozen=true but preregistration.manifest_sha256 is null"
            )

    cases_by_id: dict[str, dict[str, Any]] = {}
    category_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    difficulty_counts: Counter[str] = Counter()
    scope_counts: Counter[str] = Counter()

    for entry in manifest.cases:
        notes_meta = _parse_preregistered_notes(entry.preregistration.preregistered_notes)
        category = notes_meta.get("category") or "unknown"
        language = notes_meta.get("language") or "unknown"
        repo = notes_meta.get("repo") or entry.preregistration.source_origin
        cases_by_id[entry.case_id] = {
            "case_id": entry.case_id,
            "category": category,
            "language": language,
            "repo": repo,
            "difficulty": entry.preregistration.expected_difficulty,
            "file_scope": entry.preregistration.file_scope_category,
            "base_commit": entry.source.base_commit,
            "fix_commit": entry.source.fix_commit,
            "source_origin": entry.preregistration.source_origin,
        }
        category_counts[category] += 1
        language_counts[language] += 1
        difficulty_counts[entry.preregistration.expected_difficulty] += 1
        scope_counts[entry.preregistration.file_scope_category] += 1

    return {
        "manifest_path": str(path),
        "schema_version": manifest.schema_version,
        "preregistration_id": prereg.preregistration_id,
        "protocol_version": prereg.protocol_version,
        "registered_at": prereg.registered_at,
        "frozen": prereg.frozen,
        "declared_sha256": declared_sha,
        "computed_sha256": computed_sha,
        "sha256_verified": declared_sha == computed_sha and prereg.frozen,
        "case_count": len(manifest.cases),
        "category_counts": dict(category_counts),
        "language_counts": dict(language_counts),
        "difficulty_counts": dict(difficulty_counts),
        "scope_counts": dict(scope_counts),
        "cases_by_id": cases_by_id,
    }


def load_sidecar_metadata(jsonl_path: Path) -> dict[str, Any] | None:
    """Load the ``.meta.json`` sidecar adjacent to ``jsonl_path``, if present."""
    sidecar = jsonl_path.with_suffix(".meta.json")
    if not sidecar.is_file():
        return None
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        latest = raw.get("latest")
        if isinstance(latest, dict):
            return dict(latest)
        return dict(raw)
    return None


def detect_cymbal_version(
    *,
    explicit_version: str | None = None,
    sidecars: Sequence[Mapping[str, Any]] = (),
    probe_host: bool = True,
) -> str:
    """Resolve the cymbal version from CLI override, sidecar metadata, or host binary."""
    if explicit_version and explicit_version.strip():
        return explicit_version.strip()
    for meta in sidecars:
        if not isinstance(meta, Mapping):
            continue
        direct = meta.get("cymbal_version")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        controls = meta.get("controls")
        if isinstance(controls, Mapping):
            nested = controls.get("cymbal_version")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    if probe_host:
        binary = shutil.which("cymbal")
        if binary:
            try:
                proc = subprocess.run(
                    [binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                out = (proc.stdout or proc.stderr or "").strip()
                if proc.returncode == 0 and out:
                    return out.splitlines()[0].strip()
            except (OSError, subprocess.SubprocessError):
                pass
    return "unavailable"


def load_jsonl_rows_and_meta(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Load JSONL rows via ``heldout.load_results`` plus any adjacent ``.meta.json`` sidecar."""
    rows = heldout.load_results(path)
    sidecar = load_sidecar_metadata(path)
    return rows, sidecar


def classify_stage2_factor(raw: Mapping[str, Any]) -> str:
    """Classify a Stage 2 JSONL row into its one-factor-at-a-time ablation factor."""
    base_arm = heldout.normalize_arm(str(raw.get("arm") or "passive"))
    backend = str(raw.get("local_callers_backend") or "builtin").strip().lower()
    read_range = bool(raw.get("local_callers_read_range"))
    facts = bool(raw.get("project_facts"))

    toggles: list[str] = []
    if backend not in ("builtin", ""):
        toggles.append("cymbal" if backend == "cymbal" else f"backend={backend}")
    if read_range:
        toggles.append("read_range")
    if facts:
        toggles.append("facts")

    if base_arm != "passive":
        suffix = "+".join(toggles) if toggles else "default"
        return f"{base_arm}:{suffix}"
    if not toggles:
        return "passive_ref"
    if len(toggles) == 1:
        return toggles[0]
    return "+".join(toggles)


def normalize_row(
    raw: Mapping[str, Any],
    cases_by_id: Mapping[str, Mapping[str, Any]],
    *,
    stage: str,
    source_file: str,
) -> dict[str, Any]:
    """Normalize a raw JSONL trial row using authoritative manifest strata."""
    case_id = str(raw["case"])
    spec = str(raw.get("spec") or "vague")
    round_id = raw.get("round", 0)
    arm = heldout.normalize_arm(str(raw["arm"]))
    factor = classify_stage2_factor(raw) if stage == "stage2" else arm

    case_info = cases_by_id.get(case_id)
    in_manifest = case_info is not None
    category = str(case_info["category"]) if case_info else str(raw.get("kind") or "unknown")
    language = str(case_info["language"]) if case_info else str(raw.get("language") or "unknown")

    outcomes = heldout._extract_row_outcomes(dict(raw))
    hidden_passed = outcomes["hidden_passed"]
    if hidden_passed is None and raw.get("passed") is not None:
        hidden_passed = bool(raw["passed"])
    smoke_passed = outcomes["smoke_passed"]
    strict_passed = bool(raw["passed"]) if raw.get("passed") is not None else bool(hidden_passed)

    exit_code = raw.get("exit")
    commit_outcome = outcomes["commit_outcome"]
    hidden_outcome = outcomes["hidden_outcome"]
    smoke_outcome = raw.get("smoke_outcome")
    only_target_modified = raw.get("only_target_modified")

    is_timeout = (
        exit_code == 124
        or commit_outcome == "timeout"
        or hidden_outcome == "timeout"
        or smoke_outcome == "timeout"
    )
    is_out_of_scope = commit_outcome == "out_of_scope" or (
        only_target_modified is False
        and commit_outcome not in ("no_change", "timeout", "error")
    )

    verdicts = [str(v).upper() for v in (raw.get("audit_verdicts") or [])]
    audit_outcome = str(outcomes["audit_outcome"]).lower()
    audit_ran = bool(raw.get("audit_ran")) or audit_outcome != "not_run" or bool(verdicts)

    false_approval = bool(audit_outcome == "pass" and hidden_passed is False)
    if hidden_passed is None and raw.get("false_approval") is not None:
        false_approval = bool(raw["false_approval"])

    unknown_verdicts = max(
        int(raw.get("unknowns") or 0),
        verdicts.count("UNKNOWN"),
        1 if audit_outcome == "unknown" else 0,
    )
    unknown_final = audit_outcome == "unknown"
    audit_errors = max(
        int(raw.get("errors") or 0),
        verdicts.count("ERROR"),
        1 if audit_outcome == "error" else 0,
    )
    trial_error = raw.get("trial_error")
    is_error = (
        (exit_code is not None and exit_code not in (0, 124))
        or commit_outcome == "error"
        or audit_errors > 0
        or bool(trial_error)
    )

    tokens_measured = bool(outcomes["tokens_measured"])
    tokens = float(outcomes["tokens"]) if outcomes["tokens"] is not None else None
    first_prompt = (
        float(raw["first_editor_prompt_tokens"])
        if raw.get("first_editor_prompt_tokens") is not None
        else None
    )
    prompt_tokens = (
        float(outcomes["prompt_tokens"]) if outcomes["prompt_tokens"] is not None else None
    )
    completion_tokens = (
        float(outcomes["completion_tokens"])
        if outcomes["completion_tokens"] is not None
        else None
    )

    raw_calls_by_role = raw.get("calls_by_role")
    calls_by_role = raw_calls_by_role if isinstance(raw_calls_by_role, Mapping) else {}
    editor_calls_raw = outcomes["editor_calls"]
    if editor_calls_raw is None and "editor" in calls_by_role:
        editor_calls_raw = calls_by_role["editor"]
    editor_calls = int(editor_calls_raw) if editor_calls_raw is not None else None

    if raw.get("reflections") is not None:
        reflections: int | None = int(raw["reflections"])
    elif editor_calls is not None:
        reflections = max(0, editor_calls - 1)
    elif raw.get("extra_calls") is not None:
        reflections = int(raw["extra_calls"])
    else:
        reflections = None

    seconds = float(raw["seconds"]) if raw.get("seconds") is not None else None

    return {
        "case_id": case_id,
        "spec": spec,
        "round": round_id,
        "pair_key": (case_id, spec, round_id),
        "stage": stage,
        "arm": arm,
        "factor": factor,
        "category": category,
        "language": language,
        "in_manifest": in_manifest,
        "source_file": source_file,
        "model": raw.get("model"),
        "map_tokens": raw.get("map_tokens"),
        "exit": exit_code,
        "trial_error": str(trial_error) if trial_error else None,
        "hidden_passed": hidden_passed,
        "hidden_outcome": hidden_outcome,
        "smoke_passed": smoke_passed,
        "smoke_outcome": smoke_outcome,
        "strict_passed": strict_passed,
        "commit_passed": outcomes["commit_passed"],
        "commit_outcome": commit_outcome,
        "only_target_modified": only_target_modified,
        "is_timeout": is_timeout,
        "is_error": is_error,
        "is_out_of_scope": is_out_of_scope,
        "audit_ran": audit_ran,
        "audit_outcome": audit_outcome,
        "audit_verdicts": verdicts,
        "false_approval": false_approval,
        "unknown_final": unknown_final,
        "unknown_verdicts": unknown_verdicts,
        "audit_errors": audit_errors,
        "evidence": bool(raw.get("evidence")),
        "read_range_attached": bool(raw.get("read_range_attached")),
        "facts_attached": bool(raw.get("facts_attached")),
        "contract_rejected": bool(raw.get("contract_rejected")),
        "tokens_measured": tokens_measured,
        "tokens": tokens,
        "first_editor_prompt_tokens": first_prompt,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "editor_calls": editor_calls,
        "reflections": reflections,
        "extra_calls": int(raw["extra_calls"]) if raw.get("extra_calls") is not None else None,
        "seconds": seconds,
        "row_metadata": raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {},
    }


# ---------------------------------------------------------------------------
# Aggregation, pairing, control non-inferiority & shipping gate
# ---------------------------------------------------------------------------


def _mean_summary(values: Sequence[float | None], total_n: int) -> dict[str, Any]:
    measured = [float(v) for v in values if v is not None]
    n = len(measured)
    return {
        "mean": (sum(measured) / n) if n else None,
        "p50": heldout._percentile(measured, 50.0) if n else None,
        "p95": heldout._percentile(measured, 95.0) if n else None,
        "total": sum(measured) if n else None,
        "measured_n": n,
        "total_n": total_n,
    }


def compute_arm_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute standalone descriptive, safety, token, and latency metrics for an arm."""
    n = len(rows)
    hidden_known = [r for r in rows if r["hidden_passed"] is not None]
    hidden_n = len(hidden_known)
    hidden_pass = sum(1 for r in hidden_known if r["hidden_passed"])
    w_lo, w_hi = wilson_ci(hidden_pass, hidden_n)

    smoke_known = [r for r in rows if r["smoke_passed"] is not None]
    smoke_pass = sum(1 for r in smoke_known if r["smoke_passed"])
    strict_pass = sum(1 for r in rows if r["strict_passed"])
    commit_known = [r for r in rows if r["commit_passed"] is not None]
    commit_pass = sum(1 for r in commit_known if r["commit_passed"])

    audit_ran = sum(1 for r in rows if r["audit_ran"])
    audit_outcomes: Counter[str] = Counter(str(r["audit_outcome"]) for r in rows)
    false_approvals = sum(1 for r in rows if r["false_approval"])
    unknown_rows = sum(1 for r in rows if r["unknown_final"] or r["unknown_verdicts"] > 0)
    unknown_final_rows = sum(1 for r in rows if r["unknown_final"])
    unknown_verdicts = sum(int(r["unknown_verdicts"]) for r in rows)
    timeouts = sum(1 for r in rows if r["is_timeout"])
    errors = sum(1 for r in rows if r["is_error"])
    out_of_scope = sum(1 for r in rows if r["is_out_of_scope"])
    evidence_count = sum(1 for r in rows if r["evidence"])

    first_prompt = _mean_summary([r["first_editor_prompt_tokens"] for r in rows], n)
    cum_prompt = _mean_summary([r["prompt_tokens"] for r in rows], n)
    completion = _mean_summary([r["completion_tokens"] for r in rows], n)
    total_tok = _mean_summary([r["tokens"] for r in rows], n)
    editor_calls = _mean_summary(
        [float(r["editor_calls"]) if r["editor_calls"] is not None else None for r in rows],
        n,
    )
    reflections = _mean_summary(
        [float(r["reflections"]) if r["reflections"] is not None else None for r in rows],
        n,
    )
    latency = _mean_summary([r["seconds"] for r in rows], n)

    tokens_per_solved = (
        (total_tok["total"] / hidden_pass)
        if hidden_pass > 0 and total_tok["measured_n"] == n and total_tok["total"] is not None
        else None
    )

    return {
        "n": n,
        "hidden_n": hidden_n,
        "hidden_passed": hidden_pass,
        "hidden_pass_rate": (hidden_pass / hidden_n) if hidden_n else None,
        "wilson_ci_low": w_lo,
        "wilson_ci_high": w_hi,
        "smoke_n": len(smoke_known),
        "smoke_passed": smoke_pass,
        "strict_passed": strict_pass,
        "commit_n": len(commit_known),
        "commit_passed": commit_pass,
        "evidence_attached": evidence_count,
        "audit_ran": audit_ran,
        "audit_outcomes": dict(sorted(audit_outcomes.items())),
        "false_approval_count": false_approvals,
        "false_approval_rate": (false_approvals / audit_ran) if audit_ran else None,
        "false_approval_rate_all": (false_approvals / n) if n else None,
        "unknown_row_count": unknown_rows,
        "unknown_final_count": unknown_final_rows,
        "unknown_verdict_count": unknown_verdicts,
        "unknown_rate": (unknown_rows / audit_ran) if audit_ran else None,
        "unknown_final_rate": (unknown_final_rows / audit_ran) if audit_ran else None,
        "timeouts": timeouts,
        "errors": errors,
        "out_of_scope": out_of_scope,
        "first_prompt_tokens": first_prompt,
        "cumulative_prompt_tokens": cum_prompt,
        "completion_tokens": completion,
        "total_tokens": total_tok,
        "tokens_per_solved": tokens_per_solved,
        "editor_calls": editor_calls,
        "reflections": reflections,
        "latency_seconds": latency,
    }


def compute_paired_comparison(
    treat_by_key: Mapping[tuple[Any, ...], Mapping[str, Any]],
    ref_by_key: Mapping[tuple[Any, ...], Mapping[str, Any]],
    *,
    keys: Sequence[tuple[Any, ...]] | None = None,
) -> dict[str, Any]:
    """Compute paired binary (McNemar + Newcombe 95% CI) and matched-pair numeric deltas."""
    selected = set(keys) if keys is not None else set(treat_by_key) | set(ref_by_key)
    treatment_keys = selected.intersection(treat_by_key)
    reference_keys = selected.intersection(ref_by_key)
    shared_keys = sorted(treatment_keys & reference_keys)

    both_pass = wins = losses = both_fail = 0
    for key in shared_keys:
        t_row = treat_by_key[key]
        r_row = ref_by_key[key]
        t_ok = t_row["hidden_passed"]
        r_ok = r_row["hidden_passed"]
        if t_ok is None or r_ok is None:
            continue
        if t_ok and r_ok:
            both_pass += 1
        elif t_ok and not r_ok:
            wins += 1
        elif not t_ok and r_ok:
            losses += 1
        else:
            both_fail += 1

    n_pairs = both_pass + wins + losses + both_fail
    delta, ci_low, ci_high = newcombe_paired_ci(both_pass, wins, losses, both_fail)
    p_value = exact_mcnemar_p(wins, losses) if n_pairs else None
    treat_rate = ((both_pass + wins) / n_pairs) if n_pairs else None
    ref_rate = ((both_pass + losses) / n_pairs) if n_pairs else None

    def _pair_records(field: str) -> list[tuple[float | None, float | None, bool, bool]]:
        return [
            (
                treat_by_key[k][field],
                ref_by_key[k][field],
                bool(treat_by_key[k]["is_timeout"]),
                bool(ref_by_key[k]["is_timeout"]),
            )
            for k in shared_keys
        ]

    return {
        "n_pairs": n_pairs,
        "new_timeouts": sum(
            bool(treat_by_key[k]["is_timeout"]) and not ref_by_key[k]["is_timeout"]
            for k in shared_keys
        ),
        "treatment_only": len(treatment_keys - reference_keys),
        "reference_only": len(reference_keys - treatment_keys),
        "both_pass": both_pass,
        "wins": wins,
        "losses": losses,
        "both_fail": both_fail,
        "net_wins": wins - losses,
        "treatment_pass_rate": treat_rate,
        "reference_pass_rate": ref_rate,
        "delta": delta,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_value": p_value,
        "first_prompt_tokens": paired_numeric_delta(_pair_records("first_editor_prompt_tokens")),
        "cumulative_prompt_tokens": paired_numeric_delta(_pair_records("prompt_tokens")),
        "completion_tokens": paired_numeric_delta(_pair_records("completion_tokens")),
        "total_tokens": paired_numeric_delta(_pair_records("tokens")),
        "latency_seconds": paired_numeric_delta(_pair_records("seconds")),
    }


def evaluate_control_non_inferiority(
    treat_by_key: Mapping[tuple[Any, ...], Mapping[str, Any]],
    ref_by_key: Mapping[tuple[Any, ...], Mapping[str, Any]],
    control_keys: Sequence[tuple[Any, ...]],
    *,
    margin: float = DEFAULT_CONTROL_MARGIN,
) -> dict[str, Any]:
    """Evaluate control-task non-inferiority with exact power and upper-bound caveats."""
    paired = compute_paired_comparison(treat_by_key, ref_by_key, keys=control_keys)
    n_pairs = int(paired["n_pairs"])
    wins = int(paired["wins"])
    losses = int(paired["losses"])
    net_regressions = losses - wins
    delta = paired["delta"]
    ci_low = paired["ci_low"]
    ci_high = paired["ci_high"]

    shared_ctrl = [k for k in control_keys if k in treat_by_key and k in ref_by_key]
    ctrl_false_approvals = sum(1 for k in shared_ctrl if treat_by_key[k]["false_approval"])
    ctrl_errors = sum(1 for k in shared_ctrl if treat_by_key[k]["is_error"])
    ctrl_out_of_scope = sum(1 for k in shared_ctrl if treat_by_key[k]["is_out_of_scope"])
    ctrl_timeouts = sum(1 for k in shared_ctrl if treat_by_key[k]["is_timeout"])
    new_ctrl_timeouts = paired["new_timeouts"]
    catastrophic_failures = (
        ctrl_false_approvals + ctrl_errors + ctrl_out_of_scope + new_ctrl_timeouts
    )

    upper_95_one_sided = exact_binomial_upper_bound(losses, n_pairs, alpha=0.05)
    cp_low, cp_high = clopper_pearson_ci(losses, n_pairs, alpha=0.05)

    operational_pass = (
        n_pairs > 0
        and delta is not None
        and (delta + 1e-12) >= -margin
        and catastrophic_failures == 0
    )
    ci_non_inferior = ci_low is not None and (ci_low + 1e-12) >= -margin
    stat_sig_degradation = ci_high is not None and ci_high < 0.0

    if n_pairs == 0:
        power_note = (
            "No paired control tasks available; control non-inferiority cannot be evaluated."
        )
    else:
        ub_pct = f"{upper_95_one_sided * 100.0:.1f}%" if upper_95_one_sided is not None else "n/a"
        cp_str = (
            f"[{cp_low * 100.0:.1f}%, {cp_high * 100.0:.1f}%]"
            if cp_low is not None and cp_high is not None
            else "n/a"
        )
        if operational_pass:
            status_phrase = (
                f"within the preregistered <= {margin * 100.0:.0f}% operational control tolerance "
                f"(losses={losses}/{n_pairs}, net_regressions={net_regressions}/{n_pairs})"
            )
        else:
            status_phrase = (
                f"FAILED preregistered <= {margin * 100.0:.0f}% operational control tolerance "
                f"(losses={losses}/{n_pairs}, net_regressions={net_regressions}/{n_pairs}, "
                f"catastrophic={catastrophic_failures})"
            )
        power_note = (
            f"Control n={n_pairs}: {status_phrase}; exact one-sided 95% upper bound on control "
            f"regression rate is {ub_pct} (two-sided 95% Clopper-Pearson {cp_str}). "
            "Observing 0 regressions on n=10 is NOT proof of zero degradation "
            "(bounding true regression rate below 5% at 95% confidence with 0 losses "
            "requires n >= 59 independent control tasks)."
        )

    return {
        "n_pairs": n_pairs,
        "wins": wins,
        "losses": losses,
        "net_regressions": net_regressions,
        "delta": delta,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_value": paired["p_value"],
        "margin": margin,
        "one_sided_95_upper_bound": upper_95_one_sided,
        "clopper_pearson_95_low": cp_low,
        "clopper_pearson_95_high": cp_high,
        "control_false_approvals": ctrl_false_approvals,
        "control_errors": ctrl_errors,
        "control_out_of_scope": ctrl_out_of_scope,
        "control_timeouts": ctrl_timeouts,
        "new_control_timeouts": new_ctrl_timeouts,
        "catastrophic_failures": catastrophic_failures,
        "operational_pass": operational_pass,
        "ci_non_inferior": ci_non_inferior,
        "stat_sig_degradation": stat_sig_degradation,
        "power_limitation_note": power_note,
    }


def evaluate_shipping_gate(
    *,
    arm_name: str,
    stage: str,
    arm_metrics: Mapping[str, Any],
    required_pairs: int,
    frozen: bool,
    overall_paired: Mapping[str, Any],
    caller_paired: Mapping[str, Any],
    control_ni: Mapping[str, Any],
    alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """Emit an evidence-only OPT_IN / DEFAULT_ON / BLOCKED / INCOMPLETE shipping gate verdict."""
    false_approvals = int(arm_metrics["false_approval_count"])
    errors = int(arm_metrics["errors"])
    out_of_scope = int(arm_metrics["out_of_scope"])
    new_timeouts = int(overall_paired["new_timeouts"])
    safety_ok = (
        false_approvals == 0
        and errors == 0
        and out_of_scope == 0
        and new_timeouts == 0
    )

    n_pairs = int(overall_paired["n_pairs"])
    treatment_only = int(overall_paired.get("treatment_only", 0))
    reference_only = int(overall_paired.get("reference_only", 0))
    manifest_frozen = bool(frozen)
    coverage_complete = (
        required_pairs > 0
        and n_pairs == required_pairs
        and treatment_only == 0
        and reference_only == 0
    )

    control_operational_ok = bool(control_ni["operational_pass"])
    control_blocked = (not control_operational_ok) and (
        coverage_complete or int(control_ni.get("n_pairs", 0)) > 0
    )
    control_ci_ok = bool(control_ni["ci_non_inferior"])

    caller_delta = caller_paired.get("delta")
    caller_p = caller_paired.get("p_value")
    overall_delta = overall_paired.get("delta")
    overall_p = overall_paired.get("p_value")

    sig_caller_lift = (
        caller_delta is not None
        and caller_delta > 0.0
        and caller_p is not None
        and caller_p < alpha
    )
    sig_regression = (
        (
            caller_delta is not None
            and caller_delta < 0.0
            and caller_p is not None
            and caller_p < alpha
        )
        or (
            overall_delta is not None
            and overall_delta < 0.0
            and overall_p is not None
            and overall_p < alpha
        )
        or bool(control_ni["stat_sig_degradation"])
    )

    is_unseeded_facts = stage == "stage2" and arm_name == "facts"
    efficiency_lift = False
    if stage == "stage2" and not is_unseeded_facts:
        non_degrading_accuracy = (
            (caller_delta is None or caller_delta >= 0.0)
            and (overall_delta is None or overall_delta >= 0.0)
        )
        tok_stat = overall_paired.get("total_tokens") or {}
        lat_stat = overall_paired.get("latency_seconds") or {}
        tok_sig_drop = (
            tok_stat.get("delta_pct") is not None
            and tok_stat["delta_pct"] < 0.0
            and tok_stat.get("ci_high") is not None
            and tok_stat["ci_high"] < 0.0
        )
        lat_sig_drop = (
            lat_stat.get("delta_pct") is not None
            and lat_stat["delta_pct"] < 0.0
            and lat_stat.get("ci_high") is not None
            and lat_stat["ci_high"] < 0.0
        )
        efficiency_lift = non_degrading_accuracy and (tok_sig_drop or lat_sig_drop)

    empirical_lift_ok = (not is_unseeded_facts) and (sig_caller_lift or efficiency_lift)

    reasons: list[str] = []
    if not safety_ok:
        reasons.append(
            "Critical safety regression(s): "
            f"false_approvals={false_approvals}, errors={errors}, "
            f"out_of_scope={out_of_scope}, new_timeouts={new_timeouts}"
        )
    if control_blocked:
        reasons.append(
            "Control operational non-inferiority gate failed "
            f"(net_regressions={control_ni['net_regressions']}/{control_ni['n_pairs']}, "
            f"delta={_fmt_signed_pct(control_ni['delta'])}, "
            f"catastrophic={control_ni['catastrophic_failures']})"
        )
    if sig_regression:
        reasons.append(
            "Statistically significant regression observed "
            f"(caller_delta={_fmt_signed_pct(caller_delta)}, "
            f"overall_delta={_fmt_signed_pct(overall_delta)})"
        )
    if not manifest_frozen or not coverage_complete:
        reasons.append(
            f"Frozen manifest and complete paired coverage required "
            f"(frozen={manifest_frozen}); matched hidden outcomes "
            f"{n_pairs}/{required_pairs}, "
            f"treatment_only={treatment_only}, "
            f"reference_only={reference_only}."
        )

    if not safety_ok or control_blocked or sig_regression:
        verdict = "BLOCKED"
    elif not manifest_frozen or not coverage_complete:
        verdict = "INCOMPLETE"
    elif control_ci_ok and empirical_lift_ok:
        verdict = "DEFAULT_ON"
        reasons.append(
            "All gates met: zero critical safety regressions, control 95% CI within "
            f"-{control_ni['margin'] * 100.0:.0f}% margin "
            f"({_fmt_ci_pct(control_ni['ci_low'], control_ni['ci_high'])}), "
            "and statistically significant empirical lift."
        )
    else:
        verdict = "OPT_IN"
        if not control_ci_ok:
            reasons.append(
                "Control 95% paired CI "
                f"{_fmt_ci_pct(control_ni['ci_low'], control_ni['ci_high'])} "
                f"crosses -{control_ni['margin'] * 100.0:.0f}% non-inferiority margin "
                f"(n={control_ni['n_pairs']} underpowered for default-on proof)"
            )
        if is_unseeded_facts:
            reasons.append(
                "Unseeded replay corpus tests zero-regression/overhead for facts=on only; "
                "positive fact-retrieval lift must be established on seeded convention suites"
            )
        elif not empirical_lift_ok:
            reasons.append(
                "No statistically significant empirical lift on caller-dependent tasks "
                f"(caller_delta={_fmt_signed_pct(caller_delta)}, "
                f"p={_fmt_p(caller_p)})"
            )

    return {
        "arm": arm_name,
        "stage": stage,
        "verdict": verdict,
        "checks": {
            "frozen": manifest_frozen,
            "coverage_complete": coverage_complete,
            "safety_ok": safety_ok,
            "control_operational_ok": control_operational_ok,
            "control_ci_ok": control_ci_ok,
            "empirical_lift_ok": empirical_lift_ok,
            "sig_regression": sig_regression,
        },
        "reasons": reasons,
    }


def build_failure_catalog(
    model_label: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """List every observed failure, timeout, false approval, UNKNOWN verdict, or error."""
    catalog: list[dict[str, Any]] = []
    for r in rows:
        tags: list[str] = []
        if r["hidden_passed"] is False:
            tags.append("HIDDEN_FAIL")
        if r["smoke_passed"] is False:
            tags.append("SMOKE_FAIL")
        if r["is_timeout"]:
            tags.append("TIMEOUT")
        if r["false_approval"]:
            tags.append("FALSE_APPROVAL")
        if r["unknown_final"] or int(r["unknown_verdicts"]) > 0:
            tags.append("UNKNOWN_VERDICT")
        if r["is_error"]:
            tags.append("ERROR")
        if r["is_out_of_scope"]:
            tags.append("OUT_OF_SCOPE")
        if not tags:
            continue
        catalog.append(
            {
                "model_label": model_label,
                "stage": r["stage"],
                "arm": r["factor"] if r["stage"] == "stage2" else r["arm"],
                "case_id": r["case_id"],
                "spec": r["spec"],
                "round": r["round"],
                "category": r["category"],
                "language": r["language"],
                "tags": tags,
                "hidden_outcome": r["hidden_outcome"]
                or ("pass" if r["hidden_passed"] else "fail"),
                "smoke_outcome": r["smoke_outcome"]
                or (
                    "n/a"
                    if r["smoke_passed"] is None
                    else ("pass" if r["smoke_passed"] else "fail")
                ),
                "audit_outcome": r["audit_outcome"],
                "unknown_verdicts": int(r["unknown_verdicts"]),
                "commit_outcome": r["commit_outcome"] or "n/a",
                "exit": r["exit"],
                "trial_error": r.get("trial_error"),
                "seconds": r["seconds"],
            }
        )
    catalog.sort(
        key=lambda item: (
            item["model_label"],
            item["stage"],
            item["arm"],
            item["case_id"],
            item["spec"],
            str(item["round"]),
        )
    )
    return catalog


def _index_rows_by_key(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[Any, ...], Mapping[str, Any]], list[tuple[Any, ...]]]:
    indexed: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    duplicates: list[tuple[Any, ...]] = []
    for row in rows:
        key = row["pair_key"]
        if key in indexed:
            duplicates.append(key)
        indexed[key] = row
    return indexed, duplicates


def _ordered_categories(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    present = {str(r["category"]) for r in rows}
    return [c for c in CATEGORY_ORDER if c in present] + sorted(present - set(CATEGORY_ORDER))


def _ordered_languages(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    present = {str(r["language"]) for r in rows}
    return [lg for lg in LANGUAGE_ORDER if lg in present] + sorted(present - set(LANGUAGE_ORDER))


def _stratified_analysis(
    arms_order: Sequence[str],
    reference_arm: str,
    by_arm_key: Mapping[str, Mapping[tuple[Any, ...], Mapping[str, Any]]],
    key_to_meta: Mapping[tuple[Any, ...], tuple[str, str]],
    strata_values: Sequence[str],
    *,
    meta_index: int,
    include_caller_combined: bool = False,
) -> list[dict[str, Any]]:
    """Compute stratified standalone + paired tables by category or language."""
    ref_map = by_arm_key.get(reference_arm, {})
    results: list[dict[str, Any]] = []

    def _add_stratum(name: str, selected_keys: list[tuple[Any, ...]]) -> None:
        arm_rows: dict[str, dict[str, Any]] = {}
        for arm in arms_order:
            arm_map = by_arm_key.get(arm, {})
            subset = [arm_map[k] for k in selected_keys if k in arm_map]
            metrics = compute_arm_metrics(subset)
            paired = (
                None
                if arm == reference_arm
                else compute_paired_comparison(arm_map, ref_map, keys=selected_keys)
            )
            arm_rows[arm] = {
                "metrics": metrics,
                "paired": paired,
            }
        results.append(
            {
                "stratum": name,
                "n_keys": len(selected_keys),
                "arms": arm_rows,
            }
        )

    for value in strata_values:
        keys = sorted(k for k, meta in key_to_meta.items() if meta[meta_index] == value)
        _add_stratum(value, keys)
        if include_caller_combined and value == "indirect_caller":
            caller_keys = sorted(
                k
                for k, meta in key_to_meta.items()
                if meta[meta_index] in ("direct_caller", "indirect_caller")
            )
            if caller_keys:
                _add_stratum("caller_dependent", caller_keys)

    return results


def _collect_run_provenance(
    label: str,
    paths: Sequence[Path],
    sidecars: Sequence[tuple[Path, Mapping[str, Any] | None]],
    rows: Sequence[Mapping[str, Any]],
    cymbal_version: str,
) -> dict[str, Any]:
    """Summarize exact run provenance from sidecar ``.meta.json`` files and rows."""
    models: set[str] = set()
    commits: set[str] = set()
    dirty_flags: set[bool] = set()
    pythons: set[str] = set()
    seeds: set[Any] = set()
    map_tokens_set: set[Any] = set()
    suites: set[str] = set()
    timestamps: set[str] = set()
    sidecar_files: list[str] = []

    for path, meta in sidecars:
        if meta is None:
            continue
        sidecar_files.append(str(path.with_suffix(".meta.json")))
        if meta.get("model"):
            models.add(str(meta["model"]))
        if meta.get("git_commit"):
            commits.add(str(meta["git_commit"]))
        if meta.get("git_dirty") is not None:
            dirty_flags.add(bool(meta["git_dirty"]))
        if meta.get("python_version"):
            pythons.add(str(meta["python_version"]))
        if meta.get("seed") is not None:
            seeds.add(meta["seed"])
        if meta.get("map_tokens") is not None:
            map_tokens_set.add(meta["map_tokens"])
        if meta.get("suite"):
            suites.add(str(meta["suite"]))
        if meta.get("timestamp_utc"):
            timestamps.add(str(meta["timestamp_utc"]))

    for row in rows:
        if row.get("model"):
            models.add(str(row["model"]))
        if row.get("map_tokens") is not None:
            map_tokens_set.add(row["map_tokens"])
        rmeta = row.get("row_metadata") or {}
        if rmeta.get("git_commit"):
            commits.add(str(rmeta["git_commit"]))
        if rmeta.get("git_dirty") is not None:
            dirty_flags.add(bool(rmeta["git_dirty"]))
        if rmeta.get("python_version"):
            pythons.add(str(rmeta["python_version"]))
        if rmeta.get("seed") is not None:
            seeds.add(rmeta["seed"])
        if rmeta.get("suite"):
            suites.add(str(rmeta["suite"]))

    return {
        "label": label,
        "input_files": [str(p) for p in paths],
        "sidecar_files": sidecar_files,
        "models": sorted(models),
        "git_commits": sorted(commits),
        "git_dirty": sorted(dirty_flags),
        "python_versions": sorted(pythons),
        "seeds": sorted(seeds, key=str),
        "map_tokens": sorted(map_tokens_set, key=str),
        "suites": sorted(suites),
        "timestamps_utc": sorted(timestamps),
        "cymbal_version": cymbal_version,
        "total_rows": len(rows),
    }


def analyze_run(
    run_spec: ModelRunSpec,
    manifest_info: Mapping[str, Any],
    *,
    control_margin: float = DEFAULT_CONTROL_MARGIN,
    cymbal_version: str | None = None,
    probe_host_cymbal: bool = True,
) -> dict[str, Any]:
    """Analyze Stage 1 and Stage 2 JSONL results for a single model run."""
    cases_by_id = manifest_info["cases_by_id"]

    stage1_rows: list[dict[str, Any]] = []
    stage2_rows: list[dict[str, Any]] = []
    sidecars: list[tuple[Path, dict[str, Any] | None]] = []

    for path in run_spec.stage1_paths:
        raw_rows, meta = load_jsonl_rows_and_meta(path)
        sidecars.append((path, meta))
        for raw in raw_rows:
            stage1_rows.append(
                normalize_row(raw, cases_by_id, stage="stage1", source_file=str(path))
            )

    for path in run_spec.stage2_paths:
        raw_rows, meta = load_jsonl_rows_and_meta(path)
        sidecars.append((path, meta))
        for raw in raw_rows:
            stage2_rows.append(
                normalize_row(raw, cases_by_id, stage="stage2", source_file=str(path))
            )

    all_rows = stage1_rows + stage2_rows
    resolved_cymbal = detect_cymbal_version(
        explicit_version=cymbal_version,
        sidecars=[m for _, m in sidecars if m is not None],
        probe_host=probe_host_cymbal,
    )
    provenance = _collect_run_provenance(
        run_spec.label,
        list(run_spec.stage1_paths) + list(run_spec.stage2_paths),
        sidecars,
        all_rows,
        resolved_cymbal,
    )

    warnings: list[str] = []
    unmatched_cases = sorted({r["case_id"] for r in all_rows if not r["in_manifest"]})
    if unmatched_cases:
        raise ManifestVerificationError(
            f"Found {len(unmatched_cases)} case_id(s) not present in manifest: "
            + ", ".join(unmatched_cases)
        )

    def required_pairs(treatment, reference):
        rounds = {(spec, round_id) for _, spec, round_id in treatment.keys() | reference.keys()}
        return len(cases_by_id) * len(rounds)

    # --- Stage 1 analysis ---
    s1_rows_by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in stage1_rows:
        s1_rows_by_arm[row["arm"]].append(row)

    s1_arms = [a for a in STAGE1_ARM_ORDER if a in s1_rows_by_arm] + sorted(
        set(s1_rows_by_arm.keys()) - set(STAGE1_ARM_ORDER)
    )
    s1_by_arm_key: dict[str, dict[tuple[Any, ...], Mapping[str, Any]]] = {}
    for arm in s1_arms:
        indexed, dups = _index_rows_by_key(s1_rows_by_arm[arm])
        s1_by_arm_key[arm] = indexed
        if dups:
            warnings.append(
                f"Stage 1 arm {arm!r} had {len(dups)} duplicate (case, spec, round) row(s); "
                "kept last occurrence."
            )

    s1_key_meta: dict[tuple[Any, ...], tuple[str, str]] = {
        r["pair_key"]: (r["category"], r["language"]) for r in stage1_rows
    }
    s1_all_keys = sorted(s1_key_meta.keys())
    s1_caller_keys = sorted(
        k
        for k, (cat, _lang) in s1_key_meta.items()
        if cat in ("direct_caller", "indirect_caller")
    )
    s1_control_keys = sorted(
        k for k, (cat, _lang) in s1_key_meta.items() if cat == "control"
    )

    s1_ref_arm = "baseline"
    s1_ref_map = s1_by_arm_key.get(s1_ref_arm, {})

    s1_table: list[dict[str, Any]] = []
    s1_control_ni: dict[str, dict[str, Any]] = {}
    s1_gates: list[dict[str, Any]] = []
    for arm in s1_arms:
        arm_map = s1_by_arm_key[arm]
        metrics = compute_arm_metrics(list(arm_map.values()))
        paired_vs_ref = (
            None
            if arm == s1_ref_arm
            else compute_paired_comparison(arm_map, s1_ref_map, keys=s1_all_keys)
        )
        caller_paired = (
            None
            if arm == s1_ref_arm
            else compute_paired_comparison(arm_map, s1_ref_map, keys=s1_caller_keys)
        )
        s1_table.append(
            {
                "arm": arm,
                "is_reference": arm == s1_ref_arm,
                "reference_arm": s1_ref_arm,
                "metrics": metrics,
                "paired": paired_vs_ref,
                "caller_paired": caller_paired,
            }
        )
        if arm != s1_ref_arm and s1_ref_map:
            assert paired_vs_ref is not None and caller_paired is not None
            ctrl_ni = evaluate_control_non_inferiority(
                arm_map,
                s1_ref_map,
                s1_control_keys,
                margin=control_margin,
            )
            s1_control_ni[arm] = ctrl_ni
            s1_gates.append(
                evaluate_shipping_gate(
                    arm_name=arm,
                    stage="stage1",
                    arm_metrics=metrics,
                    required_pairs=required_pairs(arm_map, s1_ref_map),
                    frozen=manifest_info["sha256_verified"],
                    overall_paired=paired_vs_ref,
                    caller_paired=caller_paired,
                    control_ni=ctrl_ni,
                )
            )

    s1_categories = _ordered_categories(stage1_rows)
    s1_languages = _ordered_languages(stage1_rows)
    s1_by_category = (
        _stratified_analysis(
            s1_arms,
            s1_ref_arm,
            s1_by_arm_key,
            s1_key_meta,
            s1_categories,
            meta_index=0,
            include_caller_combined=True,
        )
        if s1_arms
        else []
    )
    s1_by_language = (
        _stratified_analysis(
            s1_arms,
            s1_ref_arm,
            s1_by_arm_key,
            s1_key_meta,
            s1_languages,
            meta_index=1,
            include_caller_combined=False,
        )
        if s1_arms
        else []
    )

    # --- Stage 2 analysis (one-factor-at-a-time ablations vs Stage 1 passive) ---
    s2_rows_by_factor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in stage2_rows:
        s2_rows_by_factor[row["factor"]].append(row)

    # Build passive reference: prefer Stage 1 passive, fill missing keys from Stage 2 passive_ref
    s2_ref_map: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    if "passive_ref" in s2_rows_by_factor:
        s2_passive_idx, _ = _index_rows_by_key(s2_rows_by_factor["passive_ref"])
        s2_ref_map.update(s2_passive_idx)
    if "passive" in s1_by_arm_key:
        s2_ref_map.update(s1_by_arm_key["passive"])

    s2_factor_keys = [f for f in s2_rows_by_factor.keys() if f != "passive_ref"]
    s2_factors = [f for f in STAGE2_FACTOR_ORDER if f in s2_factor_keys] + sorted(
        set(s2_factor_keys) - set(STAGE2_FACTOR_ORDER)
    )

    s2_by_arm_key: dict[str, dict[tuple[Any, ...], Mapping[str, Any]]] = {}
    if s2_ref_map:
        s2_by_arm_key["passive"] = s2_ref_map
    for factor in s2_factors:
        indexed, dups = _index_rows_by_key(s2_rows_by_factor[factor])
        s2_by_arm_key[factor] = indexed
        if dups:
            warnings.append(
                f"Stage 2 factor {factor!r} had {len(dups)} duplicate (case, spec, round) row(s); "
                "kept last occurrence."
            )

    s2_key_meta: dict[tuple[Any, ...], tuple[str, str]] = {}
    for row in stage2_rows:
        s2_key_meta[row["pair_key"]] = (row["category"], row["language"])
    for key, row in s2_ref_map.items():
        s2_key_meta.setdefault(key, (row["category"], row["language"]))

    s2_all_keys = sorted(s2_key_meta.keys())
    s2_caller_keys = sorted(
        k
        for k, (cat, _lang) in s2_key_meta.items()
        if cat in ("direct_caller", "indirect_caller")
    )
    s2_control_keys = sorted(
        k for k, (cat, _lang) in s2_key_meta.items() if cat == "control"
    )

    s2_ref_metrics = compute_arm_metrics(list(s2_ref_map.values())) if s2_ref_map else {}
    s2_table: list[dict[str, Any]] = []
    s2_control_ni: dict[str, dict[str, Any]] = {}
    s2_gates: list[dict[str, Any]] = []
    s2_display_arms = (["passive"] if s2_ref_map else []) + s2_factors

    if s2_factors:
        if s2_ref_map:
            s2_table.append(
                {
                    "factor": "passive",
                    "display_label": "passive (Stage 1 ref)",
                    "is_reference": True,
                    "reference_arm": "passive",
                    "metrics": s2_ref_metrics,
                    "paired": None,
                    "caller_paired": None,
                }
            )
        for factor in s2_factors:
            f_map = s2_by_arm_key[factor]
            metrics = compute_arm_metrics(list(f_map.values()))
            paired_vs_passive = (
                compute_paired_comparison(f_map, s2_ref_map, keys=s2_all_keys)
                if s2_ref_map
                else None
            )
            caller_paired = (
                compute_paired_comparison(f_map, s2_ref_map, keys=s2_caller_keys)
                if s2_ref_map
                else None
            )
            s2_table.append(
                {
                    "factor": factor,
                    "display_label": STAGE2_FACTOR_LABELS.get(factor, factor),
                    "is_reference": False,
                    "reference_arm": "passive",
                    "metrics": metrics,
                    "paired": paired_vs_passive,
                    "caller_paired": caller_paired,
                }
            )
            if s2_ref_map and paired_vs_passive is not None and caller_paired is not None:
                ctrl_ni = evaluate_control_non_inferiority(
                    f_map,
                    s2_ref_map,
                    s2_control_keys,
                    margin=control_margin,
                )
                s2_control_ni[factor] = ctrl_ni
                s2_gates.append(
                    evaluate_shipping_gate(
                        arm_name=factor,
                        stage="stage2",
                        arm_metrics=metrics,
                        required_pairs=required_pairs(f_map, s2_ref_map),
                        frozen=manifest_info["sha256_verified"],
                        overall_paired=paired_vs_passive,
                        caller_paired=caller_paired,
                        control_ni=ctrl_ni,
                    )
                )

    s2_strata_rows = list(s2_ref_map.values()) + [
        r for f in s2_factors for r in s2_by_arm_key[f].values()
    ]
    s2_categories = _ordered_categories(s2_strata_rows)
    s2_languages = _ordered_languages(s2_strata_rows)
    s2_by_category = (
        _stratified_analysis(
            s2_display_arms,
            "passive",
            s2_by_arm_key,
            s2_key_meta,
            s2_categories,
            meta_index=0,
            include_caller_combined=True,
        )
        if s2_factors and s2_ref_map
        else []
    )
    s2_by_language = (
        _stratified_analysis(
            s2_display_arms,
            "passive",
            s2_by_arm_key,
            s2_key_meta,
            s2_languages,
            meta_index=1,
            include_caller_combined=False,
        )
        if s2_factors and s2_ref_map
        else []
    )

    failure_catalog = build_failure_catalog(run_spec.label, all_rows)

    return {
        "label": run_spec.label,
        "provenance": provenance,
        "warnings": warnings,
        "stage1": {
            "reference_arm": s1_ref_arm,
            "arms": s1_arms,
            "table": s1_table,
            "by_category": s1_by_category,
            "by_language": s1_by_language,
            "control_non_inferiority": s1_control_ni,
        },
        "stage2": {
            "reference_arm": "passive",
            "factors": s2_factors,
            "has_facts_factor": "facts" in s2_factors,
            "table": s2_table,
            "by_category": s2_by_category,
            "by_language": s2_by_language,
            "control_non_inferiority": s2_control_ni,
        },
        "failure_catalog": failure_catalog,
        "shipping_gates": {
            "stage1": s1_gates,
            "stage2": s2_gates,
        },
    }


def _aggregate_shipping_gates(model_reports: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate shipping gate verdicts conservatively across all evaluated models."""
    grouped: dict[tuple[str, str], list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    order: list[tuple[str, str]] = []
    for rep in model_reports:
        label = str(rep["label"])
        for stage_key in ("stage1", "stage2"):
            for gate in rep["shipping_gates"][stage_key]:
                pair = (stage_key, str(gate["arm"]))
                if pair not in grouped:
                    order.append(pair)
                grouped[pair].append((label, gate))

    summary: list[dict[str, Any]] = []
    for stage_key, arm_name in order:
        entries = grouped[(stage_key, arm_name)]
        verdicts = {label: str(gate["verdict"]) for label, gate in entries}
        if any(v == "BLOCKED" for v in verdicts.values()):
            overall = "BLOCKED"
        elif "INCOMPLETE" in verdicts.values():
            overall = "INCOMPLETE"
        elif verdicts and all(v == "DEFAULT_ON" for v in verdicts.values()):
            overall = "DEFAULT_ON"
        else:
            overall = "OPT_IN"
        summary.append(
            {
                "stage": stage_key,
                "arm": arm_name,
                "overall_verdict": overall,
                "per_model_verdicts": verdicts,
                "reasons": [
                    f"{label}: {reason}"
                    for label, gate in entries
                    for reason in gate["reasons"]
                ],
            }
        )
    return summary


def analyze_report(
    manifest_path: str | Path,
    runs: Sequence[ModelRunSpec],
    *,
    require_frozen: bool = True,
    control_margin: float = DEFAULT_CONTROL_MARGIN,
    cymbal_version: str | None = None,
    probe_host_cymbal: bool = True,
) -> dict[str, Any]:
    """Run full verified C2 statistical analysis across one or more model runs."""
    manifest_info = load_and_verify_manifest(manifest_path, require_frozen=require_frozen)
    model_reports = [
        analyze_run(
            run_spec,
            manifest_info,
            control_margin=control_margin,
            cymbal_version=cymbal_version,
            probe_host_cymbal=probe_host_cymbal,
        )
        for run_spec in runs
    ]
    all_failures = [
        entry for rep in model_reports for entry in rep["failure_catalog"]
    ]
    overall_gates = _aggregate_shipping_gates(model_reports)
    return {
        "schema_version": "c2-report-1.0",
        "methodological_grounding": {
            "train_test_separation": (
                "ir-textbook p. 320 (passage 069dc983a616649ad3adc765): "
                "strict separation of dev tuning from frozen unseen test corpus."
            ),
            "stratified_evaluation": (
                "beir-guide lines 414-438 (passage 90905b05c622807ee5f8a78d): "
                "report heterogeneous benchmarks stratified by task category and language "
                "with paired CIs rather than collapsing into one opaque average."
            ),
        },
        "manifest": {k: v for k, v in manifest_info.items() if k != "cases_by_id"},
        "control_margin": control_margin,
        "models": model_reports,
        "failure_catalog": all_failures,
        "overall_shipping_gates": overall_gates,
    }


# ---------------------------------------------------------------------------
# Markdown formatting helpers
# ---------------------------------------------------------------------------


def _fmt_rate(passed: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{passed}/{total} ({(passed / total) * 100.0:.1f}%)"


def _fmt_wilson(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "n/a"
    return f"[{lo * 100.0:.1f}%, {hi * 100.0:.1f}%]"


def _fmt_signed_pct(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val * 100.0:+.1f}pp"


def _fmt_ci_pct(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "n/a"
    return f"[{lo * 100.0:+.1f}pp, {hi * 100.0:+.1f}pp]"


def _fmt_p(p_val: float | None) -> str:
    if p_val is None:
        return "n/a"
    return f"{p_val:.4f}"


def _fmt_num(val: float | None, precision: int = 1) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{precision}f}"


def _fmt_measured_mean(stat: Mapping[str, Any], precision: int = 0) -> str:
    mean = stat.get("mean")
    m = int(stat.get("measured_n") or 0)
    t = int(stat.get("total_n") or 0)
    if mean is None:
        return f"n/a (0/{t})"
    suffix = f" ({m}/{t})" if m < t else ""
    return f"{mean:.{precision}f}{suffix}"


def _fmt_paired_pct_delta(stat: Mapping[str, Any] | None) -> str:
    if stat is None:
        return "ref"
    m = int(stat["n_matched_pairs"])
    t = int(stat["n_total_pairs"])
    timeouts = int(stat["n_timeout_either"])
    pct = stat["delta_pct"]
    if pct is None:
        return f"n/a ({m}/{t})"
    notes = [f"{m}/{t}"]
    if timeouts > 0:
        notes.append(f"to={timeouts}")
    return f"{pct:+.1f}% ({', '.join(notes)})"


def _fmt_audit_rate(count: int, audit_ran: int) -> str:
    if audit_ran == 0:
        return f"{count}/0 (n/a)"
    return f"{count}/{audit_ran} ({(count / audit_ran) * 100.0:.1f}%)"


def _fmt_paired_wl(paired: Mapping[str, Any] | None) -> str:
    if paired is None:
        return "ref"
    treat_only = int(paired.get("treatment_only") or 0)
    ref_only = int(paired.get("reference_only") or 0)
    if treat_only > 0 or ref_only > 0:
        return (
            f"+{paired['wins']}/-{paired['losses']} "
            f"(n={paired['n_pairs']}, treat_only={treat_only}, ref_only={ref_only})"
        )
    return f"+{paired['wins']}/-{paired['losses']} (n={paired['n_pairs']})"


def _fmt_timeouts(metrics: Mapping[str, Any], paired: Mapping[str, Any] | None) -> str:
    total = int(metrics["timeouts"])
    if paired is None:
        return str(total)
    new_to = int(paired.get("new_timeouts") or 0)
    return f"{total} (new={new_to})"


def _append_pairing_notes(
    lines: list[str],
    table_rows: Sequence[Mapping[str, Any]],
    *,
    label_key: str,
) -> None:
    unmatched_notes: list[str] = []
    timeout_notes: list[str] = []
    for row in table_rows:
        paired = row.get("paired")
        if paired is None:
            continue
        name = row[label_key]
        treat_only = int(paired.get("treatment_only") or 0)
        ref_only = int(paired.get("reference_only") or 0)
        new_to = int(paired.get("new_timeouts") or 0)
        if treat_only > 0 or ref_only > 0:
            unmatched_notes.append(
                f"`{name}` (treatment_only={treat_only}, reference_only={ref_only})"
            )
        if new_to > 0:
            timeout_notes.append(f"`{name}` (new_timeouts={new_to})")
    if unmatched_notes:
        lines.append(f"> **Unmatched Attempts:** {', '.join(unmatched_notes)}")
        lines.append("")
    if timeout_notes:
        lines.append(f"> **Matched-Pair Timeout Regressions:** {', '.join(timeout_notes)}")
        lines.append("")


def _render_stratified_table(
    lines: list[str],
    title: str,
    strata: Sequence[Mapping[str, Any]],
    arms_order: Sequence[str],
    reference_arm: str,
    label_map: Mapping[str, str] | None = None,
) -> None:
    lines.append(f"#### {title}")
    lines.append("")
    if not strata:
        lines.append("_No stratified data available._")
        lines.append("")
        return

    header = ["Stratum", "n"]
    for arm in arms_order:
        disp = label_map.get(arm, arm) if label_map else arm
        if arm == reference_arm:
            header.append(f"`{disp}` (ref)")
        else:
            header.append(f"`{disp}` (pass; +w/-l, p)")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---", "---:"] + ["---"] * len(arms_order)) + " |")

    for entry in strata:
        row_cells = [f"`{entry['stratum']}`", str(entry["n_keys"])]
        for arm in arms_order:
            cell = entry["arms"][arm]
            m = cell["metrics"]
            rate_str = _fmt_rate(m["hidden_passed"], m["hidden_n"])
            paired = cell["paired"]
            if paired is None:
                row_cells.append(rate_str)
            else:
                row_cells.append(
                    f"{rate_str}; +{paired['wins']}/-{paired['losses']} "
                    f"({_fmt_signed_pct(paired['delta'])}, p={_fmt_p(paired['p_value'])})"
                )
        lines.append("| " + " | ".join(row_cells) + " |")
    lines.append("")


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the complete Issue #5 Section C2 stratified statistical report as Markdown."""
    manifest = report["manifest"]
    lines: list[str] = []

    lines.append("# Issue #5 Section C2 — Stratified Replay Evaluation Report")
    lines.append("")
    lines.append(
        "> **Methodological Grounding:** Strict separation of development tuning from the frozen "
        "unseen test corpus (`ir-textbook`, p. 320, `069dc983a616649ad3adc765`), and mandatory "
        "stratified reporting by task category and language rather than collapsing heterogeneous "
        "strata into one opaque average (`beir-guide`, lines 414-438, `90905b05c622807ee5f8a78d`)."
    )
    lines.append("")

    # 1. Manifest verification
    lines.append("## 1. Frozen Manifest Verification")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| **Manifest Path** | `{manifest['manifest_path']}` |")
    lines.append(f"| **Preregistration ID** | `{manifest['preregistration_id']}` |")
    lines.append(f"| **Protocol Version** | `{manifest['protocol_version']}` |")
    lines.append(f"| **Registered At** | `{manifest['registered_at']}` |")
    lines.append(f"| **Frozen** | `{str(manifest['frozen']).lower()}` |")
    lines.append(f"| **Declared `manifest_sha256`** | `{manifest['declared_sha256']}` |")
    lines.append(f"| **Computed `manifest_sha256`** | `{manifest['computed_sha256']}` |")
    lines.append(
        f"| **Digest Verified** | `{'PASS' if manifest['sha256_verified'] else 'UNVERIFIED'}` |"
    )
    lines.append(f"| **Total Tasks** | `{manifest['case_count']}` |")
    cat_summary = ", ".join(
        f"{c}={manifest['category_counts'].get(c, 0)}"
        for c in CATEGORY_ORDER
        if c in manifest["category_counts"]
    )
    lang_summary = ", ".join(
        f"{lg}={manifest['language_counts'].get(lg, 0)}"
        for lg in LANGUAGE_ORDER
        if lg in manifest["language_counts"]
    )
    lines.append(f"| **Category Balance** | `{cat_summary}` |")
    lines.append(f"| **Language Balance** | `{lang_summary}` |")
    lines.append("")

    # 2. Run metadata
    lines.append("## 2. Run Metadata & Provenance")
    lines.append("")
    lines.append(
        "| Run Label | Model | Git Commit | Python | Seed | `map_tokens` | Cymbal | Rows |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | ---: |")
    for mrep in report["models"]:
        prov = mrep["provenance"]
        model_str = ", ".join(prov["models"]) or "n/a"
        commit_str = ", ".join(c[:12] for c in prov["git_commits"]) or "n/a"
        if True in prov["git_dirty"]:
            commit_str += " (dirty)"
        py_str = ", ".join(prov["python_versions"]) or "n/a"
        seed_str = ", ".join(str(s) for s in prov["seeds"]) or "n/a"
        map_str = ", ".join(str(mt) for mt in prov["map_tokens"]) or "n/a"
        lines.append(
            f"| `{prov['label']}` | `{model_str}` | `{commit_str}` | `{py_str}` | "
            f"`{seed_str}` | `{map_str}` | `{prov['cymbal_version']}` | {prov['total_rows']} |"
        )
    lines.append("")

    # 3. Per-model sections
    for idx, mrep in enumerate(report["models"], start=1):
        label = mrep["label"]
        lines.append(f"## 3.{idx}. Model Evaluation: `{label}`")
        lines.append("")
        if mrep["warnings"]:
            for warn in mrep["warnings"]:
                lines.append(f"> **Warning:** {warn}")
            lines.append("")

        # Stage 1 table
        s1 = mrep["stage1"]
        lines.append(
            "### Stage 1 — Five Context/Audit Arms (`builtin`, `read-range=off`, `facts=off`)"
        )
        lines.append("")
        if not s1["table"]:
            lines.append("_No Stage 1 JSONL rows provided._")
            lines.append("")
        else:
            lines.append(
                "_Aggregate 30-task metrics are descriptive context only; interpret jointly "
                "with the stratified Category and Language tables below._"
            )
            lines.append("")
            lines.append(
                "| Arm | Hidden Pass | Wilson 95% CI | Paired vs Ref (+w/-l) | "
                "Diff (Newcombe 95% CI) | McNemar p | False Approvals | UNKNOWN Verdicts | "
                "Mean 1st-Prompt | Mean Cum-Prompt | Mean Compl | Mean Calls / Refl | "
                "Latency p50 / p95 (s) | Timeouts (New) |"
            )
            lines.append(
                "| --- | --- | --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | "
                "---: | ---: | ---: |"
            )
            for row in s1["table"]:
                m = row["metrics"]
                p = row["paired"]
                paired_wl = _fmt_paired_wl(p)
                paired_ci = (
                    "ref"
                    if p is None
                    else f"{_fmt_signed_pct(p['delta'])} {_fmt_ci_pct(p['ci_low'], p['ci_high'])}"
                )
                p_str = "ref" if p is None else _fmt_p(p["p_value"])
                fa_str = _fmt_audit_rate(m["false_approval_count"], m["audit_ran"])
                unk_str = _fmt_audit_rate(m["unknown_row_count"], m["audit_ran"])
                if m["unknown_verdict_count"] > m["unknown_row_count"]:
                    unk_str += f" [{m['unknown_verdict_count']}v]"
                calls_refl = (
                    f"{_fmt_num(m['editor_calls']['mean'], 2)} / "
                    f"{_fmt_num(m['reflections']['mean'], 2)}"
                )
                lat_p50_p95 = (
                    f"{_fmt_num(m['latency_seconds']['p50'], 1)} / "
                    f"{_fmt_num(m['latency_seconds']['p95'], 1)}"
                )
                lines.append(
                    f"| `{row['arm']}` | {_fmt_rate(m['hidden_passed'], m['hidden_n'])} | "
                    f"{_fmt_wilson(m['wilson_ci_low'], m['wilson_ci_high'])} | "
                    f"{paired_wl} | {paired_ci} | {p_str} | {fa_str} | {unk_str} | "
                    f"{_fmt_measured_mean(m['first_prompt_tokens'])} | "
                    f"{_fmt_measured_mean(m['cumulative_prompt_tokens'])} | "
                    f"{_fmt_measured_mean(m['completion_tokens'])} | "
                    f"{calls_refl} | {lat_p50_p95} | {_fmt_timeouts(m, p)} |"
                )
            lines.append("")
            _append_pairing_notes(lines, s1["table"], label_key="arm")

            _render_stratified_table(
                lines,
                "Stage 1 — Stratified by Category (Never Collapsing Heterogeneous Strata)",
                s1["by_category"],
                s1["arms"],
                s1["reference_arm"],
            )
            _render_stratified_table(
                lines,
                "Stage 1 — Stratified by Language (All Covered Languages)",
                s1["by_language"],
                s1["arms"],
                s1["reference_arm"],
            )

        # Stage 2 table
        s2 = mrep["stage2"]
        lines.append("### Stage 2 — One-Factor-at-a-Time Ablations vs. Stage 1 `passive`")
        lines.append("")
        if not s2["factors"]:
            lines.append("_No Stage 2 ablation JSONL rows provided._")
            lines.append("")
        else:
            if s2["has_facts_factor"]:
                lines.append(
                    "> **Scope Note (`facts=on`):** Unseeded replay repositories have no "
                    "pre-seeded facts store (`seed_facts=()`), so `facts=on` here measures "
                    "zero-regression and empty-store token/latency overhead only. Positive "
                    "fact-retrieval lift is evaluated separately on seeded convention suites."
                )
                lines.append("")
            lines.append(
                "| Factor | Hidden Pass | Wilson 95% CI | Paired vs `passive` (+w/-l) | "
                "Diff (Newcombe 95% CI) | McNemar p | Paired 1st-Prompt Delta | "
                "Paired Total-Tok Delta | Paired Latency Delta | Latency p50 / p95 (s) | "
                "Timeouts (New) |"
            )
            lines.append(
                "| --- | --- | --- | --- | --- | ---: | --- | --- | --- | ---: | ---: |"
            )
            for row in s2["table"]:
                m = row["metrics"]
                p = row["paired"]
                paired_wl = _fmt_paired_wl(p)
                paired_ci = (
                    "ref"
                    if p is None
                    else f"{_fmt_signed_pct(p['delta'])} {_fmt_ci_pct(p['ci_low'], p['ci_high'])}"
                )
                p_str = "ref" if p is None else _fmt_p(p["p_value"])
                d_first = _fmt_paired_pct_delta(p["first_prompt_tokens"] if p else None)
                d_tok = _fmt_paired_pct_delta(p["total_tokens"] if p else None)
                d_lat = _fmt_paired_pct_delta(p["latency_seconds"] if p else None)
                lat_p50_p95 = (
                    f"{_fmt_num(m['latency_seconds']['p50'], 1)} / "
                    f"{_fmt_num(m['latency_seconds']['p95'], 1)}"
                )
                lines.append(
                    f"| `{row['display_label']}` | "
                    f"{_fmt_rate(m['hidden_passed'], m['hidden_n'])} | "
                    f"{_fmt_wilson(m['wilson_ci_low'], m['wilson_ci_high'])} | "
                    f"{paired_wl} | {paired_ci} | {p_str} | {d_first} | {d_tok} | {d_lat} | "
                    f"{lat_p50_p95} | {_fmt_timeouts(m, p)} |"
                )
            lines.append("")
            _append_pairing_notes(lines, s2["table"], label_key="display_label")

            has_s2_ref = any(r["is_reference"] for r in s2["table"])
            s2_arms_order = (["passive"] if has_s2_ref else []) + s2["factors"]
            _render_stratified_table(
                lines,
                "Stage 2 — Stratified by Category",
                s2["by_category"],
                s2_arms_order,
                "passive",
                label_map=STAGE2_FACTOR_LABELS,
            )
            _render_stratified_table(
                lines,
                "Stage 2 — Stratified by Language",
                s2["by_language"],
                s2_arms_order,
                "passive",
                label_map=STAGE2_FACTOR_LABELS,
            )

        # Control non-inferiority section
        lines.append(f"### Control Non-Inferiority Check (`{label}`)")
        lines.append("")
        ni_rows: list[tuple[str, str, Mapping[str, Any]]] = []
        for arm, ni in s1["control_non_inferiority"].items():
            ni_rows.append(("Stage 1", arm, ni))
        for factor, ni in s2["control_non_inferiority"].items():
            ni_rows.append(("Stage 2", STAGE2_FACTOR_LABELS.get(factor, factor), ni))

        if not ni_rows:
            lines.append("_No paired control-stratum comparisons available._")
            lines.append("")
        else:
            lines.append(
                "| Stage | Arm / Factor | n | +w/-l (net reg) | Paired Diff (95% CI) | "
                "1-Sided 95% UB (Reg Rate) | Clopper-Pearson 95% CI | Catastrophic | "
                "Operational Gate | Strict CI Gate |"
            )
            lines.append(
                "| --- | --- | ---: | --- | --- | ---: | --- | ---: | --- | --- |"
            )
            for stg, name, ni in ni_rows:
                ub = (
                    f"{ni['one_sided_95_upper_bound'] * 100.0:.1f}%"
                    if ni["one_sided_95_upper_bound"] is not None
                    else "n/a"
                )
                cp = _fmt_wilson(ni["clopper_pearson_95_low"], ni["clopper_pearson_95_high"])
                op_str = "PASS" if ni["operational_pass"] else "FAIL"
                ci_str = "PASS" if ni["ci_non_inferior"] else "UNDERPOWERED/FAIL"
                lines.append(
                    f"| {stg} | `{name}` | {ni['n_pairs']} | "
                    f"+{ni['wins']}/-{ni['losses']} ({ni['net_regressions']}) | "
                    f"{_fmt_signed_pct(ni['delta'])} {_fmt_ci_pct(ni['ci_low'], ni['ci_high'])} | "
                    f"{ub} | {cp} | {ni['catastrophic_failures']} | {op_str} | {ci_str} |"
                )
            lines.append("")
            lines.append(
                "> **Sample-Size Power Limitation:** On `n = 10` control tasks, even `0/10` "
                "paired regressions has an exact one-sided 95% upper confidence bound of `25.9%` "
                "(two-sided Clopper-Pearson `[0.0%, 30.8%]`), and `1/10` regressions has an "
                "upper bound of `39.4%`. Passing the operational `<= 1/10` tolerance is **never** "
                "proof of zero degradation; bounding true regression rate below `5%` at 95% "
                "confidence with `0` losses requires `n >= 59` independent control tasks."
            )
            lines.append("")

    # 4. Failure catalog
    lines.append("## 4. Failure, Timeout, False-Approval & UNKNOWN Verdict Catalog")
    lines.append("")
    failures = report["failure_catalog"]
    if not failures:
        lines.append(
            "_No failures, timeouts, audit false approvals, or UNKNOWN verdicts observed._"
        )
        lines.append("")
    else:
        tag_counts: Counter[str] = Counter()
        for item in failures:
            for tag in item["tags"]:
                tag_counts[tag] += 1
        summary_bits = ", ".join(f"`{k}={v}`" for k, v in sorted(tag_counts.items()))
        lines.append(
            f"**Observed anomaly counts:** {summary_bits} "
            f"(total flagged rows: {len(failures)})"
        )
        lines.append("")
        lines.append(
            "| Model | Stage | Arm | Case ID | Category | Language | Flags | "
            "Hidden | Smoke | Audit | Commit | Exit | Seconds |"
        )
        lines.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: |"
        )
        for item in failures:
            exit_str = item["exit"] if item["exit"] is not None else "n/a"
            lines.append(
                f"| `{item['model_label']}` | {item['stage']} | `{item['arm']}` | "
                f"`{item['case_id']}` | `{item['category']}` | `{item['language']}` | "
                f"`{', '.join(item['tags'])}` | {item['hidden_outcome']} | "
                f"{item['smoke_outcome']} | {item['audit_outcome']} | "
                f"{item['commit_outcome']} | {exit_str} | "
                f"{_fmt_num(item['seconds'], 1)} |"
            )
        lines.append("")

    # 5. Explicit Opt-In vs Default-On Shipping Gate Verdict
    lines.append("## 5. Shipping Gate Verdict (`OPT_IN`, `DEFAULT_ON`, `BLOCKED`, or `INCOMPLETE`)")
    lines.append("")
    gates = report["overall_shipping_gates"]
    if not gates:
        lines.append("_No treatment comparisons available to adjudicate shipping gates._")
        lines.append("")
    else:
        lines.append(
            "| Stage | Candidate | Overall Verdict | Per-Model Verdicts | Evidence Rationale |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for gate in gates:
            per_model = ", ".join(
                f"`{ml}={v}`" for ml, v in sorted(gate["per_model_verdicts"].items())
            )
            rationale = "; ".join(gate["reasons"])
            lines.append(
                f"| {gate['stage']} | `{gate['arm']}` | **{gate['overall_verdict']}** | "
                f"{per_model} | {rationale} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_run_spec(raw: str) -> ModelRunSpec:
    """Parse ``LABEL:STAGE1[:STAGE2_CSV]`` into a ``ModelRunSpec``."""
    parts = raw.split(":", 2)
    if len(parts) < 2:
        raise ValueError(
            f"Invalid --run specification {raw!r}; expected LABEL:STAGE1[:STAGE2_CSV]"
        )
    label = parts[0].strip()
    if not label:
        raise ValueError(f"Empty model label in --run specification {raw!r}")
    stage1_raw = [p.strip() for p in parts[1].split(",") if p.strip()]
    stage2_raw = [p.strip() for p in parts[2].split(",") if p.strip()] if len(parts) == 3 else []
    if not stage1_raw and not stage2_raw:
        raise ValueError(f"--run {raw!r} must include at least one Stage 1 or Stage 2 JSONL path")
    return ModelRunSpec(
        label=label,
        stage1_paths=tuple(Path(p) for p in stage1_raw),
        stage2_paths=tuple(Path(p) for p in stage2_raw),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the Issue #5 Section C2 stratified statistical evaluation report "
            "from frozen manifest and Stage 1 / Stage 2 replay trial JSONLs."
        )
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Path to frozen replay corpus manifest JSON.",
    )
    parser.add_argument(
        "--stage1-jsonl",
        action="append",
        default=[],
        type=Path,
        help="Path to Stage 1 JSONL file (repeatable).",
    )
    parser.add_argument(
        "--stage2-jsonl",
        action="append",
        default=[],
        type=Path,
        help="Path to Stage 2 ablation JSONL file (repeatable for cymbal, readrange, facts).",
    )
    parser.add_argument(
        "--model-label",
        default=None,
        help="Display label for the model when using --stage1-jsonl / --stage2-jsonl.",
    )
    parser.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="LABEL:STAGE1[:STAGE2_CSV]",
        help=(
            "Repeatable multi-model run specification: LABEL:STAGE1_JSONL[:STAGE2_CSV]. "
            "Cannot be combined with --stage1-jsonl / --stage2-jsonl."
        ),
    )
    parser.add_argument(
        "--out-md",
        required=True,
        type=Path,
        help="Output path for the Markdown report.",
    )
    parser.add_argument(
        "--out-json",
        default=None,
        type=Path,
        help="Optional output path for the structured JSON report.",
    )
    parser.add_argument(
        "--control-margin",
        type=float,
        default=DEFAULT_CONTROL_MARGIN,
        help=f"Preregistered control non-inferiority margin (default: {DEFAULT_CONTROL_MARGIN}).",
    )
    parser.add_argument(
        "--cymbal-version",
        default=None,
        help="Optional explicit cymbal version string for run metadata.",
    )
    parser.add_argument(
        "--allow-unfrozen",
        action="store_true",
        help="Allow analyzing an unfrozen draft manifest (still checks manifest_sha256 if set).",
    )
    return parser


def _infer_single_model_label(paths: Sequence[Path]) -> str:
    for path in paths:
        sidecar = load_sidecar_metadata(path)
        if sidecar and sidecar.get("model"):
            return str(sidecar["model"])
    for path in paths:
        rows = heldout.load_results(path)
        for row in rows:
            if row.get("model"):
                return str(row["model"])
    return "model"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    has_single = bool(args.stage1_jsonl or args.stage2_jsonl)
    has_runs = bool(args.run)
    if has_single and has_runs:
        parser.error("Use either --run or --stage1-jsonl/--stage2-jsonl, not both.")
    if not has_single and not has_runs:
        parser.error("Provide at least one --stage1-jsonl/--stage2-jsonl or --run input.")

    runs: list[ModelRunSpec] = []
    if has_runs:
        for spec_str in args.run:
            try:
                runs.append(parse_run_spec(spec_str))
            except ValueError as exc:
                parser.error(str(exc))
    else:
        all_paths = list(args.stage1_jsonl) + list(args.stage2_jsonl)
        label = args.model_label or _infer_single_model_label(all_paths)
        runs.append(
            ModelRunSpec(
                label=label,
                stage1_paths=tuple(args.stage1_jsonl),
                stage2_paths=tuple(args.stage2_jsonl),
            )
        )

    report = analyze_report(
        args.manifest,
        runs,
        require_frozen=not args.allow_unfrozen,
        control_margin=args.control_margin,
        cymbal_version=args.cymbal_version,
    )
    markdown = render_markdown(report)

    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(markdown, encoding="utf-8")
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
