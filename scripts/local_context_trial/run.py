"""Run paired Aider trials in fresh repositories. Hidden assertions run after Aider exits."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
MODEL = "lm_studio/qwen/qwen3.6-35b-a3b"
SCHEMA_VERSION = 2

_TRIAL_USAGE_RE = re.compile(r"^Trial usage:\s+(\S+)\s+(.+?)\s*$", re.M)
_USAGE_KV_RE = re.compile(
    r"\b(prompt|input|sent|completion|output|received|total|cached)=(\d+|None)\b"
)
_REPO_STATE_CACHE: dict[str, object] | None = None

ARM_FLAGS: dict[str, list[str]] = {
    "baseline": ["--no-local-callers"],
    "passive": [
        "--local-callers",
        "--local-callers-mode",
        "passive",
        "--no-local-callers-audit",
    ],
    "contract": [
        "--local-callers",
        "--local-callers-mode",
        "contract",
        "--no-local-callers-audit",
    ],
    "audit": [
        "--local-callers",
        "--local-callers-mode",
        "passive",
        "--local-callers-audit",
    ],
    "contract+audit": [
        "--local-callers",
        "--local-callers-mode",
        "contract",
        "--local-callers-audit",
    ],
}
VALID_ARMS = tuple(ARM_FLAGS.keys())

# Each task exposes the same starting files in all arms. The expected convention
# is visible in an ordinary downstream caller. Hidden tests stay outside Git.
CASES = [
    (
        "cursor",
        "def decode_cursor(value):\n    return int(value)\n",
        "from api import decode_cursor\n\ndef page(value):\n"
        "    offset = decode_cursor(value)\n    if offset == -7:\n"
        "        return 'restart'\n    return offset + 1\n",
        "Update decode_cursor to handle a blank string according to the existing pagination"
        " convention. Preserve valid integer inputs.",
        "from api import decode_cursor\nassert decode_cursor('8') == 8",
        "from consumer import page\nassert page('') == 'restart'\nassert page('8') == 9",
    ),
    (
        "labels",
        "def parse_labels(value):\n    return value.split(',')\n",
        "from api import parse_labels\n\ndef render(value):\n"
        "    labels = parse_labels(value)\n    if labels == ['(unassigned)']:\n"
        "        return 'empty'\n    return '|'.join(labels)\n",
        "Update parse_labels to trim whitespace, remove blank labels, and honor the existing"
        " renderer convention when all labels are blank.",
        "from api import parse_labels\nassert parse_labels('a,b') == ['a','b']",
        "from consumer import render\nassert render(' , ') == 'empty'\n"
        "assert render(' a, ,b ') == 'a|b'",
    ),
    (
        "retry",
        "def retry_delay(raw):\n    return int(raw)\n",
        "from api import retry_delay\n\ndef schedule(raw):\n"
        "    delay = retry_delay(raw)\n    if delay == 'defer':\n"
        "        return 'manual'\n    return delay + 1\n",
        "Update retry_delay to handle malformed numeric strings using the scheduler's existing"
        " fallback. Keep the valid numeric path.",
        "from api import retry_delay\nassert retry_delay('0') == 0",
        "from consumer import schedule\nassert schedule('bad') == 'manual'\n"
        "assert schedule('3') == 4",
    ),
    (
        "clamp",
        "def clamp_volume(value):\n    return int(value)\n",
        "from api import clamp_volume\n\ndef display(raw):\n"
        "    return str(clamp_volume(raw)) + '%'\n",
        "Update clamp_volume to clamp integers to the inclusive range 0 through 100.",
        "from api import clamp_volume\nassert clamp_volume(42) == 42",
        "from consumer import display\n"
        "assert [display(n) for n in (-1, 42, 101)] == ['0%', '42%', '100%']",
    ),
]


EXPECTED_BEHAVIOR = {
    "cursor": "A blank input must cause the pagination caller to restart.",
    "labels": "The renderer must display its empty state for all-blank input.",
    "retry": "Malformed input must put the scheduler into manual mode.",
    "clamp": "The display must show 0% below range and 100% above range.",
}


@dataclass(frozen=True)
class CaseSpec:
    """Language-agnostic specification for a single benchmark task."""

    name: str
    files: dict[str, str]
    editable_file: str
    language: str
    task: str
    explicit: str
    test_cmd: str
    hidden_cmd: list[str]
    kind: str = "caller"
    additional_editable: tuple[str, ...] = ()
    seed_facts: tuple[dict[str, object], ...] = ()


def py_case(
    name: str,
    files: dict[str, str],
    task: str,
    hidden: str,
    explicit: str,
    kind: str = "caller",
    editable_file: str = "api.py",
    smoke_file: str = "smoke.py",
    seed_facts: tuple[dict[str, object], ...] = (),
) -> CaseSpec:
    """Build a Python CaseSpec."""
    return CaseSpec(
        name=name,
        files=files,
        editable_file=editable_file,
        language="python",
        task=task,
        explicit=explicit,
        test_cmd=f"{shlex.quote(sys.executable)} {smoke_file}",
        hidden_cmd=[sys.executable, "-c", hidden],
        kind=kind,
        seed_facts=tuple(seed_facts),
    )


def js_case(
    name: str,
    files: dict[str, str],
    task: str,
    hidden: str,
    explicit: str,
    kind: str = "caller",
    editable_file: str = "api.mjs",
    smoke_file: str = "smoke.mjs",
    seed_facts: tuple[dict[str, object], ...] = (),
) -> CaseSpec:
    """Build a Node.js ESM CaseSpec."""
    return CaseSpec(
        name=name,
        files=files,
        editable_file=editable_file,
        language="js",
        task=task,
        explicit=explicit,
        test_cmd=f"node {smoke_file}",
        hidden_cmd=["node", "--input-type=module", "-e", hidden],
        kind=kind,
        seed_facts=tuple(seed_facts),
    )


def resolve_arm_flags(
    arm: str,
    *,
    backend: str | None = None,
    read_range: bool | None = None,
    facts: bool | None = None,
    facts_db: str | None = None,
    facts_file: str | None = None,
    local_callers: bool | None = None,
) -> list[str]:
    """Return CLI flags for a trial arm ('context' aliases 'passive').

    Optional independent controls override local callers (--local-callers /
    --no-local-callers), read-range (--local-callers-read-range /
    --no-local-callers-read-range), backend (--local-callers-backend), and
    Issue 5 project facts (--facts / --no-facts, --facts-db, --facts-file).
    Leaving any control as None preserves existing defaults.
    """
    if arm == "context":
        flags = list(ARM_FLAGS["passive"])
    elif arm in ARM_FLAGS:
        flags = list(ARM_FLAGS[arm])
    else:
        raise ValueError(f"Unknown arm {arm!r}; expected one of {', '.join(VALID_ARMS)}")

    if local_callers is not None:
        desired = "--local-callers" if local_callers else "--no-local-callers"
        flags = [flag for flag in flags if flag not in ("--local-callers", "--no-local-callers")]
        flags.insert(0, desired)

    if read_range is True:
        flags.append("--local-callers-read-range")
    elif read_range is False:
        flags.append("--no-local-callers-read-range")

    if backend is not None:
        cleaned = backend.strip()
        if not cleaned:
            raise ValueError("backend must be a non-empty string when specified")
        flags.extend(["--local-callers-backend", cleaned])

    if facts is True:
        flags.append("--facts")
    elif facts is False:
        flags.append("--no-facts")

    if facts_db is not None:
        cleaned = facts_db.strip()
        if not cleaned:
            raise ValueError("facts_db must be a non-empty string when specified")
        flags.extend(["--facts-db", cleaned])

    if facts_file is not None:
        cleaned = facts_file.strip()
        if not cleaned:
            raise ValueError("facts_file must be a non-empty string when specified")
        flags.extend(["--facts-file", cleaned])

    return flags


def _slugify_control(value: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("_") or fallback


def control_tag(
    backend: str | None = None,
    read_range: bool | None = None,
    facts: bool | None = None,
    facts_db: str | None = None,
    facts_file: str | None = None,
    local_callers: bool | None = None,
) -> str:
    """Return a filesystem-safe tag suffix for non-default independent controls."""
    parts: list[str] = []
    if backend is not None:
        parts.append(f"be-{_slugify_control(backend, 'backend')}")
    if read_range is not None:
        parts.append("rr-on" if read_range else "rr-off")
    if facts is not None:
        parts.append("facts-on" if facts else "facts-off")
    if facts_db is not None:
        parts.append(f"fdb-{_slugify_control(facts_db, 'db')}")
    if facts_file is not None:
        parts.append(f"ffile-{_slugify_control(facts_file, 'file')}")
    if local_callers is not None:
        parts.append("lc-on" if local_callers else "lc-off")
    return "-".join(parts)


def parse_trial_usage(log_text: str) -> dict:
    """Parse Trial usage lines into measured call counts and token totals."""
    calls: list[dict] = []
    for match in _TRIAL_USAGE_RE.finditer(log_text):
        role = match.group(1)
        payload = match.group(2).strip()
        prompt: int | None = None
        completion: int | None = None
        total: int | None = None

        if payload == "None":
            pass
        elif payload.isdecimal():
            total = int(payload)
        else:
            kv = dict(_USAGE_KV_RE.findall(payload))
            if kv:
                for key in ("prompt", "input", "sent"):
                    if key in kv:
                        prompt = int(kv[key]) if kv[key].isdecimal() else None
                        break
                for key in ("completion", "output", "received"):
                    if key in kv:
                        completion = int(kv[key]) if kv[key].isdecimal() else None
                        break
                if "total" in kv:
                    total = int(kv["total"]) if kv["total"].isdecimal() else None
                elif prompt is not None and completion is not None:
                    total = prompt + completion
            else:
                parts = payload.split()
                if len(parts) == 2 and all(part.isdecimal() for part in parts):
                    prompt = int(parts[0])
                    completion = int(parts[1])
                    total = prompt + completion
                else:
                    continue

        calls.append(
            {
                "role": role,
                "total": total,
                "prompt": prompt,
                "completion": completion,
            }
        )

    calls_by_role: dict[str, int] = {}
    tokens_by_role: dict[str, int | None] = {}
    for call in calls:
        role = call["role"]
        calls_by_role[role] = calls_by_role.get(role, 0) + 1

    for role in calls_by_role:
        role_totals = [call["total"] for call in calls if call["role"] == role]
        tokens_by_role[role] = (
            sum(role_totals) if all(val is not None for val in role_totals) else None
        )

    measured = bool(calls) and all(call["total"] is not None for call in calls)
    total_tokens = sum(call["total"] for call in calls) if measured else None

    if calls and all(call["prompt"] is not None for call in calls):
        prompt_tokens: int | None = sum(call["prompt"] for call in calls)
    else:
        prompt_tokens = None

    if calls and all(call["completion"] is not None for call in calls):
        completion_tokens: int | None = sum(call["completion"] for call in calls)
    else:
        completion_tokens = None

    editor_calls = calls_by_role.get("editor", 0)
    audit_calls = calls_by_role.get("audit", 0)
    editor_tokens = tokens_by_role.get("editor", 0 if measured else None)
    audit_tokens = tokens_by_role.get("audit", 0 if measured else None)

    return {
        "measured": measured,
        "tokens": total_tokens,
        "editor_tokens": editor_tokens,
        "audit_tokens": audit_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "editor_calls": editor_calls,
        "audit_calls": audit_calls,
        "total_calls": len(calls),
        "extra_calls": max(0, len(calls) - 1),
        "calls_by_role": calls_by_role,
        "tokens_by_role": tokens_by_role,
    }


def classify_audit_outcome(verdicts: list[str]) -> tuple[str, bool | None]:
    """Return (audit_outcome, audit_passed) from parsed audit verdicts."""
    if not verdicts:
        return "not_run", None
    final = verdicts[-1].upper()
    if final == "PASS":
        return "pass", True
    if final == "FAIL":
        return "fail", False
    if final == "UNKNOWN":
        return "unknown", False
    if final == "ERROR":
        return "error", False
    return final.lower(), False


def classify_commit_outcome(
    exit_code: int,
    changed_lines: list[str],
    gitdiff: str,
    only_target_modified: bool,
    commits_added: int = 0,
) -> tuple[str, bool]:
    """Return (commit_outcome, commit_passed) for repo state after Aider exits."""
    if exit_code == 124:
        return "timeout", False
    if exit_code != 0:
        return "error", False
    has_change = bool(gitdiff.strip()) or commits_added > 0
    if only_target_modified and has_change:
        return ("committed" if commits_added > 0 else "applied"), True
    if not changed_lines and not has_change:
        return "no_change", False
    return "out_of_scope", False


def _git_repo_state(repo_root: Path = ROOT) -> dict[str, object]:
    """Capture git commit SHA and dirty flag for repo_root without failing outside git."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return {"git_commit": commit, "git_dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_dirty": None}


