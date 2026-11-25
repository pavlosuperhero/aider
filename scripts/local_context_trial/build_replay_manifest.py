"""Build and freeze the unified 7-language 30-task Issue #5 C1 replay corpus.

Loads the five pre-validated external case pools (Python, JavaScript,
polyglot TypeScript/C++/Bash, C, and Rust) from ``--input-dir``, selects the
curated 30-task balanced corpus (10 direct_caller, 10 indirect_caller,
10 control across 7 executable languages with >= 3 multi-file tasks), enforces
prompt leakage and balance invariants, builds the preregistered manifest with
canonical ``manifest_sha256`` computed via
``replay_corpus.compute_manifest_digest``, sets ``frozen=True``, and writes the
frozen manifest JSON to ``--out``.

No external repositories, case pools, or hidden test files are bundled into git.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import replay_corpus  # noqa: E402

PROTOCOL_VERSION = "1.0"
PREREGISTRATION_ID = "issue5-c1-polyglot-v1"
REGISTERED_AT = "2026-11-25T16:40:00+02:00"

DEFAULT_INPUT_DIR = Path("/tmp/issue5-replay-corpus")
DEFAULT_OUT_PATH = DEFAULT_INPUT_DIR / "manifest.json"

INPUT_CASE_FILES: tuple[str, ...] = (
    "cases_python.json",
    "cases_js.json",
    "cases_polyglot.json",
    "cases_c.json",
    "cases_rust.json",
)

ALLOWED_CATEGORIES: tuple[str, ...] = (
    "direct_caller",
    "indirect_caller",
    "control",
)
ALLOWED_LANGUAGES: tuple[str, ...] = (
    "python",
    "javascript",
    "typescript",
    "cpp",
    "bash",
    "c",
    "rust",
)

EXPECTED_CATEGORY_COUNTS: dict[str, int] = {
    "direct_caller": 10,
    "indirect_caller": 10,
    "control": 10,
}
EXPECTED_LANGUAGE_COUNTS: dict[str, int] = {
    "python": 6,
    "javascript": 4,
    "typescript": 4,
    "cpp": 4,
    "bash": 4,
    "c": 4,
    "rust": 4,
}
EXPECTED_TOTAL_CASES = 30
MIN_MULTI_FILE_CASES = 3

CURATED_CASE_IDS: tuple[str, ...] = (
    # Python (6): 1 direct_caller (multi_file), 3 indirect_caller (incl. multi_file), 2 control
    "py_packaging_invalid_interpreter_tag",
    "py_pathspec_globstar_newline",
    "py_pathspec_gitignore_cr_trim",
    "py_dotenv_parser_crlf_recovery",
    "py_click_show_envvar_sequence",
    "py_itsdangerous_future_timestamp",
    # JavaScript (4): 1 direct_caller (multi_file), 1 indirect_caller, 2 control
    "js_dotenv_quiet_from_env",
    "js_semver_inc_identifier_validation",
    "js_dotenv_fast_bom",
    "js_ms_reject_infinity",
    # TypeScript (4): 2 direct_caller, 1 indirect_caller, 1 control
    "ts_pathe_resolve_unc_prefix",
    "ts_ohash_serialize_map_keys",
    "ts_pathe_normalize_windows_drive",
    "ts_scule_is_uppercase",
    # C++ (4): 1 direct_caller, 1 indirect_caller, 2 control
    "cpp_inih_multiline_no_value",
    "cpp_fmt_dynamic_args_size",
    "cpp_fmt_printf_unsigned_sign",
    "cpp_semver_prerelease_zero",
    # Bash (4): 1 direct_caller, 2 indirect_caller, 1 control
    "bash_bats_semaphore_exit_status",
    "bash_bats_load_stack_trace",
    "bash_rbenv_version_file_traversal",
    "bash_rbenv_init_shell_suffix",
    # C (4): 2 direct_caller, 1 indirect_caller, 1 control
    "c_yajl_parse_integer_non_digit",
    "c_hiredis_map_elements_overflow",
    "c_yajl_decode_null_byte",
    "c_tiny_regex_dot_newline",
    # Rust (4): 2 direct_caller, 1 indirect_caller, 1 control
    "rust_semver_eval_less_prerelease",
    "rust_humantime_duration_nsec_carry_overflow",
    "rust_json_print_dec_neg_exp",
    "rust_semver_display_formatter_flags",
)

LEAKAGE_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "diff --git",
    "@@ ",
    "+++ b/",
    "--- a/",
    "/tmp/issue5-replay-corpus",
    "/private/tmp/issue5-replay-corpus",
    "/tmp/issue5-replay-repos",
    "/private/tmp/issue5-replay-repos",
    "{hidden_dir}",
    "hidden_py/",
    "hidden_js/",
    "hidden_polyglot/",
    "hidden_c/",
    "hidden_rust/",
    "_hidden.py",
    "_hidden.sh",
    "_hidden.cjs",
    "assert ",
    "assert.equal",
    "assert.strictEqual",
    "assert.throws",
    "pytest.raises",
    "unittest.TestCase",
    "fix_commit",
    "base_commit",
)

_DIFF_HEADER_RE = re.compile(r"(?m)^(?:\+\+\+|---)\s+\S+")


def parse_case_metadata(entry: replay_corpus.ReplayCaseEntry) -> dict[str, str]:
    """Parse structured key=value metadata from ``preregistration.preregistered_notes``."""
    meta: dict[str, str] = {}
    notes = entry.preregistration.preregistered_notes or ""
    for part in notes.split(";"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        meta[key.strip()] = value.strip()

    category = meta.get("category", "")
    language = meta.get("language", "")
    if category not in ALLOWED_CATEGORIES:
        raise ValueError(
            f"Case {entry.case_id!r} has invalid or missing category {category!r} "
            f"in preregistered_notes"
        )
    if language not in ALLOWED_LANGUAGES:
        raise ValueError(
            f"Case {entry.case_id!r} has invalid or missing language {language!r} "
            f"in preregistered_notes"
        )
    return meta


def check_case_leakage(entry: replay_corpus.ReplayCaseEntry) -> list[str]:
    """Verify prompt and guidance text do not leak diffs, commits, or hidden test paths."""
    violations: list[str] = []
    hidden_tokens: set[str] = set()
    for hf in entry.hidden.files:
        if hf.target_path:
            hidden_tokens.add(hf.target_path)
            hidden_tokens.add(Path(hf.target_path).name)
        if hf.source_path:
            hidden_tokens.add(hf.source_path)
            hidden_tokens.add(Path(hf.source_path).name)
        if hf.git_path:
            hidden_tokens.add(hf.git_path)

    commit_tokens: list[tuple[str, str]] = [
        ("base_commit", entry.source.base_commit),
    ]
    if entry.source.fix_commit:
        commit_tokens.append(("fix_commit", entry.source.fix_commit))

    for field_name, text in (
        ("task_prompt", entry.task_prompt),
        ("explicit_guidance", entry.explicit_guidance),
    ):
        for forbidden in LEAKAGE_FORBIDDEN_SUBSTRINGS:
            if forbidden in text:
                violations.append(
                    f"{entry.case_id}.{field_name} contains forbidden token {forbidden!r}"
                )
        if _DIFF_HEADER_RE.search(text):
            violations.append(f"{entry.case_id}.{field_name} contains unified diff header markers")
        for hidden_tok in sorted(hidden_tokens):
            if hidden_tok and hidden_tok in text:
                violations.append(
                    f"{entry.case_id}.{field_name} leaks hidden test path {hidden_tok!r}"
                )
        for label, sha in commit_tokens:
            if sha and (sha in text or sha[:10] in text):
                violations.append(
                    f"{entry.case_id}.{field_name} leaks {label} SHA {sha[:10]!r}"
                )

    return violations


def load_case_pool(
    input_dir: Path = DEFAULT_INPUT_DIR,
    filenames: Sequence[str] = INPUT_CASE_FILES,
) -> dict[str, replay_corpus.ReplayCaseEntry]:
    """Load and parse all external candidate case pools into a case_id map."""
    pool: dict[str, replay_corpus.ReplayCaseEntry] = {}
    for filename in filenames:
        path = Path(input_dir) / filename
        if not path.is_file():
            raise FileNotFoundError(f"Missing external case pool file: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"Case pool {path} must contain a JSON array")
        for idx, item in enumerate(raw):
            entry = replay_corpus.ReplayCaseEntry.from_dict(item, f"{filename}[{idx}]")
            if entry.case_id in pool:
                raise ValueError(
                    f"Duplicate case_id {entry.case_id!r} encountered in {filename}"
                )
            pool[entry.case_id] = entry
    return pool


def select_curated_cases(
    pool: Mapping[str, replay_corpus.ReplayCaseEntry],
    curated_ids: Sequence[str] = CURATED_CASE_IDS,
) -> tuple[replay_corpus.ReplayCaseEntry, ...]:
    """Select the exact curated case list in deterministic order."""
    if len(set(curated_ids)) != len(curated_ids):
        dups = sorted(cid for cid, count in Counter(curated_ids).items() if count > 1)
        raise ValueError(f"Duplicate case_id(s) in curated selection: {', '.join(dups)}")
    missing = [cid for cid in curated_ids if cid not in pool]
    if missing:
        raise ValueError(
            f"Curated case_id(s) missing from input pool: {', '.join(missing)}"
        )
    return tuple(pool[cid] for cid in curated_ids)


def validate_selection_invariants(
    entries: Sequence[replay_corpus.ReplayCaseEntry],
    *,
    expected_category_counts: Mapping[str, int] = EXPECTED_CATEGORY_COUNTS,
    expected_language_counts: Mapping[str, int] = EXPECTED_LANGUAGE_COUNTS,
    min_multi_file: int = MIN_MULTI_FILE_CASES,
) -> dict[str, Any]:
    """Enforce corpus size, category/language balance, multi-file floor, and leakage guards."""
    expected_total = sum(expected_category_counts.values())
    if len(entries) != expected_total:
        raise ValueError(
            f"Expected exactly {expected_total} selected cases, got {len(entries)}"
        )

    seen_ids: set[str] = set()
    category_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    difficulty_counts: Counter[str] = Counter()
    multi_file_count = 0
    leakage_errors: list[str] = []

    for entry in entries:
        if entry.case_id in seen_ids:
            raise ValueError(f"Duplicate selected case_id: {entry.case_id!r}")
        seen_ids.add(entry.case_id)

        meta = parse_case_metadata(entry)
        category_counts[meta["category"]] += 1
        language_counts[meta["language"]] += 1
        difficulty_counts[entry.preregistration.expected_difficulty] += 1

        actual_scope = "single_file" if len(entry.editable_files) == 1 else "multi_file"
        if entry.preregistration.file_scope_category != actual_scope:
            raise ValueError(
                f"Case {entry.case_id!r} declares file_scope_category="
                f"{entry.preregistration.file_scope_category!r} but has "
                f"{len(entry.editable_files)} editable file(s)"
            )
        if actual_scope == "multi_file":
            multi_file_count += 1

        if not entry.preregistration.leakage_checked:
            leakage_errors.append(
                f"{entry.case_id}.preregistration.leakage_checked must be true"
            )
        leakage_errors.extend(check_case_leakage(entry))

    if leakage_errors:
        raise ValueError(f"Leakage guard failed: {'; '.join(leakage_errors)}")

    if dict(category_counts) != dict(expected_category_counts):
        raise ValueError(
            "Category balance mismatch: "
            f"expected {dict(expected_category_counts)}, got {dict(category_counts)}"
        )

    if dict(language_counts) != dict(expected_language_counts):
        raise ValueError(
            "Language balance mismatch: "
            f"expected {dict(expected_language_counts)}, got {dict(language_counts)}"
        )

    if multi_file_count < min_multi_file:
        raise ValueError(
            f"Expected at least {min_multi_file} multi_file cases, got {multi_file_count}"
        )

    return {
        "total": len(entries),
        "categories": dict(sorted(category_counts.items())),
        "languages": dict(sorted(language_counts.items())),
        "difficulties": dict(sorted(difficulty_counts.items())),
        "multi_file_count": multi_file_count,
    }


def build_preregistration_spec(
    *,
    frozen: bool = True,
    manifest_sha256: str | None = None,
) -> replay_corpus.PreregistrationSpec:
    """Build the preregistration metadata for the unified 7-language 30-task C1 corpus."""
    return replay_corpus.PreregistrationSpec(
        protocol_version=PROTOCOL_VERSION,
        preregistration_id=PREREGISTRATION_ID,
        registered_at=REGISTERED_AT,
        frozen=frozen,
        hypothesis=(
            "On 30 unseen historical open-source bugfix commits spanning 7 executable "
            "languages (Python=6, JavaScript=4, TypeScript=4, C++=4, Bash=4, C=4, Rust=4) "
            "with balanced task strata (10 direct_caller, 10 indirect_caller, 10 control; "
            ">=3 multi_file), local caller context improves paired hidden-test pass rate "
            "on caller-dependent strata relative to baseline without exceeding the "
            "preregistered control non-inferiority tolerance, and one-factor-at-a-time "
            "Stage 2 ablations (cymbal v0.18.0, read-range=on, facts=on) quantify "
            "per-stratum accuracy, token, and latency trade-offs."
        ),
        primary_metric="paired_hidden_pass_rate_delta",
        secondary_metrics=(
            "correct_edit_rate",
            "smoke_pass_rate",
            "audit_verdict_breakdown_pass_fail_unknown_error",
            "false_approval_rate",
            "commit_rate",
            "first_editor_prompt_tokens",
            "cumulative_prompt_tokens",
            "completion_tokens",
            "total_tokens_p50_p95",
            "tokens_per_solved_task",
            "wall_clock_seconds_p50_p95",
            "mean_editor_calls",
            "caller_evidence_attachment_rate",
        ),
        inclusion_criteria=(
            "Genuine historical bugfix commit from an unseen compact open-source repository "
            "with base_commit = fix_commit~1 and zero external network or package installs "
            "required at verification time.",
            "Executable on host toolchains across 7 languages (Python, JavaScript, "
            "TypeScript via Node type-stripping, C++ via clang++, Bash, C via clang, "
            "Rust via rustc).",
            "SmokeSpec passes on both base_commit and fix_commit.",
            "External HiddenSpec (placement=hidden_dir, SHA-256 pinned) fails on "
            "base_commit and passes when only declared editable_files from fix_commit "
            "are applied.",
            "Editable files are compact enough for local 8B and 30B model context windows "
            "and were never used during feature or prompt development.",
        ),
        exclusion_criteria=(
            "Synthetic tasks or development fixtures previously used to tune prompts, "
            "heuristics, or verifier rules (heldout.py synthetic, issue5, and requests suites).",
            "Languages lacking an executable host runtime or Cymbal v0.18.0 indexing at "
            "freeze time (Java excluded due to missing OpenJDK on host; Groovy excluded "
            "due to missing Groovy runtime and Cymbal v0.18.0 .groovy indexing support).",
            "Commits requiring network access, heavy build systems, or external test "
            "dependencies at verification time.",
            "Doc-only, CI-only, formatting-only, or refactor commits lacking a clear "
            "fail-on-base / pass-on-fix behavioral regression.",
        ),
        stopping_rule=(
            "Freeze corpus at exactly 30 validated tasks (10 direct_caller, "
            "10 indirect_caller, 10 control across 7 languages; >=3 multi_file) prior to "
            "invoking any LLM on Stage 1 or Stage 2; no post-hoc task additions, removals, "
            "category reassignments, or prompt edits are permitted after manifest_sha256 "
            "is frozen."
        ),
        leakage_controls=(
            "External git clones (/tmp/issue5-replay-repos) and hidden verification files "
            "(/tmp/issue5-replay-corpus/hidden_*) stay outside the aider git repository.",
            "Materialization excludes tests/, docs/, benchmarks/, and examples/ via "
            "SourcePin include_globs/exclude_globs so fix-commit tests never enter "
            "candidate workspaces.",
            "All HiddenFileSpec items use placement='hidden_dir' with pinned SHA-256 "
            "digests and are staged only inside a temporary hidden directory after the "
            "model finishes editing.",
            "Automated leakage guards verify task_prompt and explicit_guidance contain no "
            "diff markers, commit SHAs, hidden file paths, or test assertions.",
        ),
        cutoff_date=None,
        cutoff_direction="on_or_after",
        manifest_sha256=manifest_sha256,
    )


def build_manifest_from_entries(
    entries: Sequence[replay_corpus.ReplayCaseEntry],
    *,
    freeze: bool = True,
    validate_invariants: bool = True,
) -> replay_corpus.ReplayManifest:
    """Assemble and optionally freeze a ReplayManifest from parsed ReplayCaseEntry items."""
    if validate_invariants:
        validate_selection_invariants(entries)

    prereg = build_preregistration_spec(frozen=freeze, manifest_sha256=None)
    manifest = replay_corpus.ReplayManifest(
        schema_version=replay_corpus.SCHEMA_VERSION,
        preregistration=prereg,
        cases=tuple(entries),
    )
    if freeze:
        digest = replay_corpus.compute_manifest_digest(manifest)
        manifest = replace(
            manifest,
            preregistration=replace(
                manifest.preregistration,
                frozen=True,
                manifest_sha256=digest,
            ),
        )
    return manifest


def build_manifest(
    input_dir: Path = DEFAULT_INPUT_DIR,
    *,
    freeze: bool = True,
    curated_ids: Sequence[str] = CURATED_CASE_IDS,
    filenames: Sequence[str] = INPUT_CASE_FILES,
) -> replay_corpus.ReplayManifest:
    """Load external case pools, select the 30 curated cases, and freeze the manifest."""
    pool = load_case_pool(input_dir, filenames=filenames)
    selected = select_curated_cases(pool, curated_ids=curated_ids)
    return build_manifest_from_entries(selected, freeze=freeze, validate_invariants=True)


def write_manifest(manifest: replay_corpus.ReplayManifest, out_path: Path) -> Path:
    """Write a canonical formatted ReplayManifest JSON to ``out_path``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.to_dict()
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble and freeze the unified 7-language 30-task Issue #5 C1 replay manifest."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing external cases_*.json pools (default: {DEFAULT_INPUT_DIR}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_PATH,
        help=f"Output frozen manifest JSON path (default: {DEFAULT_OUT_PATH}).",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run replay_corpus.validate_manifest(..., run_checks=True, require_frozen=True).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    manifest = build_manifest(args.input_dir, freeze=True)
    summary = validate_selection_invariants(manifest.cases)
    write_manifest(manifest, args.out)
    print(
        f"Wrote frozen manifest ({summary['total']} cases, "
        f"multi_file={summary['multi_file_count']}) to {args.out}\n"
        f"preregistration_id={manifest.preregistration.preregistration_id}\n"
        f"manifest_sha256={manifest.preregistration.manifest_sha256}\n"
        f"categories={summary['categories']}\n"
        f"languages={summary['languages']}"
    )

    if args.validate:
        report = replay_corpus.validate_manifest(
            manifest,
            base_dir=args.out.resolve().parent,
            run_checks=True,
            require_frozen=True,
        )
        if not report.ok:
            for err in report.errors:
                print(f"ERROR [{err.code}] ({err.case_id or 'manifest'}): {err.message}")
            return 1
        print(f"Validation OK ({len(report.case_results)}/{len(manifest.cases)} cases passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
