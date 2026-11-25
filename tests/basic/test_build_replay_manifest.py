"""Offline unit tests for scripts/local_context_trial/build_replay_manifest.py."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

TRIAL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "local_context_trial"
if str(TRIAL_DIR) not in sys.path:
    sys.path.insert(0, str(TRIAL_DIR))

import build_replay_manifest as brm  # noqa: E402
import replay_corpus  # noqa: E402

_LANG_CATEGORY_SLOTS: tuple[tuple[str, str], ...] = (
    # Python (6): 1 direct, 3 indirect, 2 control
    ("python", "direct_caller"),
    ("python", "indirect_caller"),
    ("python", "indirect_caller"),
    ("python", "indirect_caller"),
    ("python", "control"),
    ("python", "control"),
    # JavaScript (4): 1 direct, 1 indirect, 2 control
    ("javascript", "direct_caller"),
    ("javascript", "indirect_caller"),
    ("javascript", "control"),
    ("javascript", "control"),
    # TypeScript (4): 2 direct, 1 indirect, 1 control
    ("typescript", "direct_caller"),
    ("typescript", "direct_caller"),
    ("typescript", "indirect_caller"),
    ("typescript", "control"),
    # C++ (4): 1 direct, 1 indirect, 2 control
    ("cpp", "direct_caller"),
    ("cpp", "indirect_caller"),
    ("cpp", "control"),
    ("cpp", "control"),
    # Bash (4): 1 direct, 2 indirect, 1 control
    ("bash", "direct_caller"),
    ("bash", "indirect_caller"),
    ("bash", "indirect_caller"),
    ("bash", "control"),
    # C (4): 2 direct, 1 indirect, 1 control
    ("c", "direct_caller"),
    ("c", "direct_caller"),
    ("c", "indirect_caller"),
    ("c", "control"),
    # Rust (4): 2 direct, 1 indirect, 1 control
    ("rust", "direct_caller"),
    ("rust", "direct_caller"),
    ("rust", "indirect_caller"),
    ("rust", "control"),
)


def _make_entry(
    case_id: str,
    *,
    language: str,
    category: str,
    multi_file: bool = False,
    task_prompt: str | None = None,
    explicit_guidance: str | None = None,
    leakage_checked: bool = True,
    file_scope_override: str | None = None,
) -> replay_corpus.ReplayCaseEntry:
    editable = ("src/a.txt", "src/b.txt") if multi_file else ("src/a.txt",)
    file_scope = file_scope_override or ("multi_file" if multi_file else "single_file")
    base_sha = "1" * 40
    fix_sha = "2" * 40
    hidden_name = f"{case_id}_verify.py"
    return replay_corpus.ReplayCaseEntry(
        case_id=case_id,
        task_prompt=task_prompt or f"Fix behavioral regression in {editable[0]}.",
        explicit_guidance=explicit_guidance or "Preserve existing public behavior.",
        editable_files=editable,
        source=replay_corpus.SourcePin(
            repo_path=f"/fake/repos/{language}_repo",
            base_commit=base_sha,
            fix_commit=fix_sha,
            expected_tree_sha="3" * 40,
            expected_files_sha256="4" * 64,
            include_globs=("src/**",),
            exclude_globs=("tests/**",),
            version_label=f"{language}@11111111",
        ),
        hidden=replay_corpus.HiddenSpec(
            command=("{python}", f"{{hidden_dir}}/{hidden_name}"),
            files=(
                replay_corpus.HiddenFileSpec(
                    target_path=hidden_name,
                    source_path=f"/fake/hidden/{hidden_name}",
                    placement="hidden_dir",
                    sha256="5" * 64,
                ),
            ),
            env={},
            timeout_seconds=30.0,
            expected_exit_code=0,
        ),
        smoke=replay_corpus.SmokeSpec(
            command=("{python}", "-c", "pass"),
            timeout_seconds=15.0,
        ),
        preregistration=replay_corpus.CasePreregistration(
            selection_rationale="Historical bugfix satisfying inclusion criteria.",
            source_origin=f"org/{language}-repo#1",
            commit_date="2026-07-01",
            expected_difficulty="medium",
            file_scope_category=file_scope,
            leakage_checked=leakage_checked,
            preregistered_notes=(
                f"category={category}; language={language}; repo=org/{language}-repo"
            ),
        ),
    )


def _make_balanced_entries() -> list[replay_corpus.ReplayCaseEntry]:
    assert len(brm.CURATED_CASE_IDS) == len(_LANG_CATEGORY_SLOTS) == 30
    multi_indices = {0, 1, 6}
    return [
        _make_entry(
            cid,
            language=lang,
            category=cat,
            multi_file=(idx in multi_indices),
        )
        for idx, (cid, (lang, cat)) in enumerate(
            zip(brm.CURATED_CASE_IDS, _LANG_CATEGORY_SLOTS)
        )
    ]


def _write_synthetic_pools(input_dir: Path, entries: list[replay_corpus.ReplayCaseEntry]) -> None:
    by_file: dict[str, list[dict[str, object]]] = {name: [] for name in brm.INPUT_CASE_FILES}
    for entry in entries:
        lang = brm.parse_case_metadata(entry)["language"]
        if lang == "python":
            target = "cases_python.json"
        elif lang == "javascript":
            target = "cases_js.json"
        elif lang == "c":
            target = "cases_c.json"
        elif lang == "rust":
            target = "cases_rust.json"
        else:
            target = "cases_polyglot.json"
        by_file[target].append(entry.to_dict())

    for filename, rows in by_file.items():
        (input_dir / filename).write_text(json.dumps(rows, indent=2), encoding="utf-8")


def test_curated_constants_and_balance_invariants():
    assert len(brm.CURATED_CASE_IDS) == 30
    assert len(set(brm.CURATED_CASE_IDS)) == 30
    assert sum(brm.EXPECTED_CATEGORY_COUNTS.values()) == 30
    assert sum(brm.EXPECTED_LANGUAGE_COUNTS.values()) == 30
    assert len(brm.EXPECTED_LANGUAGE_COUNTS) == 7

    entries = _make_balanced_entries()
    summary = brm.validate_selection_invariants(entries)
    assert summary["total"] == 30
    assert summary["categories"] == {
        "control": 10,
        "direct_caller": 10,
        "indirect_caller": 10,
    }
    assert summary["languages"] == {
        "bash": 4,
        "c": 4,
        "cpp": 4,
        "javascript": 4,
        "python": 6,
        "rust": 4,
        "typescript": 4,
    }
    assert summary["multi_file_count"] == 3


def test_selection_invariants_reject_category_language_and_multifile_violations():
    entries = _make_balanced_entries()

    # Wrong total count
    with pytest.raises(ValueError, match="Expected exactly 30"):
        brm.validate_selection_invariants(entries[:-1])

    # Category imbalance (11 direct, 9 indirect)
    bad_cat = list(entries)
    bad_cat[1] = replace(
        bad_cat[1],
        preregistration=replace(
            bad_cat[1].preregistration,
            preregistered_notes="category=direct_caller; language=python; repo=org/py",
        ),
    )
    with pytest.raises(ValueError, match="Category balance mismatch"):
        brm.validate_selection_invariants(bad_cat)

    # Language imbalance (7 python, 3 javascript)
    bad_lang = list(entries)
    bad_lang[6] = replace(
        bad_lang[6],
        preregistration=replace(
            bad_lang[6].preregistration,
            preregistered_notes="category=direct_caller; language=python; repo=org/py",
        ),
    )
    with pytest.raises(ValueError, match="Language balance mismatch"):
        brm.validate_selection_invariants(bad_lang)

    # Too few multi_file cases (< 3)
    too_few_multi = list(entries)
    too_few_multi[6] = _make_entry(
        too_few_multi[6].case_id,
        language="javascript",
        category="direct_caller",
        multi_file=False,
    )
    with pytest.raises(ValueError, match="Expected at least 3 multi_file"):
        brm.validate_selection_invariants(too_few_multi)

    # file_scope_category inconsistent with editable_files length
    bad_scope = list(entries)
    bad_scope[0] = _make_entry(
        bad_scope[0].case_id,
        language="python",
        category="direct_caller",
        multi_file=True,
        file_scope_override="single_file",
    )
    with pytest.raises(ValueError, match="file_scope_category"):
        brm.validate_selection_invariants(bad_scope)


@pytest.mark.parametrize(
    ("field_name", "bad_text", "expected_match"),
    [
        ("task_prompt", "Apply diff --git a/x b/x to fix.", "diff --git"),
        ("explicit_guidance", "Hunk starts at @@ -10,3 +10,4 @@", "@@ "),
        ("task_prompt", "See +++ b/src/a.txt for patch.", "+++ b/"),
        ("task_prompt", "Header:\n--- old.py\n+++ new.py", "unified diff header"),
        (
            "task_prompt",
            "Check /tmp/issue5-replay-corpus/hidden_py/case.py",
            "/tmp/issue5-replay-corpus",
        ),
        ("explicit_guidance", "Run {hidden_dir}/runner.sh", "{hidden_dir}"),
        ("task_prompt", "Matches sample_case_verify.py", "leaks hidden test path"),
        ("explicit_guidance", "Inspect /fake/hidden/sample_case_verify.py", "leaks hidden test"),
        ("task_prompt", "Commit 2222222222 fixed this.", "leaks fix_commit SHA"),
        ("explicit_guidance", "Parent 1111111111 had bug.", "leaks base_commit SHA"),
    ],
)
def test_leakage_guards_catch_diffs_hidden_paths_and_shas(
    field_name: str,
    bad_text: str,
    expected_match: str,
):
    kwargs = {field_name: bad_text}
    entry = _make_entry(
        "sample_case",
        language="python",
        category="control",
        **kwargs,
    )
    violations = brm.check_case_leakage(entry)
    assert any(expected_match in v for v in violations), violations


def test_leakage_checked_false_fails_validation():
    entries = _make_balanced_entries()
    entries[0] = replace(
        entries[0],
        preregistration=replace(entries[0].preregistration, leakage_checked=False),
    )
    with pytest.raises(ValueError, match="leakage_checked must be true"):
        brm.validate_selection_invariants(entries)


def test_select_curated_cases_errors_on_missing_or_duplicate_ids():
    entries = _make_balanced_entries()
    pool = {e.case_id: e for e in entries}

    with pytest.raises(ValueError, match="Duplicate case_id"):
        brm.select_curated_cases(pool, curated_ids=(entries[0].case_id, entries[0].case_id))

    del pool[entries[-1].case_id]
    with pytest.raises(ValueError, match="missing from input pool"):
        brm.select_curated_cases(pool)


def test_preregistration_schema_and_freeze_digest():
    entries = _make_balanced_entries()

    unfrozen = brm.build_manifest_from_entries(entries, freeze=False)
    assert unfrozen.schema_version == replay_corpus.SCHEMA_VERSION
    assert unfrozen.preregistration.protocol_version == "1.0"
    assert unfrozen.preregistration.preregistration_id == "issue5-c1-polyglot-v1"
    assert unfrozen.preregistration.registered_at == "2026-11-25T16:40:00+02:00"
    assert unfrozen.preregistration.frozen is False
    assert unfrozen.preregistration.manifest_sha256 is None

    frozen = brm.build_manifest_from_entries(entries, freeze=True)
    expected_digest = replay_corpus.compute_manifest_digest(frozen)
    assert frozen.preregistration.frozen is True
    assert frozen.preregistration.manifest_sha256 == expected_digest
    assert len(expected_digest) == 64

    # Digest stability across independent builds
    frozen_again = brm.build_manifest_from_entries(entries, freeze=True)
    assert frozen_again.preregistration.manifest_sha256 == expected_digest

    # Tampering with any case prompt invalidates the pinned digest
    tampered_cases = (
        replace(frozen.cases[0], task_prompt=frozen.cases[0].task_prompt + " extra"),
        *frozen.cases[1:],
    )
    tampered = replace(frozen, cases=tampered_cases)
    assert replay_corpus.compute_manifest_digest(tampered) != expected_digest


def test_cli_end_to_end_offline_pools_and_roundtrip(tmp_path: Path):
    entries = _make_balanced_entries()
    input_dir = tmp_path / "pools"
    input_dir.mkdir()
    _write_synthetic_pools(input_dir, entries)
    out_path = tmp_path / "out" / "manifest.json"

    rc = brm.main(["--input-dir", str(input_dir), "--out", str(out_path)])
    assert rc == 0
    assert out_path.is_file()

    loaded, _ = replay_corpus.load_manifest(out_path)
    assert len(loaded.cases) == 30
    assert [c.case_id for c in loaded.cases] == list(brm.CURATED_CASE_IDS)
    assert loaded.preregistration.frozen is True
    assert loaded.preregistration.protocol_version == "1.0"
    assert loaded.preregistration.preregistration_id == "issue5-c1-polyglot-v1"
    assert loaded.preregistration.registered_at == "2026-11-25T16:40:00+02:00"
    assert loaded.preregistration.manifest_sha256 == replay_corpus.compute_manifest_digest(loaded)

    # Duplicate case across pool files fails loudly
    dup_file = input_dir / "cases_c.json"
    dup_rows = json.loads(dup_file.read_text(encoding="utf-8"))
    dup_rows.append(entries[0].to_dict())
    dup_file.write_text(json.dumps(dup_rows), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate case_id"):
        brm.load_case_pool(input_dir)