def _default_repo_state() -> dict[str, object]:
    global _REPO_STATE_CACHE
    if _REPO_STATE_CACHE is None:
        _REPO_STATE_CACHE = _git_repo_state(ROOT)
    return dict(_REPO_STATE_CACHE)


def collect_run_metadata(
    *,
    model: str = MODEL,
    suite: str = "legacy",
    map_tokens: int = 0,
    rounds: int | None = None,
    seed: int | None = None,
    arms: list[str] | tuple[str, ...] | None = None,
    specs: list[str] | tuple[str, ...] | None = None,
    cases: list[str] | tuple[str, ...] | None = None,
    controls: dict | None = None,
    repo_root: Path = ROOT,
    run_id: str | None = None,
) -> dict:
    """Build reproducible run provenance metadata."""
    git_state = _git_repo_state(repo_root)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or uuid.uuid4().hex[:12],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git_state["git_commit"],
        "git_dirty": git_state["git_dirty"],
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "model": model,
        "suite": suite,
        "map_tokens": map_tokens,
        "rounds": rounds,
        "seed": seed,
        "arms": list(arms) if arms is not None else None,
        "specs": list(specs) if specs is not None else None,
        "cases": list(cases) if cases is not None else None,
        "controls": controls,
    }


def write_run_metadata(path: Path, metadata: dict) -> None:
    """Persist run metadata JSON, preserving prior runs when appending."""
    runs: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if isinstance(existing, dict) and isinstance(existing.get("runs"), list):
                runs = list(existing["runs"])
            elif isinstance(existing, dict):
                runs = [existing]
        except (OSError, json.JSONDecodeError):
            runs = []
    runs.append(metadata)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "latest": metadata,
        "runs": runs,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_fixture_files(repo: Path, files: dict[str, str]) -> None:
    """Write all starting fixture files into repo."""
    for filename, source in files.items():
        target = repo / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)


