"""Fast offline unit tests for scripts/local_context_trial/c2_report.py."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

_TRIAL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "local_context_trial"
if str(_TRIAL_DIR) not in sys.path:
    sys.path.insert(0, str(_TRIAL_DIR))

import c2_report  # noqa: E402
import replay_corpus  # noqa: E402

_DUMMY_BASE = "a" * 40
_DUMMY_FIX = "b" * 40


def _make_manifest_case(
    case_id: str,
    category: str,
    language: str,
    *,
    difficulty: str = "medium",
    editable_files: tuple[str, ...] = ("src/mod.py",),
) -> replay_corpus.ReplayCaseEntry:
    scope = "single_file" if len(editable_files) == 1 else "multi_file"
    return replay_corpus.ReplayCaseEntry(
        case_id=case_id,
        task_prompt=f"Fix bug in {case_id}.",
        explicit_guidance=f"Update behavior in {editable_files[0]}.",
        editable_files=editable_files,
        source=replay_corpus.SourcePin(
            repo_path="/tmp/fake-repo",
            base_commit=_DUMMY_BASE,
            fix_commit=_DUMMY_FIX,
        ),
        hidden=replay_corpus.HiddenSpec(command=("{python}", "{hidden_dir}/check.py")),
        preregistration=replay_corpus.CasePreregistration(
            selection_rationale="Synthetic test case for C2 report unit tests.",
            source_origin=f"example/{language}#1",
            expected_difficulty=difficulty,
            file_scope_category=scope,
            leakage_checked=True,
            preregistered_notes=(
                f"category={category}; language={language}; repo=example/{language}"
            ),
        ),
    )


def _write_mini_manifest(
    path: Path,
    entries: list[replay_corpus.ReplayCaseEntry],
    *,
    frozen: bool = True,
    corrupt_sha: bool = False,
) -> tuple[Path, str]:
    prereg = replay_corpus.PreregistrationSpec(
        protocol_version="1.0",
        preregistration_id="test-c2-mini-v1",
        registered_at="2026-11-25T16:40:00+02:00",
        frozen=frozen,
        hypothesis="Test C2 stratified report generator.",
        primary_metric="paired_hidden_pass_rate_delta",
        inclusion_criteria=("Historical bugfix",),
        exclusion_criteria=("Synthetic tuning fixtures",),
        stopping_rule="Fixed test corpus.",
        leakage_controls=("External hidden directory",),
        manifest_sha256=None,
    )
    manifest = replay_corpus.ReplayManifest(
        schema_version=replay_corpus.SCHEMA_VERSION,
        preregistration=prereg,
        cases=tuple(entries),
    )
    digest = replay_corpus.compute_manifest_digest(manifest)
    stored_sha = ("0" * 64) if corrupt_sha else (digest if frozen else None)
    manifest = replace(
        manifest,
        preregistration=replace(manifest.preregistration, manifest_sha256=stored_sha),
    )
    path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path, digest


def _build_30_case_entries() -> list[replay_corpus.ReplayCaseEntry]:
    """Build 30 synthetic cases matching the exact 10/10/10 and 7-language C1 balance."""
    spec = [
        ("python", ["direct_caller"] + ["indirect_caller"] * 3 + ["control"] * 2),
        ("javascript", ["direct_caller", "indirect_caller", "control", "control"]),
        ("typescript", ["direct_caller", "direct_caller", "indirect_caller", "control"]),
        ("cpp", ["direct_caller", "indirect_caller", "control", "control"]),
        ("bash", ["direct_caller", "indirect_caller", "indirect_caller", "control"]),
        ("c", ["direct_caller", "direct_caller", "indirect_caller", "control"]),
        ("rust", ["direct_caller", "direct_caller", "indirect_caller", "control"]),
    ]
    entries: list[replay_corpus.ReplayCaseEntry] = []
    idx = 0
    for lang, cats in spec:
        for cat in cats:
            idx += 1
            entries.append(_make_manifest_case(f"{lang}_{cat}_{idx:02d}", cat, lang))
    return entries


def _make_row(
    case_id: str,
    arm: str,
    *,
    hidden_passed: bool,
    smoke_passed: bool = True,
    only_target_modified: bool = True,
    audit_outcome: str = "not_run",
    audit_verdicts: list[str] | None = None,
    unknowns: int = 0,
    errors: int = 0,
    exit_code: int = 0,
    commit_outcome: str | None = None,
    backend: str = "builtin",
    read_range: bool = False,
    project_facts: bool = False,
    first_prompt: int | None = 2000,
    prompt_tokens: int | None = 2500,
    completion_tokens: int | None = 400,
    tokens: int | None = 2900,
    tokens_measured: bool | None = None,
    editor_calls: int | None = 1,
    seconds: float = 20.0,
    model: str = "lm_studio/qwen3-coder-30b-a3b-instruct-mlx",
    round_id: int = 0,
    spec: str = "vague",
) -> dict:
    if tokens_measured is None:
        tokens_measured = tokens is not None
    if commit_outcome is None:
        if exit_code == 124:
            commit_outcome = "timeout"
        elif exit_code != 0:
            commit_outcome = "error"
        elif only_target_modified:
            commit_outcome = "applied"
        else:
            commit_outcome = "out_of_scope"
    audit_ran = audit_outcome != "not_run" or bool(audit_verdicts)
    return {
        "schema_version": 2,
        "model": model,
        "round": round_id,
        "spec": spec,
        # Intentionally set generic replay defaults to verify manifest overrides them!
        "kind": "caller",
        "language": "python",
        "case": case_id,
        "arm": arm,
        "local_callers_backend": backend,
        "local_callers_read_range": read_range,
        "local_callers_enabled": arm != "baseline",
        "project_facts": project_facts,
        "facts_backend": "jsonl" if project_facts else "off",
        "map_tokens": 1024,
        "exit": exit_code,
        "oracle": True,
        "evidence": arm != "baseline",
        "audit_ran": audit_ran,
        "audit_verdicts": audit_verdicts if audit_verdicts is not None else [],
        "audit_outcome": audit_outcome,
        "audit_passed": (True if audit_outcome == "pass" else (None if not audit_ran else False)),
        "unknowns": unknowns,
        "errors": errors,
        "commit_outcome": commit_outcome,
        "commit_passed": commit_outcome in ("applied", "committed"),
        "only_target_modified": only_target_modified,
        "smoke_outcome": "timeout" if exit_code == 124 else ("pass" if smoke_passed else "fail"),
        "smoke_passed": smoke_passed,
        "hidden_outcome": "timeout" if exit_code == 124 else ("pass" if hidden_passed else "fail"),
        "hidden_passed": hidden_passed,
        "editor_calls": editor_calls,
        "extra_calls": max(0, (editor_calls or 1) - 1),
        "tokens": tokens,
        "tokens_measured": tokens_measured,
        "first_editor_prompt_tokens": first_prompt,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "passed": bool(hidden_passed and smoke_passed and only_target_modified),
        "seconds": seconds,
        "metadata": {
            "git_commit": "deadbeef1234567890",
            "git_dirty": False,
            "python_version": "3.12.13",
            "seed": 20261123,
            "suite": "replay",
        },
    }


def _write_jsonl(path: Path, rows: list[dict], sidecar: dict | None = None) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if sidecar is not None:
        path.with_suffix(".meta.json").write_text(
            json.dumps({"schema_version": 2, "latest": sidecar, "runs": [sidecar]}, indent=2),
            encoding="utf-8",
        )
    return path


# ---------------------------------------------------------------------------
# Unit tests: statistical primitives
# ---------------------------------------------------------------------------


def test_statistical_primitives_match_preregistered_reference_values():
    # Exact two-sided McNemar sign-test p-values (preregistration Section 6)
    assert c2_report.exact_mcnemar_p(0, 0) == 1.0
    assert c2_report.exact_mcnemar_p(6, 0) == pytest.approx(0.03125, abs=1e-9)
    assert c2_report.exact_mcnemar_p(8, 0) == pytest.approx(0.0078125, abs=1e-9)
    assert c2_report.exact_mcnemar_p(3, 5) == pytest.approx(c2_report.exact_mcnemar_p(5, 3))

    # Wilson 95% CI sanity
    lo0, hi0 = c2_report.wilson_ci(0, 10)
    assert lo0 == pytest.approx(0.0, abs=1e-9)
    assert 0.25 < hi0 < 0.30
    lo10, hi10 = c2_report.wilson_ci(10, 10)
    assert 0.70 < lo10 < 0.75
    assert hi10 == pytest.approx(1.0, abs=1e-9)

    # Exact binomial one-sided 95% upper bound & Clopper-Pearson on n=10 (preregistration Section 6)
    ub0 = c2_report.exact_binomial_upper_bound(0, 10, alpha=0.05)
    assert ub0 == pytest.approx(0.25886555, abs=1e-4)
    cp_lo, cp_hi = c2_report.clopper_pearson_ci(0, 10, alpha=0.05)
    assert cp_lo == 0.0
    assert cp_hi == pytest.approx(0.3084971, abs=1e-4)
    ub1 = c2_report.exact_binomial_upper_bound(1, 10, alpha=0.05)
    assert ub1 == pytest.approx(0.394163, abs=1e-4)

    # Newcombe Method 10 paired CI is non-degenerate at 0/0 discordant pairs
    delta, ci_lo, ci_hi = c2_report.newcombe_paired_ci(5, 0, 0, 5)
    assert delta == 0.0
    assert ci_lo is not None and ci_hi is not None
    assert ci_lo < 0.0 < ci_hi

    delta_pos, lo_pos, hi_pos = c2_report.newcombe_paired_ci(10, 8, 0, 2)
    assert delta_pos == pytest.approx(0.40)
    assert lo_pos is not None and lo_pos > 0.0
    assert hi_pos is not None and hi_pos <= 1.0


# ---------------------------------------------------------------------------
# Unit tests: manifest verification
# ---------------------------------------------------------------------------


def test_manifest_verification_detects_tampering_and_unfrozen(tmp_path: Path):
    entries = _build_30_case_entries()[:3]

    valid_path, expected_sha = _write_mini_manifest(tmp_path / "valid.json", entries, frozen=True)
    info = c2_report.load_and_verify_manifest(valid_path, require_frozen=True)
    assert info["sha256_verified"] is True
    assert info["computed_sha256"] == expected_sha
    assert info["declared_sha256"] == expected_sha

    tampered_path, _ = _write_mini_manifest(
        tmp_path / "tampered.json", entries, frozen=True, corrupt_sha=True
    )
    with pytest.raises(c2_report.ManifestVerificationError, match="mismatch"):
        c2_report.load_and_verify_manifest(tampered_path, require_frozen=True)

    unfrozen_path, _ = _write_mini_manifest(tmp_path / "unfrozen.json", entries, frozen=False)
    with pytest.raises(c2_report.ManifestVerificationError, match="not frozen"):
        c2_report.load_and_verify_manifest(unfrozen_path, require_frozen=True)

    draft_info = c2_report.load_and_verify_manifest(unfrozen_path, require_frozen=False)
    assert draft_info["frozen"] is False
    assert draft_info["sha256_verified"] is False


# ---------------------------------------------------------------------------
# Unit tests: Stage 1 + Stage 2 pairing, false approvals, UNKNOWN, matched tokens
# ---------------------------------------------------------------------------


def test_stage1_stage2_pairing_safety_metrics_and_stratification(tmp_path: Path):
    entries = _build_30_case_entries()
    manifest_path, manifest_sha = _write_mini_manifest(tmp_path / "manifest.json", entries)

    caller_ids = [
        e.case_id
        for e in entries
        if "category=direct_caller" in e.preregistration.preregistered_notes
        or "category=indirect_caller" in e.preregistration.preregistered_notes
    ]
    control_ids = [
        e.case_id
        for e in entries
        if "category=control" in e.preregistration.preregistered_notes
    ]
    assert len(caller_ids) == 20 and len(control_ids) == 10

    stage1_rows: list[dict] = []
    # Baseline: 8/20 caller pass, 9/10 control pass
    for idx, cid in enumerate(caller_ids):
        stage1_rows.append(
            _make_row(
                cid,
                "baseline",
                hidden_passed=(idx < 8),
                first_prompt=2000,
                prompt_tokens=2000,
                completion_tokens=300,
                tokens=2300,
                editor_calls=1,
                seconds=10.0,
            )
        )
    for idx, cid in enumerate(control_ids):
        stage1_rows.append(
            _make_row(
                cid,
                "baseline",
                hidden_passed=(idx < 9),
                first_prompt=2000,
                prompt_tokens=2000,
                completion_tokens=300,
                tokens=2300,
                editor_calls=1,
                seconds=10.0,
            )
        )

    # Passive: +7/-0 on callers (15/20, p=0.0156), 9/10 on control (0/10 regressions)
    for idx, cid in enumerate(caller_ids):
        stage1_rows.append(
            _make_row(
                cid,
                "passive",
                hidden_passed=(idx < 15),
                first_prompt=2400,
                prompt_tokens=2600,
                completion_tokens=350,
                tokens=2950,
                editor_calls=2 if idx % 2 == 0 else 1,
                seconds=15.0 + (idx % 5),
            )
        )
    for idx, cid in enumerate(control_ids):
        stage1_rows.append(
            _make_row(
                cid,
                "passive",
                hidden_passed=(idx < 9),
                first_prompt=2400,
                prompt_tokens=2600,
                completion_tokens=350,
                tokens=2950,
                editor_calls=1,
                seconds=14.0,
            )
        )

    # Contract: +6/-0 on callers, -2 net regressions on control -> BLOCKED
    for idx, cid in enumerate(caller_ids):
        stage1_rows.append(
            _make_row(cid, "contract", hidden_passed=(idx < 14), seconds=16.0)
        )
    for idx, cid in enumerate(control_ids):
        stage1_rows.append(
            _make_row(cid, "contract", hidden_passed=(idx < 7), seconds=16.0)
        )

    # Audit: 1 false approval (audit PASS while hidden FAIL) + 2 UNKNOWN verdicts -> BLOCKED
    for idx, cid in enumerate(caller_ids):
        if idx == 18:
            # False approval!
            stage1_rows.append(
                _make_row(
                    cid,
                    "audit",
                    hidden_passed=False,
                    audit_outcome="pass",
                    audit_verdicts=["PASS"],
                    seconds=25.0,
                )
            )
        elif idx in (16, 17):
            stage1_rows.append(
                _make_row(
                    cid,
                    "audit",
                    hidden_passed=False,
                    audit_outcome="unknown",
                    audit_verdicts=["UNKNOWN"],
                    unknowns=1,
                    seconds=25.0,
                )
            )
        else:
            stage1_rows.append(
                _make_row(
                    cid,
                    "audit",
                    hidden_passed=(idx < 15),
                    audit_outcome="pass" if idx < 15 else "fail",
                    audit_verdicts=["PASS"] if idx < 15 else ["FAIL"],
                    seconds=22.0,
                )
            )
    for idx, cid in enumerate(control_ids):
        stage1_rows.append(
            _make_row(
                cid,
                "audit",
                hidden_passed=(idx < 9),
                audit_outcome="pass" if idx < 9 else "fail",
                audit_verdicts=["PASS"] if idx < 9 else ["FAIL"],
                seconds=20.0,
            )
        )

    # Contract+audit: clean on safety, 1 recovered UNKNOWN verdict in history
    for idx, cid in enumerate(caller_ids):
        stage1_rows.append(
            _make_row(
                cid,
                "contract+audit",
                hidden_passed=(idx < 15),
                audit_outcome="pass" if idx < 15 else "fail",
                audit_verdicts=(
                    ["UNKNOWN", "PASS"]
                    if idx == 0
                    else (["PASS"] if idx < 15 else ["FAIL"])
                ),
                unknowns=1 if idx == 0 else 0,
                seconds=24.0,
            )
        )
    for idx, cid in enumerate(control_ids):
        stage1_rows.append(
            _make_row(
                cid,
                "contract+audit",
                hidden_passed=(idx < 9),
                audit_outcome="pass" if idx < 9 else "fail",
                audit_verdicts=["PASS"] if idx < 9 else ["FAIL"],
                seconds=21.0,
            )
        )

    # Stage 2 JSONLs: cymbal, read_range (with 1 missing-token row + 1 timeout), facts
    s2_cymbal_rows = [
        _make_row(
            e.case_id,
            "passive",
            backend="cymbal",
            hidden_passed=(e.case_id in caller_ids[:17] or e.case_id in control_ids[:9]),
            first_prompt=2200,
            prompt_tokens=2400,
            completion_tokens=320,
            tokens=2720,
            seconds=12.0,
        )
        for e in entries
    ]

    s2_rr_rows: list[dict] = []
    for idx, e in enumerate(entries):
        if idx == 0:
            # Timeout with missing tokens — must NOT be hidden!
            s2_rr_rows.append(
                _make_row(
                    e.case_id,
                    "passive",
                    read_range=True,
                    hidden_passed=False,
                    smoke_passed=False,
                    exit_code=124,
                    first_prompt=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    tokens=None,
                    tokens_measured=False,
                    seconds=360.0,
                )
            )
        elif idx == 1:
            # Missing tokens without timeout
            s2_rr_rows.append(
                _make_row(
                    e.case_id,
                    "passive",
                    read_range=True,
                    hidden_passed=True,
                    first_prompt=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    tokens=None,
                    tokens_measured=False,
                    seconds=11.0,
                )
            )
        else:
            s2_rr_rows.append(
                _make_row(
                    e.case_id,
                    "passive",
                    read_range=True,
                    hidden_passed=(e.case_id in caller_ids[:15] or e.case_id in control_ids[:9]),
                    first_prompt=1920,
                    prompt_tokens=2080,
                    completion_tokens=310,
                    tokens=2390,
                    seconds=12.0,
                )
            )

    s2_facts_rows = [
        _make_row(
            e.case_id,
            "passive",
            project_facts=True,
            hidden_passed=(e.case_id in caller_ids[:15] or e.case_id in control_ids[:9]),
            first_prompt=2410,
            prompt_tokens=2610,
            completion_tokens=350,
            tokens=2960,
            seconds=15.0,
        )
        for e in entries
    ]

    sidecar = {
        "model": "lm_studio/qwen3-coder-30b-a3b-instruct-mlx",
        "git_commit": "abcdef1234567890",
        "git_dirty": False,
        "python_version": "3.12.13",
        "seed": 20261123,
        "map_tokens": 1024,
        "cymbal_version": "cymbal 0.18.0",
    }
    s1_path = _write_jsonl(tmp_path / "stage1.jsonl", stage1_rows, sidecar=sidecar)
    s2_cym_path = _write_jsonl(tmp_path / "stage2_cymbal.jsonl", s2_cymbal_rows)
    s2_rr_path = _write_jsonl(tmp_path / "stage2_readrange.jsonl", s2_rr_rows)
    s2_facts_path = _write_jsonl(tmp_path / "stage2_facts.jsonl", s2_facts_rows)

    out_md = tmp_path / "report.md"
    out_json = tmp_path / "report.json"

    rc = c2_report.main(
        [
            "--manifest",
            str(manifest_path),
            "--stage1-jsonl",
            str(s1_path),
            "--stage2-jsonl",
            str(s2_cym_path),
            "--stage2-jsonl",
            str(s2_rr_path),
            "--stage2-jsonl",
            str(s2_facts_path),
            "--model-label",
            "qwen3-30b",
            "--out-md",
            str(out_md),
            "--out-json",
            str(out_json),
        ]
    )
    assert rc == 0
    assert out_md.is_file() and out_json.is_file()

    rep = json.loads(out_json.read_text(encoding="utf-8"))
    md = out_md.read_text(encoding="utf-8")

    # Manifest verification & metadata in report
    assert rep["manifest"]["computed_sha256"] == manifest_sha
    assert manifest_sha in md
    assert "cymbal 0.18.0" in md
    assert "20261123" in md
    assert "abcdef123456" in md

    mrep = rep["models"][0]
    s1_by_arm = {row["arm"]: row for row in mrep["stage1"]["table"]}

    # Stage 1 passive vs baseline: +7/-0 overall (p = 2/128 = 0.015625)
    assert s1_by_arm["passive"]["paired"]["wins"] == 7
    assert s1_by_arm["passive"]["paired"]["losses"] == 0
    assert s1_by_arm["passive"]["paired"]["p_value"] == pytest.approx(0.015625)
    assert s1_by_arm["passive"]["metrics"]["reflections"]["mean"] == pytest.approx(10 / 30)

    # Audit arm false approval & UNKNOWN verdict detection
    audit_m = s1_by_arm["audit"]["metrics"]
    assert audit_m["audit_ran"] == 30
    assert audit_m["false_approval_count"] == 1
    assert audit_m["false_approval_rate"] == pytest.approx(1 / 30)
    assert audit_m["unknown_row_count"] == 2
    assert audit_m["unknown_verdict_count"] == 2
    assert audit_m["unknown_rate"] == pytest.approx(2 / 30)

    # Stage 2 read_range matched-pair denominator must exclude the 2 missing-token rows
    # while preserving timeout count and full 30-case accuracy denominator!
    s2_by_factor = {row["factor"]: row for row in mrep["stage2"]["table"]}
    rr_row = s2_by_factor["read_range"]
    assert rr_row["metrics"]["n"] == 30
    assert rr_row["metrics"]["timeouts"] == 1
    assert rr_row["paired"]["n_pairs"] == 30
    tok_delta = rr_row["paired"]["total_tokens"]
    assert tok_delta["n_total_pairs"] == 30
    assert tok_delta["n_matched_pairs"] == 28
    assert tok_delta["n_missing_either"] == 2
    assert tok_delta["n_timeout_either"] == 1
    # Exact matched-pair delta: (2390 - 2950) / 2950 = -18.983%
    assert tok_delta["delta_pct"] == pytest.approx(((2390 - 2950) / 2950) * 100.0, abs=1e-4)

    # Stratified tables cover all categories (including caller_dependent) & all 7 languages
    cat_names = [row["stratum"] for row in mrep["stage1"]["by_category"]]
    assert cat_names == ["direct_caller", "indirect_caller", "caller_dependent", "control"]
    lang_names = [row["stratum"] for row in mrep["stage1"]["by_language"]]
    assert lang_names == ["python", "javascript", "typescript", "cpp", "bash", "c", "rust"]

    # Control non-inferiority checks
    passive_ni = mrep["stage1"]["control_non_inferiority"]["passive"]
    assert passive_ni["n_pairs"] == 10
    assert passive_ni["losses"] == 0
    assert passive_ni["operational_pass"] is True
    assert passive_ni["ci_non_inferior"] is False  # n=10 CI crosses -10%
    assert "NOT proof of zero degradation" in passive_ni["power_limitation_note"]

    contract_ni = mrep["stage1"]["control_non_inferiority"]["contract"]
    assert contract_ni["losses"] == 2
    assert contract_ni["operational_pass"] is False

    # Shipping gate verdicts
    s1_gates = {g["arm"]: g for g in mrep["shipping_gates"]["stage1"]}
    assert s1_gates["passive"]["verdict"] == "OPT_IN"
    assert s1_gates["contract"]["verdict"] == "BLOCKED"
    assert s1_gates["audit"]["verdict"] == "BLOCKED"

    s2_gates = {g["arm"]: g for g in mrep["shipping_gates"]["stage2"]}
    assert s2_gates["cymbal"]["verdict"] == "OPT_IN"
    assert s2_gates["read_range"]["verdict"] == "BLOCKED"  # new timeout!
    assert s2_gates["facts"]["verdict"] == "OPT_IN"  # unseeded replay scope note

    # Failure catalog captures false approval, UNKNOWN, and timeout
    tags_seen = {tag for item in rep["failure_catalog"] for tag in item["tags"]}
    assert {"HIDDEN_FAIL", "FALSE_APPROVAL", "UNKNOWN_VERDICT", "TIMEOUT"} <= tags_seen

    # Markdown contains all required sections & caveats
    assert "## 1. Frozen Manifest Verification" in md
    assert "## 2. Run Metadata & Provenance" in md
    assert "Stage 1 — Stratified by Category" in md
    assert "Stage 1 — Stratified by Language" in md
    assert "Stage 2 — One-Factor-at-a-Time Ablations" in md
    assert "Scope Note (`facts=on`)" in md
    assert "Sample-Size Power Limitation" in md
    assert "## 4. Failure, Timeout, False-Approval & UNKNOWN Verdict Catalog" in md
    assert "## 5. Shipping Gate Verdict" in md
    assert "28/30, to=1" in md


# ---------------------------------------------------------------------------
# Unit tests: DEFAULT_ON eligibility & multi-model --run CLI
# ---------------------------------------------------------------------------


def test_default_on_verdict_and_multi_model_run_cli(tmp_path: Path):
    # 20 caller tasks + 60 control tasks so control 95% CI stays within -10% margin
    entries = [
        _make_manifest_case(
            f"caller_{i:02d}",
            "direct_caller" if i < 10 else "indirect_caller",
            "python",
        )
        for i in range(20)
    ] + [
        _make_manifest_case(f"ctrl_{i:02d}", "control", "rust")
        for i in range(60)
    ]
    manifest_path, _ = _write_mini_manifest(tmp_path / "big_manifest.json", entries)

    # Model A: strong caller lift (+10/-0, p=0.002), 60/60 control pass (0 regressions, CI > -10%),
    # zero safety issues -> DEFAULT_ON!
    rows_a: list[dict] = []
    for i, e in enumerate(entries):
        is_caller = i < 20
        rows_a.append(
            _make_row(
                e.case_id,
                "baseline",
                hidden_passed=(i < 5) if is_caller else True,
                model="model-a",
            )
        )
        rows_a.append(
            _make_row(
                e.case_id,
                "passive",
                hidden_passed=(i < 15) if is_caller else True,
                model="model-a",
            )
        )

    # Model B: same strong lift & clean safety, plus Stage 2 cymbal ablation
    rows_b_s1 = [dict(r, model="model-b") for r in rows_a]
    rows_b_s2 = [
        _make_row(
            e.case_id,
            "passive",
            backend="cymbal",
            hidden_passed=True,
            first_prompt=1800,
            prompt_tokens=2000,
            tokens=2200,
            seconds=10.0,
            model="model-b",
        )
        for e in entries
    ]

    p_a = _write_jsonl(tmp_path / "model_a_s1.jsonl", rows_a)
    p_b1 = _write_jsonl(tmp_path / "model_b_s1.jsonl", rows_b_s1)
    p_b2 = _write_jsonl(tmp_path / "model_b_s2_cymbal.jsonl", rows_b_s2)

    out_md = tmp_path / "multi.md"
    out_json = tmp_path / "multi.json"

    rc = c2_report.main(
        [
            "--manifest",
            str(manifest_path),
            "--run",
            f"model-a:{p_a}",
            "--run",
            f"model-b:{p_b1}:{p_b2}",
            "--cymbal-version",
            "cymbal v0.18.0",
            "--out-md",
            str(out_md),
            "--out-json",
            str(out_json),
        ]
    )
    assert rc == 0
    rep = json.loads(out_json.read_text(encoding="utf-8"))
    assert [m["label"] for m in rep["models"]] == ["model-a", "model-b"]

    overall_gates = {(g["stage"], g["arm"]): g for g in rep["overall_shipping_gates"]}
    passive_gate = overall_gates[("stage1", "passive")]
    assert passive_gate["overall_verdict"] == "DEFAULT_ON"
    assert passive_gate["per_model_verdicts"] == {
        "model-a": "DEFAULT_ON",
        "model-b": "DEFAULT_ON",
    }

    cymbal_gate = overall_gates[("stage2", "cymbal")]
    assert cymbal_gate["overall_verdict"] == "DEFAULT_ON"
    assert "model-a" in out_md.read_text(encoding="utf-8")
    assert "model-b" in out_md.read_text(encoding="utf-8")


def test_parse_run_spec_validation():
    spec = c2_report.parse_run_spec("qwen:/tmp/s1.jsonl:/tmp/cym.jsonl,/tmp/rr.jsonl")
    assert spec.label == "qwen"
    assert spec.stage1_paths == (Path("/tmp/s1.jsonl"),)
    assert spec.stage2_paths == (Path("/tmp/cym.jsonl"), Path("/tmp/rr.jsonl"))

    with pytest.raises(ValueError, match="expected LABEL:STAGE1"):
        c2_report.parse_run_spec("missing-colon")

    with pytest.raises(ValueError, match="Empty model label"):
        c2_report.parse_run_spec(":/tmp/s1.jsonl")


# ---------------------------------------------------------------------------
# Regression tests: P1 #2, P2 #6, and trial_error handling
# ---------------------------------------------------------------------------


def test_p1_2_unregistered_cases_raise_manifest_verification_error(tmp_path: Path):
    """1-task frozen manifest + 79 unregistered rows must fail closed."""
    manifest_entries = [_make_manifest_case("registered_00", "direct_caller", "python")]
    manifest_path, _ = _write_mini_manifest(tmp_path / "one_case.json", manifest_entries)

    rows: list[dict] = []
    all_case_ids = ["registered_00"] + [f"unregistered_{i:02d}" for i in range(1, 80)]
    for cid in all_case_ids:
        rows.append(_make_row(cid, "baseline", hidden_passed=False))
        rows.append(_make_row(cid, "passive", hidden_passed=True))

    s1_path = _write_jsonl(tmp_path / "unregistered_s1.jsonl", rows)
    spec = c2_report.ModelRunSpec(label="m1", stage1_paths=(s1_path,))
    with pytest.raises(c2_report.ManifestVerificationError, match="79 case_id\\(s\\) not present"):
        c2_report.analyze_report(manifest_path, [spec], probe_host_cymbal=False)


def test_p1_2_incomplete_unfrozen_zero_pairs_and_blocked_precedence(tmp_path: Path):
    """Incomplete, zero-pair, or unfrozen runs never emit DEFAULT_ON; safety failure -> BLOCKED."""
    entries = [
        _make_manifest_case(
            f"caller_{i:02d}",
            "direct_caller" if i < 10 else "indirect_caller",
            "python",
        )
        for i in range(20)
    ] + [
        _make_manifest_case(f"ctrl_{i:02d}", "control", "rust")
        for i in range(60)
    ]

    def _build_default_on_rows() -> list[dict]:
        rows: list[dict] = []
        for i, e in enumerate(entries):
            is_caller = i < 20
            rows.append(
                _make_row(e.case_id, "baseline", hidden_passed=(i < 5) if is_caller else True)
            )
            rows.append(
                _make_row(e.case_id, "passive", hidden_passed=(i < 15) if is_caller else True)
            )
        return rows

    # 1. Unfrozen manifest with strong lift -> INCOMPLETE (never DEFAULT_ON)
    unfrozen_manifest, _ = _write_mini_manifest(
        tmp_path / "unfrozen_80.json", entries, frozen=False
    )
    full_s1 = _write_jsonl(tmp_path / "full_s1.jsonl", _build_default_on_rows())
    unfrozen_rep = c2_report.analyze_report(
        unfrozen_manifest,
        [c2_report.ModelRunSpec(label="m1", stage1_paths=(full_s1,))],
        require_frozen=False,
        probe_host_cymbal=False,
    )
    unfrozen_gate = unfrozen_rep["models"][0]["shipping_gates"]["stage1"][0]
    assert unfrozen_gate["verdict"] == "INCOMPLETE"
    assert unfrozen_rep["overall_shipping_gates"][0]["overall_verdict"] == "INCOMPLETE"

    # 2. Missing pair on frozen manifest -> INCOMPLETE (never DEFAULT_ON)
    frozen_manifest, _ = _write_mini_manifest(tmp_path / "frozen_80.json", entries, frozen=True)
    missing_pair_rows = [
        r
        for r in _build_default_on_rows()
        if not (r["case"] == "caller_19" and r["arm"] == "passive")
    ]
    partial_s1 = _write_jsonl(tmp_path / "partial_s1.jsonl", missing_pair_rows)
    partial_rep = c2_report.analyze_report(
        frozen_manifest,
        [c2_report.ModelRunSpec(label="m1", stage1_paths=(partial_s1,))],
        probe_host_cymbal=False,
    )
    partial_gate = partial_rep["models"][0]["shipping_gates"]["stage1"][0]
    assert partial_gate["verdict"] == "INCOMPLETE"
    assert "79/80" in " ".join(partial_gate["reasons"])
    assert "reference_only=1" in " ".join(partial_gate["reasons"])

    # 3. Zero-pairs edge case (required_pairs == 0, n_pairs == 0) -> INCOMPLETE
    zero_paired = c2_report.compute_paired_comparison({}, {})
    zero_ctrl_ni = c2_report.evaluate_control_non_inferiority({}, {}, [])
    zero_metrics = c2_report.compute_arm_metrics([])
    zero_gate = c2_report.evaluate_shipping_gate(
        arm_name="passive",
        stage="stage1",
        arm_metrics=zero_metrics,
        required_pairs=0,
        frozen=True,
        overall_paired=zero_paired,
        caller_paired=zero_paired,
        control_ni=zero_ctrl_ni,
    )
    assert zero_gate["verdict"] == "INCOMPLETE"
    assert zero_gate["checks"]["coverage_complete"] is False

    # 4. Incomplete run WITH a safety failure -> BLOCKED (while still reporting incompleteness)
    unsafe_partial_rows = _build_default_on_rows()
    unsafe_partial_rows = [
        r for r in unsafe_partial_rows if not (r["case"] == "caller_19" and r["arm"] == "passive")
    ]
    for r in unsafe_partial_rows:
        if r["case"] == "caller_18" and r["arm"] == "passive":
            r["hidden_passed"] = False
            r["audit_ran"] = True
            r["audit_outcome"] = "pass"
            r["audit_verdicts"] = ["PASS"]
    unsafe_s1 = _write_jsonl(tmp_path / "unsafe_partial_s1.jsonl", unsafe_partial_rows)
    unsafe_rep = c2_report.analyze_report(
        frozen_manifest,
        [c2_report.ModelRunSpec(label="m1", stage1_paths=(unsafe_s1,))],
        probe_host_cymbal=False,
    )
    unsafe_gate = unsafe_rep["models"][0]["shipping_gates"]["stage1"][0]
    assert unsafe_gate["verdict"] == "BLOCKED"
    reasons_text = " ".join(unsafe_gate["reasons"])
    assert "false_approvals=1" in reasons_text
    assert "79/80" in reasons_text
    assert "reference_only=1" in reasons_text


def test_p2_6_matched_pair_new_timeouts_and_unmatched_reporting(tmp_path: Path):
    """Baseline timeout on A + treatment timeout on B must report new_timeouts=1 and BLOCK."""
    entries = [
        _make_manifest_case("task_a", "direct_caller", "python"),
        _make_manifest_case("task_b", "direct_caller", "python"),
        _make_manifest_case("ctrl_01", "control", "rust"),
    ]
    manifest_path, _ = _write_mini_manifest(tmp_path / "to_manifest.json", entries)

    # Stage 1: baseline times out on task_a (passive passes), passive times out on task_b
    # (baseline passes). Aggregate timeouts are 1 vs 1, but matched-pair new_timeouts == 1!
    s1_rows = [
        _make_row("task_a", "baseline", hidden_passed=False, smoke_passed=False, exit_code=124),
        _make_row("task_a", "passive", hidden_passed=True),
        _make_row("task_b", "baseline", hidden_passed=True),
        _make_row("task_b", "passive", hidden_passed=False, smoke_passed=False, exit_code=124),
        _make_row("ctrl_01", "baseline", hidden_passed=True),
        _make_row("ctrl_01", "passive", hidden_passed=True),
        # Contract arm has unmatched treatment-only (round 1) and reference-only (missing task_b r0)
        _make_row("task_a", "contract", hidden_passed=True, round_id=0),
        _make_row("ctrl_01", "contract", hidden_passed=True, round_id=0),
        _make_row("task_a", "contract", hidden_passed=True, round_id=1),
    ]
    # Stage 2: cymbal has unmatched treatment_only=1 (round 1) and reference_only=1 (missing task_b)
    s2_rows = [
        _make_row("task_a", "passive", backend="cymbal", hidden_passed=True, round_id=0),
        _make_row("ctrl_01", "passive", backend="cymbal", hidden_passed=True, round_id=0),
        _make_row("task_a", "passive", backend="cymbal", hidden_passed=True, round_id=1),
    ]
    s1_path = _write_jsonl(tmp_path / "to_s1.jsonl", s1_rows)
    s2_path = _write_jsonl(tmp_path / "to_s2.jsonl", s2_rows)
    rep = c2_report.analyze_report(
        manifest_path,
        [c2_report.ModelRunSpec(label="m1", stage1_paths=(s1_path,), stage2_paths=(s2_path,))],
        probe_host_cymbal=False,
    )
    md = c2_report.render_markdown(rep)

    s1_by_arm = {row["arm"]: row for row in rep["models"][0]["stage1"]["table"]}
    assert s1_by_arm["baseline"]["metrics"]["timeouts"] == 1
    assert s1_by_arm["passive"]["metrics"]["timeouts"] == 1
    assert s1_by_arm["passive"]["paired"]["new_timeouts"] == 1

    gates = {g["arm"]: g for g in rep["models"][0]["shipping_gates"]["stage1"]}
    assert gates["passive"]["verdict"] == "BLOCKED"
    assert "new_timeouts=1" in " ".join(gates["passive"]["reasons"])

    assert s1_by_arm["contract"]["paired"]["treatment_only"] == 1
    assert s1_by_arm["contract"]["paired"]["reference_only"] == 1
    assert gates["contract"]["verdict"] == "INCOMPLETE"

    s2_by_factor = {row["factor"]: row for row in rep["models"][0]["stage2"]["table"]}
    assert s2_by_factor["cymbal"]["paired"]["treatment_only"] == 1
    assert s2_by_factor["cymbal"]["paired"]["reference_only"] == 1
    s2_gates = {g["arm"]: g for g in rep["models"][0]["shipping_gates"]["stage2"]}
    assert s2_gates["cymbal"]["verdict"] == "INCOMPLETE"

    assert "1 (new=1)" in md
    assert "new_timeouts=1" in md
    assert "treat_only=1, ref_only=1" in md
    assert "treatment_only=1, reference_only=1" in md
    assert "`cymbal (backend=cymbal)` (treatment_only=1, reference_only=1)" in md


def test_trial_error_row_counts_as_error_and_blocks_gate(tmp_path: Path):
    """Rows carrying trial_error are normalized as is_error=True, cataloged, and block shipping."""
    entries = [
        _make_manifest_case("caller_01", "direct_caller", "python"),
        _make_manifest_case("ctrl_01", "control", "rust"),
    ]
    manifest_path, _ = _write_mini_manifest(tmp_path / "err_manifest.json", entries)

    # Even if exit=0 and commit_outcome="applied", trial_error must mark is_error=True!
    err_row = _make_row("caller_01", "passive", hidden_passed=False, exit_code=0)
    err_row["trial_error"] = "ConnectionError: upstream reset"

    s1_rows = [
        _make_row("caller_01", "baseline", hidden_passed=False),
        err_row,
        _make_row("ctrl_01", "baseline", hidden_passed=True),
        _make_row("ctrl_01", "passive", hidden_passed=True),
    ]
    s1_path = _write_jsonl(tmp_path / "err_s1.jsonl", s1_rows)
    rep = c2_report.analyze_report(
        manifest_path,
        [c2_report.ModelRunSpec(label="m1", stage1_paths=(s1_path,))],
        probe_host_cymbal=False,
    )

    s1_by_arm = {row["arm"]: row for row in rep["models"][0]["stage1"]["table"]}
    assert s1_by_arm["passive"]["metrics"]["errors"] == 1

    gates = {g["arm"]: g for g in rep["models"][0]["shipping_gates"]["stage1"]}
    assert gates["passive"]["verdict"] == "BLOCKED"
    assert gates["passive"]["checks"]["safety_ok"] is False

    catalog_matches = [
        item
        for item in rep["failure_catalog"]
        if item["case_id"] == "caller_01" and item["arm"] == "passive"
    ]
    assert len(catalog_matches) == 1
    assert "ERROR" in catalog_matches[0]["tags"]
    assert catalog_matches[0]["trial_error"] == "ConnectionError: upstream reset"
