"""Run paired Aider trials in fresh repositories. Hidden assertions run after Aider exits."""

import argparse
from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
MODEL = "lm_studio/qwen/qwen3.6-35b-a3b"

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


def py_case(
    name: str,
    files: dict[str, str],
    task: str,
    hidden: str,
    explicit: str,
    kind: str = "caller",
    editable_file: str = "api.py",
    smoke_file: str = "smoke.py",
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
    )


def resolve_arm_flags(arm: str) -> list[str]:
    """Return CLI flags for a trial arm ('context' aliases 'passive')."""
    if arm == "context":
        return list(ARM_FLAGS["passive"])
    if arm not in ARM_FLAGS:
        raise ValueError(f"Unknown arm {arm!r}; expected one of {', '.join(VALID_ARMS)}")
    return list(ARM_FLAGS[arm])


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


def run_verification(repo: Path, case_spec: CaseSpec, timeout: int = 15) -> tuple[bool, bytes]:
    """Run smoke and hidden commands independently against repo."""
    checks = (
        ("smoke", shlex.split(case_spec.test_cmd)),
        ("hidden", case_spec.hidden_cmd),
    )
    for label, cmd in checks:
        try:
            proc = subprocess.run(cmd, cwd=repo, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"{label} verification timed out".encode()
        if proc.returncode != 0:
            parts = [f"[{label} exit={proc.returncode}]".encode()]
            if proc.stdout:
                parts.append(proc.stdout)
            if proc.stderr:
                parts.append(proc.stderr)
            return False, b"\n".join(parts)
    return True, b""


def run_spec(
    output: Path,
    case_spec: CaseSpec,
    arm: str,
    round_id,
    map_tokens: int,
    spec: str = "vague",
    run_tag: str | None = None,
    model: str = MODEL,
) -> dict:
    """Run a single Aider trial for case_spec under the requested arm."""
    if spec not in ("vague", "explicit"):
        raise ValueError(f"Unknown spec {spec!r}; expected 'vague' or 'explicit'")
    arm_flags = resolve_arm_flags(arm)
    task = case_spec.task
    if spec == "explicit" and case_spec.explicit:
        task = f"{task} {case_spec.explicit}"

    tag = run_tag if run_tag is not None else f"{round_id}-{spec}"
    folder = output / f"{tag}-{case_spec.name}-{arm}"
    folder.mkdir()
    repo = folder / "repo"
    repo.mkdir()
    write_fixture_files(repo, case_spec.files)
    init_git_repo(repo)

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
    evidence = bool(re.search(r"^Local caller evidence: (SELECTED|INCOMPLETE)", log_text, re.M))
    oracle = "required foundations loaded" in log_text
    contract_rejected = "Local caller contract check:" in log_text
    verdicts = re.findall(r"^Local caller audit: (PASS|FAIL|UNKNOWN|ERROR)\b", log_text, re.M)
    usage = re.findall(r"^Trial usage: (editor|audit) (\d+|None)$", log_text, re.M)
    if not any(kind == "editor" for kind, _ in usage):
        raise RuntimeError(f"No editor completion recorded. See {folder / 'aider.log'}")
    tokens = (
        sum(int(value) for _, value in usage)
        if usage and all(value.isdecimal() for _, value in usage)
        else None
    )

    verified, diagnostic = run_verification(repo, case_spec)
    gitdiff = subprocess.check_output(["git", "diff", "HEAD"], cwd=repo, text=True)
    (folder / "patch.diff").write_text(gitdiff)
    changed = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, text=True
    )
    (folder / "verification.txt").write_bytes(diagnostic)
    only_target_modified = set(changed.splitlines()) == {f" M {target}" for target in targets}

    return dict(
        model=model,
        round=round_id,
        spec=spec,
        kind=case_spec.kind,
        language=case_spec.language,
        case=case_spec.name,
        arm=arm,
        map_tokens=map_tokens,
        exit=code,
        oracle=oracle,
        evidence=evidence,
        contract_rejected=contract_rejected,
        audit_ran=bool(verdicts),
        audit_verdicts=verdicts,
        audit_calls=sum(kind == "audit" for kind, _ in usage),
        audit_failed="FAIL" in verdicts,
        unknowns=verdicts.count("UNKNOWN"),
        errors=verdicts.count("ERROR"),
        false_approval=bool(verdicts and verdicts[-1] == "PASS" and not verified),
        extra_calls=max(0, len(usage) - 1),
        tokens=tokens,
        passed=verified and only_target_modified,
        seconds=round(time.monotonic() - start, 1),
        patch=gitdiff,
        workdir=str(output),
    )


def run_case(output, case, enabled, run_id, map_tokens, specify_behavior):
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
    result = run_spec(
        output=output,
        case_spec=case_spec,
        arm=arm,
        round_id=run_id,
        map_tokens=map_tokens,
        spec=spec,
        run_tag=str(run_id),
    )
    result["explicit_behavior"] = specify_behavior
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--map-tokens", type=int, default=0)
    parser.add_argument("--specify-behavior", action="store_true")
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix="aider-local-trial-"))
    print(output, flush=True)
    for iteration in range(args.rounds):
        for index, case in enumerate(CASES):
            first = bool((iteration + index) % 2)
            for enabled in (first, not first):
                result = run_case(
                    output, case, enabled, iteration, args.map_tokens, args.specify_behavior
                )
                print(json.dumps({k: v for k, v in result.items() if k != "patch"}), flush=True)
                with (output / "results.jsonl").open("a") as stream:
                    stream.write(json.dumps(result) + "\n")
                if not result["oracle"] or result["exit"] or result["evidence"] != enabled:
                    raise SystemExit("Invalid trial; inspect aider.log")


if __name__ == "__main__":
    main()
