"""Offline rescorer for local-context trial JSONL results.

Re-evaluates derived scoring fields (`only_target_modified`, `commit_outcome`,
`commit_passed`, `patch_hidden_passed`, `passed`, `strict_accepted`, and
`false_approval`) via `run.score_attempt_outcomes` without mutating the source
JSONL or preserved attempt repositories.
"""

import argparse
from collections.abc import Iterable, Mapping
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent))
import heldout  # noqa: E402
import run  # noqa: E402

_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git (?:a/(.+?) b/(.+)|\"a/(.+?)\" \"b/(.+)\")$")
_SCORED_FIELDS = (
    "only_target_modified",
    "commit_outcome",
    "commit_passed",
    "patch_hidden_passed",
    "passed",
    "strict_accepted",
    "false_approval",
)


def parse_patch_changed_paths(patch_text: str) -> list[str]:
    """Extract ordered, unique repo-relative file paths from unified `diff --git` headers."""
    paths: list[str] = []
    seen: set[str] = set()
    for line in patch_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        match = _DIFF_GIT_HEADER_RE.match(line)
        if not match:
            continue
        if match.group(1) is not None and match.group(2) is not None:
            left, right = match.group(1), match.group(2)
        else:
            left = _unescape_git_quoted(match.group(3) or "")
            right = _unescape_git_quoted(match.group(4) or "")
        candidates = (right,) if left == right else (left, right)
        for path in candidates:
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _unescape_git_quoted(raw: str) -> str:
    """Decode simple C-style escapes used in quoted git diff headers."""
    return raw.encode("utf-8").decode("unicode_escape")


def load_targets_from_manifest(manifest_path: str | Path) -> dict[str, tuple[str, ...]]:
    """Load `case_id -> editable_files` mapping from a replay corpus manifest JSON."""
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"Manifest {manifest_path} does not contain a 'cases' list")
    targets: dict[str, tuple[str, ...]] = {}
    for entry in cases:
        case_id = entry.get("case_id")
        editable = entry.get("editable_files")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"Invalid case_id in manifest {manifest_path}: {entry!r}")
        if not isinstance(editable, list) or not all(isinstance(p, str) and p for p in editable):
            raise ValueError(f"Invalid editable_files for {case_id!r} in {manifest_path}")
        targets[case_id] = tuple(editable)
    return targets


def load_targets_for_suite(
    suite: str | None = None,
    manifest_path: str | Path | None = None,
) -> dict[str, tuple[str, ...]]:
    """Resolve `case_name -> targets` mapping from a manifest path and/or named suite."""
    if manifest_path is not None:
        if suite is not None and suite != "replay":
            raise ValueError("--manifest is only valid alone or with --suite replay")
        return load_targets_from_manifest(manifest_path)

    if suite == "replay":
        raise ValueError("--suite replay requires --manifest <path>")
    if suite == "synthetic":
        specs = list(heldout.CASE_SPECS)
    elif suite == "issue5":
        import issue5_cases

        specs = issue5_cases.cases()
    elif suite == "requests":
        import requests_cases

        specs = requests_cases.cases()
    else:
        raise ValueError(
            f"Unsupported suite {suite!r}; specify --manifest or --suite replay|synthetic|issue5|requests"
        )
    return {spec.name: (spec.editable_file, *spec.additional_editable) for spec in specs}