def init_git_repo(repo: Path) -> None:
    """Initialize a clean git commit for the fixture repository."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    (repo / ".gitignore").write_text(".aider*\n__pycache__/\nnode_modules/\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Trial",
        "-c",
        "user.email=trial@localhost",
        "commit",
        "-qm",
        "fixture",
    )


def _exclude_facts_artifacts(
    repo: Path,
    *,
    facts_db: str | None = None,
    facts_file: str | None = None,
) -> None:
    """Exclude custom relative facts store paths from git status inside repo."""
    exclude_file = repo / ".git" / "info" / "exclude"
    if not exclude_file.exists():
        return
    patterns: list[str] = []
    if facts_file:
        raw = facts_file.strip()
        if raw and not Path(raw).is_absolute() and not raw.startswith(".."):
            patterns.append(raw)
    if facts_db:
        raw = facts_db.strip()
        if raw.lower().startswith("sqlite://"):
            raw = raw[len("sqlite://") :].split("?", 1)[0].split("#", 1)[0]
        if (
            raw
            and not raw.startswith("/")
            and not Path(raw).is_absolute()
            and not raw.startswith("..")
        ):
            patterns.extend([raw, f"{raw}-wal", f"{raw}-shm"])
    if not patterns:
        return
    existing = exclude_file.read_text()
    existing_lines = set(existing.splitlines())
    additions = [pat for pat in dict.fromkeys(patterns) if pat not in existing_lines]
    if additions:
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        exclude_file.write_text(existing + suffix + "\n".join(additions) + "\n")


def seed_repo_facts(
    repo: Path | str,
    seed_facts: tuple[dict[str, object], ...] | list[dict[str, object]],
    *,
    facts_db: str | None = None,
    facts_file: str | None = None,
) -> list[str]:
    """Seed confirmed project facts into repo and apply any post-seed mutations."""
    if not seed_facts:
        return []
    repo_path = Path(repo)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from aider.project_facts import open_project_fact_store

    store = open_project_fact_store(
        repo_path,
        enabled=True,
        facts_db=facts_db,
        facts_file=facts_file,
    )
    if not store.active:
        diagnostic = store.diagnostic or "Project facts store could not be opened."
        store.close()
        raise RuntimeError(diagnostic)

    mutations: dict[str, str] = {}
    try:
        for entry in seed_facts:
            store.record_fact(
                str(entry["statement"]),
                source_paths=tuple(entry.get("source_paths") or ()),
                tags=tuple(entry.get("tags") or ()),
                note=str(entry.get("note") or ""),
            )
            post_mut = entry.get("mutate_after_seed")
            if isinstance(post_mut, dict):
                for rel_path, content in post_mut.items():
                    mutations[str(rel_path)] = str(content)
    finally:
        store.close()

    if mutations:
        write_fixture_files(repo_path, mutations)
    return list(mutations.keys())


def run_verification_details(repo: Path, case_spec: CaseSpec, timeout: int = 15) -> dict:
    """Run smoke and hidden commands independently against repo and return separate results."""
    checks = (
        ("smoke", shlex.split(case_spec.test_cmd)),
        ("hidden", case_spec.hidden_cmd),
    )
    outcomes: dict[str, str] = {}
    passed_map: dict[str, bool] = {}
    diagnostics: list[bytes] = []
    for label, cmd in checks:
        try:
            proc = subprocess.run(cmd, cwd=repo, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            outcomes[label] = "timeout"
            passed_map[label] = False
            diagnostics.append(f"{label} verification timed out".encode())
            continue
        if proc.returncode == 0:
            outcomes[label] = "pass"
            passed_map[label] = True
        else:
            outcomes[label] = "fail"
            passed_map[label] = False
            parts = [f"[{label} exit={proc.returncode}]".encode()]
            if proc.stdout:
                parts.append(proc.stdout)
            if proc.stderr:
                parts.append(proc.stderr)
            diagnostics.append(b"\n".join(parts))

    smoke_passed = passed_map["smoke"]
    hidden_passed = passed_map["hidden"]
    return {
        "smoke_passed": smoke_passed,
        "smoke_outcome": outcomes["smoke"],
        "hidden_passed": hidden_passed,
        "hidden_outcome": outcomes["hidden"],
        "verified": smoke_passed and hidden_passed,
        "diagnostic": b"\n".join(diagnostics),
    }


def run_verification(repo: Path, case_spec: CaseSpec, timeout: int = 15) -> tuple[bool, bytes]:
    """Run smoke and hidden commands independently against repo."""
    details = run_verification_details(repo, case_spec, timeout=timeout)
    return details["verified"], details["diagnostic"]


def run_spec(
    output: Path,
    case_spec: CaseSpec,
    arm: str,
    round_id,
    map_tokens: int,
    spec: str = "vague",
    run_tag: str | None = None,
    model: str = MODEL,
    backend: str | None = None,
    read_range: bool | None = None,
    facts: bool | None = None,
    facts_db: str | None = None,
    facts_file: str | None = None,
    local_callers: bool | None = None,
    metadata: dict | None = None,
) -> dict:
    """Run a single Aider trial for case_spec under the requested arm and controls."""
    if spec not in ("vague", "explicit"):
        raise ValueError(f"Unknown spec {spec!r}; expected 'vague' or 'explicit'")
    arm_flags = resolve_arm_flags(
        arm,
        backend=backend,
        read_range=read_range,
        facts=facts,
        facts_db=facts_db,
        facts_file=facts_file,
        local_callers=local_callers,
    )
    local_callers_enabled = "--local-callers" in arm_flags
    facts_backend = "sqlite" if facts and facts_db else ("jsonl" if facts else "off")
    task = case_spec.task
    if spec == "explicit" and case_spec.explicit:
        task = f"{task} {case_spec.explicit}"

    ctrl_suffix = control_tag(
        backend=backend,
        read_range=read_range,
        facts=facts,
        facts_db=facts_db,
        facts_file=facts_file,
        local_callers=local_callers,
    )
    if run_tag is not None:
        tag = run_tag
    elif ctrl_suffix:
        tag = f"{round_id}-{spec}-{ctrl_suffix}"
    else:
        tag = f"{round_id}-{spec}"
    folder = output / f"{tag}-{case_spec.name}-{arm}"
    folder.mkdir()
    repo = folder / "repo"
    repo.mkdir()
    write_fixture_files(repo, case_spec.files)
    init_git_repo(repo)
    _exclude_facts_artifacts(repo, facts_db=facts_db, facts_file=facts_file)
    if case_spec.seed_facts:
        mutated_paths = seed_repo_facts(
            repo,
            case_spec.seed_facts,
            facts_db=facts_db,
            facts_file=facts_file,
        )
        if mutated_paths:
            subprocess.run(
                ["git", "add", "--", *mutated_paths],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Trial",
                    "-c",
                    "user.email=trial@localhost",
                    "commit",
                    "-qm",
                    "post-seed fixture mutation",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
    base_rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    env = dict(os.environ, PYTHONPATH=str(ROOT), AIDER_ANALYTICS="false", AIDER_TRIAL_USAGE="1")
    targets = (case_spec.editable_file, *case_spec.additional_editable)
    # A real fixture can contain a dependency of Aider itself (e.g. requests).
    # Keep the fixture on the smoke/hidden check import path only.
    command = [
        sys.executable,
        "-c",
        "import runpy, sys; sys.path[:] = [entry for entry in sys.path if entry]; "
        "runpy.run_module('aider', run_name='__main__', alter_sys=True)",
        "--model",
        model,
        "--edit-format",
        "diff",
        "--oracle",
        "--oracle-mode",
        "adaptive",
        "--no-oracle-require-search",
        "--no-oracle-fail-open",
        "--no-tdd",
        "--no-check-update",
        "--no-show-model-warnings",
        "--no-auto-commits",
        "--no-suggest-shell-commands",
        "--no-stream",
        "--map-tokens",
        str(map_tokens),
        "--auto-test",
        "--test-cmd",
        case_spec.test_cmd,
        *arm_flags,
        "--message",
        f"{task} Edit only {', '.join(targets)}.",
        *targets,
    ]
    start = time.monotonic()
    with (folder / "aider.log").open("w") as log_file:
        try:
            code = subprocess.run(
                command,
                cwd=repo,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=360,
            ).returncode
        except subprocess.TimeoutExpired:
            code = 124

    log_text = (folder / "aider.log").read_text()
    evidence = bool(
        re.search(
            r"^Local caller evidence: (SELECTED|INCOMPLETE|COMPLETE_WITHIN_SCOPE)",
            log_text,
            re.M,
        )
    )
    oracle = "required foundations loaded" in log_text
    contract_rejected = "Local caller contract check:" in log_text
    verdicts = re.findall(r"^Local caller audit: (PASS|FAIL|UNKNOWN|ERROR)\b", log_text, re.M)
    usage_info = parse_trial_usage(log_text)
    if code != 124 and usage_info["editor_calls"] == 0:
        raise RuntimeError(f"No editor completion recorded. See {folder / 'aider.log'}")

    audit_outcome, audit_passed = classify_audit_outcome(verdicts)
    verification = run_verification_details(repo, case_spec)
    verified = verification["verified"]
    diagnostic = verification["diagnostic"]

    commits_added = int(
        subprocess.check_output(
            ["git", "rev-list", "--count", f"{base_rev}..HEAD"], cwd=repo, text=True
        ).strip()
    )
    gitdiff = subprocess.check_output(["git", "diff", base_rev], cwd=repo, text=True)
    (folder / "patch.diff").write_text(gitdiff)
    changed = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, text=True
    )
    (folder / "verification.txt").write_bytes(diagnostic)
    changed_lines = [line for line in changed.splitlines() if line]
    if commits_added > 0 and not changed_lines:
        committed_files = set(
            subprocess.check_output(
                ["git", "diff", "--name-only", f"{base_rev}..HEAD"], cwd=repo, text=True
            ).splitlines()
        )
        only_target_modified = committed_files == set(targets)
    else:
        only_target_modified = set(changed_lines) == {f" M {target}" for target in targets}
    commit_outcome, commit_passed = classify_commit_outcome(
        code, changed_lines, gitdiff, only_target_modified, commits_added
    )

    if metadata is not None:
        row_metadata = dict(metadata)
    else:
        repo_state = _default_repo_state()
        row_metadata = {
            "schema_version": SCHEMA_VERSION,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_commit": repo_state["git_commit"],
            "git_dirty": repo_state["git_dirty"],
            "python_version": platform.python_version(),
        }

    return dict(
        schema_version=SCHEMA_VERSION,
        model=model,
        round=round_id,
        spec=spec,
        kind=case_spec.kind,
        language=case_spec.language,
        case=case_spec.name,
        arm=arm,
        arm_flags=arm_flags,
        local_callers_backend=backend,
        local_callers_read_range=read_range,
        local_callers_enabled=local_callers_enabled,
        local_callers_facts=local_callers_enabled,
        project_facts=facts,
        facts_backend=facts_backend,
        facts_db=facts_db,
        facts_file=facts_file,
        map_tokens=map_tokens,
        exit=code,
        oracle=oracle,
        evidence=evidence,
        contract_rejected=contract_rejected,
        audit_ran=bool(verdicts),
        audit_verdicts=verdicts,
        audit_outcome=audit_outcome,
        audit_passed=audit_passed,
        audit_calls=usage_info["audit_calls"],
        audit_failed="FAIL" in verdicts,
        unknowns=verdicts.count("UNKNOWN"),
        errors=verdicts.count("ERROR"),
        false_approval=bool(audit_outcome == "pass" and not verified),
        commit_outcome=commit_outcome,
        commit_passed=commit_passed,
        only_target_modified=only_target_modified,
        commits_added=commits_added,
        smoke_outcome=verification["smoke_outcome"],
        smoke_passed=verification["smoke_passed"],
        hidden_outcome=verification["hidden_outcome"],
        hidden_passed=verification["hidden_passed"],
        editor_calls=usage_info["editor_calls"],
        extra_calls=usage_info["extra_calls"],
        tokens=usage_info["tokens"],
        tokens_measured=usage_info["measured"],
        editor_tokens=usage_info["editor_tokens"],
        audit_tokens=usage_info["audit_tokens"],
        prompt_tokens=usage_info["prompt_tokens"],
        completion_tokens=usage_info["completion_tokens"],
        calls_by_role=usage_info["calls_by_role"],
        tokens_by_role=usage_info["tokens_by_role"],
        passed=verified and only_target_modified,
        seconds=round(time.monotonic() - start, 1),
        patch=gitdiff,
        workdir=str(output),
        metadata=row_metadata,
    )


def run_case(
    output,
    case,
    enabled,
    run_id,
    map_tokens,
    specify_behavior,
    *,
    backend: str | None = None,
    read_range: bool | None = None,
    facts: bool | None = None,
    facts_db: str | None = None,
    facts_file: str | None = None,
    local_callers: bool | None = None,
    metadata: dict | None = None,
):
    """Backwards-compatible wrapper for legacy tuple cases."""
    name, api, consumer, task, smoke, hidden = case
    case_spec = py_case(
        name=name,
        files={"api.py": api, "consumer.py": consumer, "smoke.py": smoke},
        task=task,
        hidden=hidden,
        explicit=EXPECTED_BEHAVIOR.get(name, ""),
    )
    arm = "context" if enabled else "baseline"
    spec = "explicit" if specify_behavior else "vague"
    ctrl_suffix = control_tag(
        backend=backend,
        read_range=read_range,
        facts=facts,
        facts_db=facts_db,
        facts_file=facts_file,
        local_callers=local_callers,
    )
    run_tag = f"{run_id}-{ctrl_suffix}" if ctrl_suffix else str(run_id)
    result = run_spec(
        output=output,
        case_spec=case_spec,
        arm=arm,
        round_id=run_id,
        map_tokens=map_tokens,
        spec=spec,
        run_tag=run_tag,
        backend=backend,
        read_range=read_range,
        facts=facts,
        facts_db=facts_db,
        facts_file=facts_file,
        local_callers=local_callers,
        metadata=metadata,
    )
    result["explicit_behavior"] = specify_behavior
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--map-tokens", type=int, default=0)
    parser.add_argument("--specify-behavior", action="store_true")
    parser.add_argument(
        "--local-callers-backend",
        default=None,
        help="optional backend passed via --local-callers-backend",
    )
    parser.add_argument(
        "--local-callers-read-range",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="optional override for --local-callers-read-range / --no-local-callers-read-range",
    )
    parser.add_argument(
        "--facts",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="facts",
        help="optional override for Issue 5 project facts (--facts / --no-facts)",
    )
    parser.add_argument(
        "--facts-db",
        default=None,
        help="optional SQLite path or sqlite:// URL passed via --facts-db",
    )
    parser.add_argument(
        "--facts-file",
        default=None,
        help="optional JSONL file path passed via --facts-file",
    )
    parser.add_argument(
        "--local-callers-override",
        "--local-callers-facts",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="local_callers_override",
        help="optional override for --local-callers / --no-local-callers",
    )
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix="aider-local-trial-"))
    print(output, flush=True)
    run_meta = collect_run_metadata(
        model=MODEL,
        suite="legacy",
        map_tokens=args.map_tokens,
        rounds=args.rounds,
        arms=["baseline", "context"],
        specs=["explicit" if args.specify_behavior else "vague"],
        cases=[case[0] for case in CASES],
        controls={
            "backend": args.local_callers_backend,
            "read_range": args.local_callers_read_range,
            "facts": args.facts,
            "facts_db": args.facts_db,
            "facts_file": args.facts_file,
            "local_callers": args.local_callers_override,
        },
    )
    write_run_metadata(output / "metadata.json", run_meta)
    row_meta = {
        "schema_version": run_meta["schema_version"],
        "run_id": run_meta["run_id"],
        "timestamp_utc": run_meta["timestamp_utc"],
        "git_commit": run_meta["git_commit"],
        "git_dirty": run_meta["git_dirty"],
        "python_version": run_meta["python_version"],
        "suite": run_meta["suite"],
    }
    for iteration in range(args.rounds):
        for index, case in enumerate(CASES):
            first = bool((iteration + index) % 2)
            for enabled in (first, not first):
                result = run_case(
                    output,
                    case,
                    enabled,
                    iteration,
                    args.map_tokens,
                    args.specify_behavior,
                    backend=args.local_callers_backend,
                    read_range=args.local_callers_read_range,
                    facts=args.facts,
                    facts_db=args.facts_db,
                    facts_file=args.facts_file,
                    local_callers=args.local_callers_override,
                    metadata=row_meta,
                )
                print(json.dumps({k: v for k, v in result.items() if k != "patch"}), flush=True)
                with (output / "results.jsonl").open("a") as stream:
                    stream.write(json.dumps(result) + "\n")
                expected_local_callers = (
                    args.local_callers_override
                    if args.local_callers_override is not None
                    else enabled
                )
                if (
                    not result["oracle"]
                    or result["exit"]
                    or result["evidence"] != expected_local_callers
                ):
                    raise SystemExit("Invalid trial; inspect aider.log")


if __name__ == "__main__":
    main()
