"""Offline checks for the pinned Requests JSON trial fixture."""

from importlib.metadata import distribution
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

TRIAL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "local_context_trial"
if str(TRIAL_DIR) not in sys.path:
    sys.path.insert(0, str(TRIAL_DIR))

import requests_cases  # noqa: E402
import run  # noqa: E402


@pytest.mark.parametrize(
    ("fixed_files", "passes"),
    [
        ((), False),
        (("requests/models.py",), False),
        (("requests/exceptions.py",), False),
        (("requests/models.py", "requests/exceptions.py"), True),
    ],
)
def test_requests_json_checks_require_both_public_exception_fixes(tmp_path, fixed_files, passes):
    package = distribution("requests")
    if package.version != "2.34.2":
        pytest.skip("Requests trial requires requests==2.34.2")
    case = next(case for case in requests_cases.cases() if case.name == "requests_json")
    files = case.files.copy()
    for path in fixed_files:
        files[path] = package.locate_file(path).read_text()
    run.write_fixture_files(tmp_path, files)

    for command in (shlex.split(case.test_cmd), case.hidden_cmd):
        proc = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=15)
        assert (proc.returncode == 0) == passes, (command, proc.stdout, proc.stderr)