def locate_attempt_repo(row: Mapping[str, object]) -> Path | None:
    """Locate the preserved `repo/` directory for a trial row without mutating disk.

    Checks exact `run.control_tag(...)` folder names under `row["workdir"]` first,
    then falls back to an unambiguous scan matching `*-<case>-<arm>`.
    """
    workdir_raw = row.get("workdir")
    case_name = row.get("case")
    arm = row.get("arm")
    if not workdir_raw or not case_name or not arm:
        return None
    workdir = Path(str(workdir_raw))
    if not workdir.is_dir():
        return None

    round_id = row.get("round")
    spec = row.get("spec")
    backend = row.get("local_callers_backend")
    read_range = row.get("local_callers_read_range")
    facts = row.get("project_facts")
    facts_db = row.get("facts_db")
    facts_file = row.get("facts_file")
    lc_enabled = row.get("local_callers_enabled")

    candidate_names: list[str] = []
    for lc_val in (None, lc_enabled):
        ctrl_suffix = run.control_tag(
            backend=str(backend) if backend is not None else None,
            read_range=bool(read_range) if read_range is not None else None,
            facts=bool(facts) if facts is not None else None,
            facts_db=str(facts_db) if facts_db is not None else None,
            facts_file=str(facts_file) if facts_file is not None else None,
            local_callers=bool(lc_val) if lc_val is not None else None,
        )
        tags = []
        if round_id is not None and spec is not None:
            tags.append(f"{round_id}-{spec}-{ctrl_suffix}" if ctrl_suffix else f"{round_id}-{spec}")
        if round_id is not None:
            tags.append(f"{round_id}-{ctrl_suffix}" if ctrl_suffix else str(round_id))
        for tag in tags:
            name = f"{tag}-{case_name}-{arm}"
            if name not in candidate_names:
                candidate_names.append(name)

    for folder_name in candidate_names:
        repo = workdir / folder_name / "repo"
        if (repo / ".git").exists():
            return repo

    suffix = f"-{case_name}-{arm}"
    prefixes = []
    if round_id is not None and spec is not None:
        prefixes.append(f"{round_id}-{spec}-")
    prefixes.append("")

    for prefix in prefixes:
        matches = [
            entry / "repo"
            for entry in sorted(workdir.iterdir())
            if entry.is_dir()
            and entry.name.startswith(prefix)
            and entry.name.endswith(suffix)
            and (entry / "repo" / ".git").exists()
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _resolve_repo_base_rev(repo: Path, commits_added: int) -> str:
    """Determine base_rev inside a preserved attempt repo using read-only git calls."""
    if commits_added > 0:
        candidate = f"HEAD~{commits_added}"
        proc = subprocess.run(
            ["git", "--no-optional-locks", "rev-parse", "--verify", candidate],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        roots = subprocess.check_output(
            ["git", "--no-optional-locks", "rev-list", "--max-parents=0", "HEAD"],
            cwd=repo,
            text=True,
        ).splitlines()
        if roots:
            return roots[0].strip()
    return "HEAD"


def inspect_repo_changes(repo: Path, *, commits_added: int = 0) -> tuple[list[str], list[str], str]:
    """Read `(changed_paths, changed_status_lines, gitdiff)` from `repo` without writes."""
    base_rev = _resolve_repo_base_rev(repo, commits_added)
    changed_status = subprocess.check_output(
        [
            "git",
            "--no-optional-locks",
            "status",
            "--porcelain",
            "--no-renames",
            "-z",
            "--untracked-files=all",
        ],
        cwd=repo,
        text=True,
    )
    changed_status_lines = changed_status.split("\0")[:-1]
    changed_paths = subprocess.check_output(
        ["git", "--no-optional-locks", "diff", "--name-only", "--no-renames", "-z", base_rev],
        cwd=repo,
        text=True,
    ).split("\0")[:-1]
    gitdiff = subprocess.check_output(
        ["git", "--no-optional-locks", "diff", base_rev],
        cwd=repo,
        text=True,
    )
    return changed_paths, changed_status_lines, gitdiff


def _resolve_row_arm_flags(row: Mapping[str, object]) -> tuple[str, ...]:
    raw_flags = row.get("arm_flags")
    if isinstance(raw_flags, (list, tuple)):
        return tuple(str(flag) for flag in raw_flags)
    arm = str(row.get("arm") or "baseline")
    lc_enabled = row.get("local_callers_enabled")
    return tuple(
        run.resolve_arm_flags(
            arm,
            backend=(
                str(row["local_callers_backend"])
                if row.get("local_callers_backend") is not None
                else None
            ),
            read_range=(
                bool(row["local_callers_read_range"])
                if row.get("local_callers_read_range") is not None
                else None
            ),
            facts=bool(row["project_facts"]) if row.get("project_facts") is not None else None,
            facts_db=str(row["facts_db"]) if row.get("facts_db") is not None else None,
            facts_file=str(row["facts_file"]) if row.get("facts_file") is not None else None,
            local_callers=bool(lc_enabled) if lc_enabled is not None else None,
        )
    )


def rescore_row(
    row: Mapping[str, object],
    targets: Iterable[str],
    *,
    repo_dir: Path | None = None,
) -> tuple[dict, str, dict[str, tuple[object, object]]]:
    """Re-evaluate scoring fields for a single trial row and return `(new_row, source, diffs)`.

    Preconditions:
        `targets` contains the valid editable repo-relative paths for `row["case"]`.
    Postconditions:
        Returns a fresh row dict (input `row` is never mutated), `rescore_source`
        (`"repo"` or `"patch_only"`), and a mapping of changed scored fields
        `field -> (old_value, new_value)`.
    """
    exit_code = int(row.get("exit", 0))
    commits_added = int(row.get("commits_added") or 0)
    patch_text = str(row.get("patch") or "")

    resolved_repo = repo_dir if repo_dir is not None else locate_attempt_repo(row)
    if resolved_repo is not None and (resolved_repo / ".git").exists():
        changed_paths, changed_status_lines, repo_diff = inspect_repo_changes(
            resolved_repo, commits_added=commits_added
        )
        gitdiff = patch_text or repo_diff
        rescore_source = "repo"
    else:
        changed_paths = parse_patch_changed_paths(patch_text)
        changed_status_lines = [f" M {path}" for path in changed_paths]
        gitdiff = patch_text
        rescore_source = "patch_only"

    editor_calls = row.get("editor_calls")
    trial_error = row.get("trial_error")
    if editor_calls is not None:
        missing_completion = (exit_code != 124 and int(editor_calls) == 0) or (
            trial_error == "No editor completion recorded"
        )
    else:
        missing_completion = trial_error == "No editor completion recorded"

    arm_flags = _resolve_row_arm_flags(row)
    if row.get("local_callers_enabled") is not None:
        local_callers_enabled = bool(row["local_callers_enabled"])
    else:
        local_callers_enabled = "--local-callers" in arm_flags

    if "audit_passed" in row:
        audit_passed = row["audit_passed"]
    else:
        verdicts = row.get("audit_verdicts") or []
        _, audit_passed = run.classify_audit_outcome([str(v) for v in verdicts])

    outcomes = run.score_attempt_outcomes(
        exit_code=exit_code,
        missing_completion=missing_completion,
        changed_status_lines=changed_status_lines,
        changed_paths=changed_paths,
        targets=targets,
        gitdiff=gitdiff,
        commits_added=commits_added,
        smoke_passed=bool(row.get("smoke_passed")),
        hidden_passed=bool(row.get("hidden_passed")),
        local_callers_enabled=local_callers_enabled,
        arm_flags=arm_flags,
        audit_passed=audit_passed if isinstance(audit_passed, bool) else None,
    )

    new_row = dict(row)
    new_row.update(outcomes)
    existing_meta = row.get("metadata")
    new_meta = dict(existing_meta) if isinstance(existing_meta, dict) else {}
    new_meta["rescore_source"] = rescore_source
    new_row["metadata"] = new_meta

    diffs: dict[str, tuple[object, object]] = {}
    for field in _SCORED_FIELDS:
        old_val = row.get(field)
        new_val = new_row.get(field)
        if old_val != new_val:
            diffs[field] = (old_val, new_val)
    return new_row, rescore_source, diffs


def rescore_jsonl(
    input_path: str | Path,
    output_path: str | Path,
    targets_by_case: Mapping[str, tuple[str, ...]],
) -> dict:
    """Rescore all trial rows in `input_path` and write derived JSONL to `output_path`."""
    in_path = Path(input_path).resolve()
    out_path = Path(output_path).resolve()
    if in_path == out_path:
        raise ValueError("Output path must differ from input JSONL to avoid in-place mutation")

    out_lines: list[str] = []
    source_counts = {"repo": 0, "patch_only": 0}
    changed_rows: list[dict] = []
    total_rows = 0

    for raw_line in in_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        record = json.loads(raw_line)
        if isinstance(record, dict) and record.get("record_type") == "metadata":
            out_lines.append(json.dumps(record))
            continue
        if not isinstance(record, dict):
            raise ValueError(f"Expected JSON object row in {in_path}, got {type(record).__name__}")
        case_name = str(record.get("case") or "")
        if case_name not in targets_by_case:
            raise KeyError(f"Case {case_name!r} not found in targets mapping")

        total_rows += 1
        rescored, source, diffs = rescore_row(record, targets_by_case[case_name])
        source_counts[source] += 1
        out_lines.append(json.dumps(rescored))
        if diffs:
            changed_rows.append(
                {
                    "index": total_rows - 1,
                    "round": rescored.get("round"),
                    "spec": rescored.get("spec"),
                    "case": case_name,
                    "arm": rescored.get("arm"),
                    "rescore_source": source,
                    "diffs": diffs,
                }
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
    return {
        "input": str(in_path),
        "output": str(out_path),
        "total_rows": total_rows,
        "rescore_source": source_counts,
        "changed_count": len(changed_rows),
        "changed_rows": changed_rows,
    }


def format_rescore_summary(summary: dict) -> str:
    """Format a compact human-readable report of rescored rows."""
    sources = summary["rescore_source"]
    lines = [
        (
            f"Rescored {summary['total_rows']} rows -> {summary['output']} "
            f"(repo={sources['repo']}, patch_only={sources['patch_only']}, "
            f"changed={summary['changed_count']})"
        )
    ]
    for entry in summary["changed_rows"]:
        diffs = entry["diffs"]
        if "only_target_modified" in diffs or "passed" in diffs:
            diff_parts = [f"{k}: {old}->{new}" for k, (old, new) in diffs.items()]
            lines.append(
                f"  [row {entry['index']}] round={entry['round']} spec={entry['spec']} "
                f"case={entry['case']} arm={entry['arm']} source={entry['rescore_source']} "
                + " | ".join(diff_parts)
            )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_jsonl", help="path to existing trial JSONL file (read-only)")
    parser.add_argument(
        "--out",
        required=True,
        help="destination path for derived rescored JSONL (must differ from input)",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="path to a replay corpus manifest JSON supplying case editable_files",
    )
    parser.add_argument(
        "--suite",
        choices=["replay", "synthetic", "issue5", "requests"],
        default=None,
        help="benchmark suite supplying CaseSpec targets",
    )
    return parser


def main(argv: list[str] | None = None) -> dict:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        targets_by_case = load_targets_for_suite(
            suite=args.suite,
            manifest_path=args.manifest,
        )
        summary = rescore_jsonl(args.input_jsonl, args.out, targets_by_case)
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(format_rescore_summary(summary))
    return summary


if __name__ == "__main__":
    main()
