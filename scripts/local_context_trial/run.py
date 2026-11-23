"""Run paired Aider trials in fresh repositories. Hidden assertions run after Aider exits."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
MODEL = "lm_studio/qwen3-coder-30b-a3b-instruct-mlx"
# Each task exposes the same starting files in both arms. The expected convention
# is visible in an ordinary downstream caller. Hidden tests stay outside Git.
CASES = [
    (
        "cursor",
        "def decode_cursor(value):\n    return int(value)\n",
        "from api import decode_cursor\n\ndef page(value):\n    offset = decode_cursor(value)\n    if offset == -7:\n        return 'restart'\n    return offset + 1\n",
        "Update decode_cursor to handle a blank string according to the existing pagination convention. Preserve valid integer inputs.",
        "from api import decode_cursor\nassert decode_cursor('8') == 8",
        "from consumer import page\nassert page('') == 'restart'\nassert page('8') == 9",
    ),
    (
        "labels",
        "def parse_labels(value):\n    return value.split(',')\n",
        "from api import parse_labels\n\ndef render(value):\n    labels = parse_labels(value)\n    if labels == ['(unassigned)']:\n        return 'empty'\n    return '|'.join(labels)\n",
        "Update parse_labels to trim whitespace, remove blank labels, and honor the existing renderer convention when all labels are blank.",
        "from api import parse_labels\nassert parse_labels('a,b') == ['a','b']",
        "from consumer import render\nassert render(' , ') == 'empty'\nassert render(' a, ,b ') == 'a|b'",
    ),
    (
        "retry",
        "def retry_delay(raw):\n    return int(raw)\n",
        "from api import retry_delay\n\ndef schedule(raw):\n    delay = retry_delay(raw)\n    if delay == 'defer':\n        return 'manual'\n    return delay + 1\n",
        "Update retry_delay to handle malformed numeric strings using the scheduler's existing fallback. Keep the valid numeric path.",
        "from api import retry_delay\nassert retry_delay('0') == 0",
        "from consumer import schedule\nassert schedule('bad') == 'manual'\nassert schedule('3') == 4",
    ),
]


def run_case(output, case, enabled, run_id):
    name, api, consumer, task, smoke, hidden = case
    arm = "context" if enabled else "baseline"
    folder = output / f"{run_id}-{name}-{arm}"
    folder.mkdir()
    repo = folder / "repo"
    repo.mkdir()
    files = {"api.py": api, "consumer.py": consumer, "smoke.py": smoke}
    for filename, source in files.items():
        (repo / filename).write_text(source)
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Trial", "-c", "user.email=trial@localhost", "commit", "-qm", "fixture")
    env = dict(os.environ, PYTHONPATH=str(ROOT), AIDER_ANALYTICS="false")
    command = [
        sys.executable, "-m", "aider", "--model", MODEL, "--edit-format", "diff",
        "--oracle", "--no-oracle-require-search", "--no-oracle-fail-open", "--no-tdd",
        "--no-check-update", "--no-show-model-warnings", "--no-auto-commits",
        "--no-suggest-shell-commands", "--no-stream", "--map-tokens", "0",
        "--auto-test", "--test-cmd", f"{sys.executable} smoke.py",
        "--local-callers" if enabled else "--no-local-callers",
        "--message", task + " Edit only api.py.", "api.py",
    ]
    start = time.monotonic()
    with (folder / "aider.log").open("w") as log:
        try:
            code = subprocess.run(command, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=360).returncode
        except subprocess.TimeoutExpired:
            code = 124
    log = (folder / "aider.log").read_text()
    evidence = "Local caller evidence:" in log
    oracle = "required foundations loaded" in log
    hidden_run = subprocess.run([sys.executable, "-c", hidden], cwd=repo, capture_output=True)
    gitdiff = subprocess.check_output(["git", "diff", "HEAD"], cwd=repo, text=True)
    (folder / "patch.diff").write_text(gitdiff)
    changed = subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=repo, text=True)
    (folder / "verification.txt").write_bytes(hidden_run.stderr)
    return dict(case=name, arm=arm, exit=code, oracle=oracle, evidence=evidence,
                passed=hidden_run.returncode == 0 and changed.splitlines() == ["api.py"],
                seconds=round(time.monotonic()-start, 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix="aider-local-trial-"))
    print(output, flush=True)
    for iteration in range(args.rounds):
        for index, case in enumerate(CASES):
            first = bool((iteration + index) % 2)
            for enabled in (first, not first):
                result = run_case(output, case, enabled, iteration)
                print(json.dumps(result), flush=True)
                with (output / "results.jsonl").open("a") as stream:
                    stream.write(json.dumps(result) + "\n")
                if not result["oracle"] or result["exit"] or result["evidence"] != enabled:
                    raise SystemExit("Invalid trial; inspect aider.log")


if __name__ == "__main__":
    main()
