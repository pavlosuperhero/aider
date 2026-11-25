"""Held-out multi-language, multi-arm factorial trial for local caller context.

Pre-registered hypotheses:
  Original (2026-11-23):
    H1  On caller-dependent tasks with a vague spec, passive --local-callers raises
        the hidden-check pass rate over baseline.
    H2  On caller-dependent tasks with an explicit spec, --local-callers raises it.
    H3  On control tasks (caller irrelevant), --local-callers does not lower it.
  Expanded multi-arm & multi-language suite:
    H4  On same-type traps (py_bounds, names, js_names) and noisy-caller tasks
        (py_noisy), active enforcement (contract, audit, contract+audit) improves
        hidden-check pass rate over passive context alone without degrading control
        tasks.
    H5  Local caller context gains generalize beyond Python to JavaScript ESM tasks.

Analysis:
  Pairs share (case, spec, round); exact two-sided sign test on discordant pairs
  comparing each treatment arm against baseline and against passive. The repo map
  budget is fixed for every run so only caller-context mode/audit and spec wording
  vary. None of these tasks was used to tune the feature.
"""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
import shlex
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).parent))
import run  # noqa: E402

ARM_ORDER = run.VALID_ARMS
LANGUAGE_ORDER = ("python", "js")
KIND_ORDER = ("caller", "noisy", "facts", "read_range", "control")
SPEC_ORDER = ("vague", "explicit")
FIXTURE_SETUP_ERRORS = (
    "SyntaxError",
    "ModuleNotFoundError",
    "ERR_MODULE_NOT_FOUND",
)

CASE_SPECS: list[run.CaseSpec] = [
    # --- Original 7 Python held-out tasks (preserved intact) ---
    run.py_case(
        name="duration",
        files={
            "api.py": "def parse_duration(text):\n    return int(text)\n",
            "consumer.py": (
                "from api import parse_duration\n\n"
                "def timeout_ms(raw):\n"
                "    seconds = parse_duration(raw)\n"
                "    return seconds * 1000\n"
            ),
            "smoke.py": "from api import parse_duration\nassert parse_duration('5') == 5\n",
        },
        task=(
            "Update parse_duration to also accept values with an 'm' suffix, returning the"
            " unit its callers expect. Keep plain numbers working."
        ),
        hidden=(
            "from consumer import timeout_ms\n"
            "assert timeout_ms('2m') == 120000\n"
            "assert timeout_ms('5') == 5000"
        ),
        explicit="timeout_ms('2m') must return 120000.",
        kind="caller",
    ),
    run.py_case(
        name="names",
        files={
            "api.py": "def split_name(full):\n    return full.split(' ', 1)\n",
            "consumer.py": (
                "from api import split_name\n\n"
                "def catalog(full):\n"
                "    last, first = split_name(full)\n"
                "    return last.upper() + ', ' + first\n"
            ),
            "smoke.py": "from api import split_name\nassert len(split_name('A B')) == 2\n",
        },
        task=(
            "Update split_name to return a tuple in the order its callers use and to give an"
            " empty string for the missing part of a single-word name."
        ),
        hidden=(
            "from consumer import catalog\n"
            "assert catalog('Ada Lovelace') == 'LOVELACE, Ada'\n"
            "assert catalog('Plato') == 'PLATO, '"
        ),
        explicit=(
            "catalog('Ada Lovelace') must return 'LOVELACE, Ada' and catalog('Plato') 'PLATO, '."
        ),
        kind="caller",
    ),
    run.py_case(
        name="lookup",
        files={
            "api.py": "def find_user(users, name):\n    return users.index(name)\n",
            "consumer.py": (
                "from api import find_user\n\n"
                "def greet(users, name):\n"
                "    idx = find_user(users, name)\n"
                "    if idx is None:\n"
                "        return 'guest'\n"
                "    return users[idx]\n"
            ),
            "smoke.py": "from api import find_user\nassert find_user(['a', 'b'], 'b') == 1\n",
        },
        task=(
            "Update find_user so a missing name follows the existing not-found convention"
            " instead of raising."
        ),
        hidden=(
            "from consumer import greet\n"
            "assert greet(['a', 'b'], 'z') == 'guest'\n"
            "assert greet(['a', 'b'], 'a') == 'a'"
        ),
        explicit="greet(['a', 'b'], 'z') must return 'guest'.",
        kind="caller",
    ),
    run.py_case(
        name="config",
        files={
            "api.py": (
                "def load_config(text):\n"
                "    return dict(line.split('=') for line in text.splitlines())\n"
            ),
            "consumer.py": (
                "from api import load_config\n\n"
                "def next_port(text):\n"
                "    cfg = load_config(text)\n"
                "    return cfg['port'] + 1\n"
            ),
            "smoke.py": "from api import load_config\nassert load_config('a=b')['a'] == 'b'\n",
        },
        task=(
            "Update load_config to strip whitespace around keys and values and to convert"
            " values to the types its callers need."
        ),
        hidden="from consumer import next_port\nassert next_port(' port = 80 ') == 81",
        explicit="next_port(' port = 80 ') must return 81.",
        kind="caller",
    ),
    run.py_case(
        name="token",
        files={
            "api.py": "def encode_token(user_id):\n    return str(user_id)\n",
            "consumer.py": (
                "from api import encode_token\n\n"
                "def header(uid):\n"
                "    return b'Bearer ' + encode_token(uid)\n"
            ),
            "smoke.py": "from api import encode_token\nassert encode_token(7)\n",
        },
        task=(
            "Update encode_token to prefix the id with 'u-' and return the type its callers need."
        ),
        hidden="from consumer import header\nassert header(7) == b'Bearer u-7'",
        explicit="header(7) must return b'Bearer u-7'.",
        kind="caller",
    ),
    run.py_case(
        name="email",
        files={
            "api.py": "def normalize_email(value):\n    return value.lower()\n",
            "consumer.py": (
                "from api import normalize_email\n\n"
                "def key(value):\n"
                "    return normalize_email(value)\n"
            ),
            "smoke.py": (
                "from api import normalize_email\nassert normalize_email('A@B') == 'a@b'\n"
            ),
        },
        task="Update normalize_email to also strip surrounding whitespace.",
        hidden="from consumer import key\nassert key('  A@B.c ') == 'a@b.c'",
        explicit="key('  A@B.c ') must return 'a@b.c'.",
        kind="control",
    ),
    run.py_case(
        name="slug",
        files={
            "api.py": "def slugify(title):\n    return title.lower()\n",
            "consumer.py": (
                "from api import slugify\n\n"
                "def url(title):\n"
                "    return '/posts/' + slugify(title)\n"
            ),
            "smoke.py": "from api import slugify\nassert slugify('abc') == 'abc'\n",
        },
        task=(
            "Update slugify to replace each run of non-alphanumeric characters with one hyphen"
            " and strip leading and trailing hyphens."
        ),
        hidden="from consumer import url\nassert url('Hello, World!') == '/posts/hello-world'",
        explicit="url('Hello, World!') must return '/posts/hello-world'.",
        kind="control",
    ),
    # --- New Python tasks: same-type trap & noisy-callers distraction ---
    run.py_case(
        name="py_bounds",
        files={
            "api.py": "def range_bounds(nums):\n    return (min(nums), max(nums))\n",
            "consumer.py": (
                "from api import range_bounds\n\n"
                "def span_label(nums):\n"
                "    high, low = range_bounds(nums)\n"
                '    return f"{high}->{low} (span {high - low})"\n'
            ),
            "smoke.py": "from api import range_bounds\nassert len(range_bounds([1, 5])) == 2\n",
        },
        task=(
            "Update range_bounds to ignore None entries and return the pair in the order its"
            " callers expect."
        ),
        hidden=(
            "from consumer import span_label\n"
            "assert span_label([3, None, 9, 1]) == '9->1 (span 8)'"
        ),
        explicit="span_label([3, None, 9, 1]) must return '9->1 (span 8)'.",
        kind="caller",
    ),
    run.py_case(
        name="py_noisy",
        files={
            "api.py": "def parse_tag(raw):\n    return raw.split('@', 1)\n",
            "archive.py": (
                "from legacy import parse_tag\n"
                "# returns list\n"
                "val = parse_tag('a:b')[0]\n"
            ),
            "report.py": (
                "from api import parse_tag\n\n"
                "def render_tag(raw):\n"
                "    group, code = parse_tag(raw)\n"
                '    return f"[{group}] #{code}"\n'
            ),
            "notes.py": '# parse_tag used to return a dict {"code": ..., "group": ...}\n',
            "smoke.py": "from api import parse_tag\nassert len(parse_tag('42@ops')) == 2\n",
        },
        task=(
            "Update parse_tag to strip whitespace from both parts and return the tuple in the"
            " order its callers expect, using 'misc' when '@' is absent."
        ),
        hidden=(
            "from report import render_tag\n"
            "assert render_tag(' 42 @ ops ') == '[ops] #42'\n"
            "assert render_tag('99') == '[misc] #99'"
        ),
        explicit=(
            "render_tag(' 42 @ ops ') must return '[ops] #42' and"
            " render_tag('99') must return '[misc] #99'."
        ),
        kind="noisy",
    ),
    # --- New JavaScript / Node.js ESM tasks ---
    run.js_case(
        name="js_names",
        files={
            "api.mjs": "export function splitName(full) {\n  return full.split(' ');\n}\n",
            "consumer.mjs": (
                "import { splitName } from './api.mjs';\n"
                "export function catalog(full) {\n"
                "  const [last, first] = splitName(full);\n"
                "  return last.toUpperCase() + ', ' + first;\n"
                "}\n"
            ),
            "smoke.mjs": (
                "import assert from 'node:assert/strict';\n"
                "import { splitName } from './api.mjs';\n"
                "assert.equal(splitName('A B').length, 2);\n"
            ),
        },
        task=(
            "Update splitName to return a 2-element array in the order its callers expect and"
            " use an empty string for the missing part of a single-word name."
        ),
        hidden=(
            "import assert from 'node:assert/strict';\n"
            "import { catalog } from './consumer.mjs';\n"
            "assert.equal(catalog('Ada Lovelace'), 'LOVELACE, Ada');\n"
            "assert.equal(catalog('Plato'), 'PLATO, ');\n"
        ),
        explicit=(
            "catalog('Ada Lovelace') must return 'LOVELACE, Ada' and"
            " catalog('Plato') must return 'PLATO, '."
        ),
        kind="caller",
    ),
    run.js_case(
        name="js_price",
        files={
            "api.mjs": "export function parsePrice(text) {\n  return Number(text);\n}\n",
            "consumer.mjs": (
                "import { parsePrice } from './api.mjs';\n"
                "export function formatUsd(text) {\n"
                "  const cents = parsePrice(text);\n"
                "  return '$' + (cents / 100).toFixed(2);\n"
                "}\n"
            ),
            "smoke.mjs": (
                "import assert from 'node:assert/strict';\n"
                "import { parsePrice } from './api.mjs';\n"
                "assert.equal(parsePrice('500'), 500);\n"
            ),
        },
        task=(
            "Update parsePrice to accept strings with a leading '$' and decimal point"
            " (like '$12.50'), returning the unit its callers expect. Keep plain integer"
            " inputs working."
        ),
        hidden=(
            "import assert from 'node:assert/strict';\n"
            "import { formatUsd } from './consumer.mjs';\n"
            "assert.equal(formatUsd('$12.50'), '$12.50');\n"
            "assert.equal(formatUsd('500'), '$5.00');\n"
        ),
        explicit=(
            "formatUsd('$12.50') must return '$12.50' and formatUsd('500') must return '$5.00'."
        ),
        kind="caller",
    ),
    run.js_case(
        name="js_lookup",
        files={
            "api.mjs": (
                "export function findIndexByName(items, target) {\n"
                "  const idx = items.indexOf(target);\n"
                "  if (idx === -1) throw new Error('missing');\n"
                "  return idx;\n"
                "}\n"
            ),
            "consumer.mjs": (
                "import { findIndexByName } from './api.mjs';\n"
                "export function labelUser(items, target) {\n"
                "  const idx = findIndexByName(items, target);\n"
                "  if (idx === null) return 'guest';\n"
                "  return items[idx];\n"
                "}\n"
            ),
            "smoke.mjs": (
                "import assert from 'node:assert/strict';\n"
                "import { findIndexByName } from './api.mjs';\n"
                "assert.equal(findIndexByName(['a', 'b'], 'b'), 1);\n"
            ),
        },
        task=(
            "Update findIndexByName so a missing target follows the existing not-found"
            " convention instead of throwing."
        ),
        hidden=(
            "import assert from 'node:assert/strict';\n"
            "import { labelUser } from './consumer.mjs';\n"
            "assert.equal(labelUser(['a', 'b'], 'z'), 'guest');\n"
            "assert.equal(labelUser(['a', 'b'], 'a'), 'a');\n"
        ),
        explicit=(
            "labelUser(['a', 'b'], 'z') must return 'guest' and"
            " labelUser(['a', 'b'], 'a') must return 'a'."
        ),
        kind="caller",
    ),
    run.js_case(
        name="js_slug",
        files={
            "api.mjs": "export function slugify(title) {\n  return title.toLowerCase();\n}\n",
            "consumer.mjs": (
                "import { slugify } from './api.mjs';\n"
                "export function postUrl(title) {\n"
                "  return '/posts/' + slugify(title);\n"
                "}\n"
            ),
            "smoke.mjs": (
                "import assert from 'node:assert/strict';\n"
                "import { slugify } from './api.mjs';\n"
                "assert.equal(slugify('abc'), 'abc');\n"
            ),
        },
        task=(
            "Update slugify to replace runs of non-alphanumeric characters with a single hyphen"
            " and trim leading/trailing hyphens."
        ),
        hidden=(
            "import assert from 'node:assert/strict';\n"
            "import { postUrl } from './consumer.mjs';\n"
            "assert.equal(postUrl('Hello, World!'), '/posts/hello-world');\n"
        ),
        explicit="postUrl('Hello, World!') must return '/posts/hello-world'.",
        kind="control",
    ),
]

CASE_BY_NAME = {spec.name: spec for spec in CASE_SPECS}


def normalize_arm(arm: str) -> str:
    """Normalize legacy 'context' arm label to 'passive'."""
    return "passive" if arm == "context" else arm


def ordered_values(present: set[str], preferred: tuple[str, ...]) -> list[str]:
    extras = sorted(v for v in present if v not in preferred)
    return [v for v in preferred if v in present] + extras


_Z_95 = 1.959963984540054
_T_CRIT_95 = {
    1: 12.706204736432095,
    2: 4.302652729911275,
    3: 3.182446305284263,
    4: 2.7764451051977987,
    5: 2.5705818366147395,
    6: 2.4469118487916806,
    7: 2.3646242510102993,
    8: 2.3060041350333704,
    9: 2.2621571627409915,
    10: 2.2281388519649385,
    11: 2.200985160082949,
    12: 2.1788128296634177,
    13: 2.1603686564610127,
    14: 2.1447866879169273,
    15: 2.131449545559323,
    16: 2.1199052992210112,
    17: 2.1098155778331806,
    18: 2.10092204024096,
    19: 2.093024054408263,
    20: 2.0859634472658364,
    21: 2.079613844727662,
    22: 2.0738730679040147,
    23: 2.0686576104190406,
    24: 2.0638985616280205,
    25: 2.059538552753294,
    26: 2.055529438642871,
    27: 2.0518305164802833,
    28: 2.048407141795244,
    29: 2.045229642132703,
    30: 2.0422724563012373,
}


def sign_test(wins: int, losses: int) -> float:
    n = wins + losses
    if not n:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def _t_critical_95(df: int) -> float:
    """Two-sided 95% Student-t critical value for degrees of freedom >= 1."""
    if df < 1:
        raise ValueError(f"df must be >= 1, got {df}")
    if df in _T_CRIT_95:
        return _T_CRIT_95[df]
    z = _Z_95
    return (
        z
        + (z**3 + z) / (4.0 * df)
        + (5.0 * z**5 + 16.0 * z**3 + 3.0 * z) / (96.0 * df**2)
        + (3.0 * z**7 + 19.0 * z**5 + 17.0 * z**3 - 15.0 * z) / (384.0 * df**3)
    )


def paired_proportion_diff_ci(
    wins: int,
    losses: int,
    n_pairs: int,
) -> tuple[float | None, float | None, float | None]:
    """Return (delta, ci_low, ci_high) using Bonett-Price (2012) 95% paired CI."""
    if n_pairs < 0 or wins < 0 or losses < 0 or wins + losses > n_pairs:
        raise ValueError(
            f"Invalid paired counts: wins={wins}, losses={losses}, n_pairs={n_pairs}"
        )
    if n_pairs == 0:
        return None, None, None
    delta = (wins - losses) / n_pairs
    p10 = (wins + 1.0) / (n_pairs + 2.0)
    p01 = (losses + 1.0) / (n_pairs + 2.0)
    center = p10 - p01
    var = max(0.0, (p10 + p01 - center * center) / (n_pairs + 2.0))
    half = _Z_95 * math.sqrt(var)
    return delta, max(-1.0, center - half), min(1.0, center + half)


def paired_mean_diff_ci(
    diffs: list[float],
) -> tuple[float | None, float | None, float | None]:
    """Return (mean, ci_low, ci_high) using a 95% paired Student-t interval."""
    n = len(diffs)
    if n == 0:
        return None, None, None
    mean = sum(diffs) / n
    if n == 1:
        return mean, None, None
    ss = sum((d - mean) ** 2 for d in diffs)
    se = math.sqrt(ss / (n - 1)) / math.sqrt(n)
    half = _t_critical_95(n - 1) * se
    return mean, mean - half, mean + half


def _percentile(values: list[float], p: float) -> float | None:
    """Return the p-th percentile (0 <= p <= 100) using linear interpolation."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (p / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] + frac * (ordered[hi] - ordered[lo])


def load_results(path: Path) -> list[dict]:
    """Load trial rows from a JSONL file, ignoring blank lines and metadata records."""
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if isinstance(obj, dict) and obj.get("record_type") == "metadata":
            continue
        rows.append(obj)
    return rows


def check_fixtures(cases: list[run.CaseSpec]) -> int:
    """Verify every fixture passes smoke and fails hidden without invoking any LLM."""
    failures = 0
    with tempfile.TemporaryDirectory(prefix="aider-fixture-check-") as tmp:
        root = Path(tmp)
        for case in cases:
            repo = root / case.name
            repo.mkdir()
            run.write_fixture_files(repo, case.files)
            if case.seed_facts:
                try:
                    run.seed_repo_facts(repo, case.seed_facts)
                except Exception as exc:
                    failures += 1
                    print(
                        f"[FAIL] {case.name:10} lang={case.language:6} kind={case.kind:7}"
                        f" seed_error={exc}"
                    )
                    continue
            smoke_proc = subprocess.run(
                shlex.split(case.test_cmd),
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=15,
            )
            hidden_proc = subprocess.run(
                case.hidden_cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=15,
            )
            smoke_ok = smoke_proc.returncode == 0
            setup_err = any(marker in hidden_proc.stderr for marker in FIXTURE_SETUP_ERRORS)
            hidden_fails = hidden_proc.returncode != 0 and not setup_err
            ok = smoke_ok and hidden_fails
            status = "OK" if ok else "FAIL"
            print(
                f"[{status:4}] {case.name:10} lang={case.language:6} kind={case.kind:7}"
                f" smoke_rc={smoke_proc.returncode} hidden_rc={hidden_proc.returncode}"
            )
            if not ok:
                failures += 1
                if not smoke_ok:
                    smoke_msg = smoke_proc.stderr.strip() or smoke_proc.stdout.strip()
                    print(f"  smoke failed: {smoke_msg}")
                if hidden_proc.returncode == 0:
                    print("  hidden unexpectedly PASSED on starting code")
                elif setup_err:
                    print(f"  hidden hit setup/syntax error: {hidden_proc.stderr.strip()}")
    print(f"\nFixture self-check: {len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


def _paired_comparison(
    pairs: dict[tuple[str, str, str, str, object], dict[str, bool]],
    group_filter,
    treatment: str,
    reference: str,
) -> tuple[int, int, int]:
    win = loss = total = 0
    for key, outcomes in pairs.items():
        if not group_filter(key):
            continue
        if treatment in outcomes and reference in outcomes:
            t_val = outcomes[treatment]
            r_val = outcomes[reference]
            if t_val is None or r_val is None:
                continue
            total += 1
            win += int(bool(t_val) and not bool(r_val))
            loss += int(bool(r_val) and not bool(t_val))
    return win, loss, total


def _paired_binary_stats(
    pairs: dict[tuple[str, str, str, str, object], dict[str, bool | None]],
    group_filter,
    treatment: str,
    reference: str,
) -> dict | None:
    win, loss, n_pairs = _paired_comparison(pairs, group_filter, treatment, reference)
    if not n_pairs:
        return None
    delta, ci_low, ci_high = paired_proportion_diff_ci(win, loss, n_pairs)
    return {
        "n_pairs": n_pairs,
        "wins": win,
        "losses": loss,
        "delta": delta,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_value": sign_test(win, loss),
    }


def _paired_numeric_stats(
    pairs: dict[tuple[str, str, str, str, object], dict[str, float | None]],
    group_filter,
    treatment: str,
    reference: str,
) -> dict | None:
    diffs: list[float] = []
    t_total = 0.0
    r_total = 0.0
    for key, values in pairs.items():
        if not group_filter(key):
            continue
        if treatment in values and reference in values:
            t_val = values[treatment]
            r_val = values[reference]
            if t_val is not None and r_val is not None:
                tf = float(t_val)
                rf = float(r_val)
                diffs.append(tf - rf)
                t_total += tf
                r_total += rf
    if not diffs:
        return None
    mean, ci_low, ci_high = paired_mean_diff_ci(diffs)
    delta_pct = ((t_total - r_total) / r_total) * 100.0 if r_total else None
    return {
        "n_pairs": len(diffs),
        "mean_diff": mean,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "delta_pct": delta_pct,
    }


def _extract_row_outcomes(row: dict) -> dict:
    """Extract separate audit, commit, hidden, and token metrics with legacy compatibility."""
    if "audit_outcome" in row and row["audit_outcome"] is not None:
        audit_outcome = str(row["audit_outcome"])
    else:
        verdicts = row.get("audit_verdicts") or []
        if verdicts:
            audit_outcome = str(verdicts[-1]).lower()
        elif row.get("audit_ran"):
            audit_outcome = "fail" if row.get("audit_failed") else "pass"
        else:
            audit_outcome = "not_run"

    if "audit_passed" in row:
        audit_passed = row["audit_passed"]
    elif audit_outcome == "not_run":
        audit_passed = None
    else:
        audit_passed = audit_outcome == "pass"

    commit_passed = bool(row["commit_passed"]) if row.get("commit_passed") is not None else None
    commit_outcome = row.get("commit_outcome")
    if row.get("commits_added") is not None:
        commit_passed = row["commits_added"] > 0
    elif commit_outcome is not None:
        commit_passed = commit_outcome == "committed"
    hidden_passed = bool(row["hidden_passed"]) if row.get("hidden_passed") is not None else None
    hidden_outcome = row.get("hidden_outcome")
    smoke_passed = bool(row["smoke_passed"]) if row.get("smoke_passed") is not None else None

    tokens = row.get("tokens")
    tokens_measured = (
        bool(row["tokens_measured"])
        if "tokens_measured" in row and row["tokens_measured"] is not None
        else (tokens is not None)
    )
    return {
        "audit_outcome": audit_outcome,
        "audit_passed": audit_passed,
        "commit_outcome": commit_outcome,
        "commit_passed": commit_passed,
        "hidden_outcome": hidden_outcome,
        "hidden_passed": hidden_passed,
        "smoke_passed": smoke_passed,
        "tokens": tokens if tokens_measured else None,
        "tokens_measured": tokens_measured,
        "editor_tokens": row.get("editor_tokens"),
        "audit_tokens": row.get("audit_tokens"),
        "prompt_tokens": row.get("prompt_tokens"),
        "completion_tokens": row.get("completion_tokens"),
        "editor_calls": row.get("editor_calls"),
    }


def _row_control_tuple(
    row: dict,
) -> tuple[str | None, bool | None, bool | None, str | None, bool]:
    base_arm = normalize_arm(row["arm"])
    backend = row.get("local_callers_backend")
    read_range = row.get("local_callers_read_range")
    project_facts = None if row.get("project_facts") is None else bool(row["project_facts"])
    raw_fb = row.get("facts_backend")
    active_fb = (
        str(raw_fb)
        if raw_fb is not None and str(raw_fb) != "off" and project_facts is not False
        else None
    )
    if "local_callers_enabled" in row and row["local_callers_enabled"] is not None:
        local_callers = bool(row["local_callers_enabled"])
    elif "local_callers_facts" in row and row["local_callers_facts"] is not None:
        local_callers = bool(row["local_callers_facts"])
    else:
        local_callers = base_arm != "baseline"
    return (backend, read_range, project_facts, active_fb, local_callers)


def _build_arm_labels(rows: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Assign arm display labels, qualifying only arms that vary across controls."""
    ctrl_type = tuple[str | None, bool | None, bool | None, str | None, bool]
    controls_by_arm: dict[str, set[ctrl_type]] = defaultdict(set)
    row_base_and_ctrl: list[tuple[str, ctrl_type]] = []
    for row in rows:
        base_arm = normalize_arm(row["arm"])
        ctrl = _row_control_tuple(row)
        controls_by_arm[base_arm].add(ctrl)
        row_base_and_ctrl.append((base_arm, ctrl))

    varying_dims: dict[str, tuple[bool, bool, bool, bool, bool]] = {}
    for base_arm, ctrls in controls_by_arm.items():
        if len(ctrls) > 1:
            active_fbs = {c[3] for c in ctrls if c[3] is not None}
            varying_dims[base_arm] = (
                len({c[0] for c in ctrls}) > 1,
                len({c[1] for c in ctrls}) > 1,
                len({c[2] for c in ctrls}) > 1,
                len(active_fbs) > 1,
                len({c[4] for c in ctrls}) > 1,
            )

    labels: list[str] = []
    label_to_base: dict[str, str] = {}
    for base_arm, (backend, read_range, proj_facts, active_fb, local_callers) in row_base_and_ctrl:
        dims = varying_dims.get(base_arm)
        if not dims:
            label = base_arm
        else:
            vary_backend, vary_range, vary_facts, vary_fb, vary_lc = dims
            parts: list[str] = []
            if vary_backend:
                parts.append(f"backend={backend if backend is not None else 'default'}")
            if vary_range:
                range_str = "default" if read_range is None else ("on" if read_range else "off")
                parts.append(f"range={range_str}")
            if vary_facts:
                if proj_facts is None:
                    parts.append("facts=default")
                elif proj_facts:
                    parts.append(f"facts=on({active_fb})" if active_fb else "facts=on")
                else:
                    parts.append("facts=off")
            elif vary_fb:
                parts.append(f"facts_backend={active_fb if active_fb is not None else 'off'}")
            if vary_lc:
                parts.append("lc=on" if local_callers else "lc=off")
            label = f"{base_arm}[{','.join(parts)}]" if parts else base_arm
        labels.append(label)
        label_to_base[label] = base_arm
    return labels, label_to_base


def _order_arm_labels(present: set[str], label_to_base: dict[str, str]) -> list[str]:
    rank = {arm: idx for idx, arm in enumerate(ARM_ORDER)}

    def sort_key(label: str) -> tuple[int, str, str]:
        base = label_to_base.get(label, label)
        return (rank.get(base, len(ARM_ORDER)), base, label)

    return sorted(present, key=sort_key)


def _select_comparison_pairs(
    arms: list[str],
    label_to_base: dict[str, str],
) -> list[tuple[str, str]]:
    """Return ordered (treatment, reference) arm pairs for paired tests and intervals."""
    pairs_out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_pair(treatment: str, reference: str) -> None:
        if treatment == reference:
            return
        key = (treatment, reference)
        if key not in seen:
            seen.add(key)
            pairs_out.append(key)

    present = set(arms)
    for ref in ("baseline", "passive"):
        if ref in present:
            for arm in arms:
                if arm == ref or (ref == "passive" and arm == "baseline"):
                    continue
                add_pair(arm, ref)

    by_base: dict[str, list[str]] = defaultdict(list)
    for arm in arms:
        by_base[label_to_base.get(arm, arm)].append(arm)

    for base_arm, variants in by_base.items():
        if len(variants) > 1:
            ref_variant = variants[0]
            for variant in variants[1:]:
                add_pair(variant, ref_variant)

    if "baseline" not in present and by_base.get("baseline"):
        baseline_by_suffix = {
            arm[len("baseline") :]: arm
            for arm in by_base["baseline"]
            if arm.startswith("baseline[")
        }
        for arm in arms:
            base = label_to_base.get(arm, arm)
            if base == "baseline" or "[" not in arm:
                continue
            suffix = arm[len(base) :]
            if suffix in baseline_by_suffix:
                add_pair(arm, baseline_by_suffix[suffix])

    if not pairs_out and len(arms) > 1:
        for arm in arms[1:]:
            add_pair(arm, arms[0])

    return pairs_out


def build_summary(rows: list[dict]) -> dict:
    """Compute structured trial summary with separate outcomes, tokens, and paired intervals."""
    if not rows:
        return {
            "metadata": {"row_count": 0},
            "arms": [],
            "languages": [],
            "kinds": [],
            "specs": [],
            "groups": [],
            "cells": {},
            "arm_outcomes": {},
            "paired_groups": [],
            "paired_overall": [],
        }

    labels, label_to_base = _build_arm_labels(rows)

    cells: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    passed_pairs: dict[tuple[str, str, str, str, object], dict[str, bool | None]] = defaultdict(
        dict
    )
    hidden_pairs: dict[tuple[str, str, str, str, object], dict[str, bool | None]] = defaultdict(
        dict
    )
    commit_pairs: dict[tuple[str, str, str, str, object], dict[str, bool | None]] = defaultdict(
        dict
    )
    token_pairs: dict[tuple[str, str, str, str, object], dict[str, float | None]] = defaultdict(
        dict
    )
    first_prompt_pairs = defaultdict(dict)
    prompt_pairs = defaultdict(dict)
    seconds_pairs: dict[tuple[str, str, str, str, object], dict[str, float | None]] = defaultdict(
        dict
    )

    arm_stats: dict[str, dict] = defaultdict(
        lambda: {
            "n": 0,
            "passed": 0,
            "hidden_passed": 0,
            "hidden_recorded": 0,
            "smoke_passed": 0,
            "smoke_recorded": 0,
            "commit_passed": 0,
            "commit_recorded": 0,
            "commit_outcomes": defaultdict(int),
            "audit_ran": 0,
            "audit_passed": 0,
            "audit_failed": 0,
            "audit_outcomes": defaultdict(int),
            "evidence": 0,
            "contract_rejected": 0,
            "unknowns": 0,
            "errors": 0,
            "false_approval": 0,
            "extra_calls": 0,
            "seconds": 0.0,
            "second_values": [],
            "tokens": 0,
            "token_values": [],
            "tokens_measured_n": 0,
            "editor_tokens": 0,
            "editor_tokens_measured_n": 0,
            "audit_tokens": 0,
            "audit_tokens_measured_n": 0,
            "prompt_tokens": 0,
            "prompt_tokens_measured_n": 0,
            "completion_tokens": 0,
            "completion_tokens_measured_n": 0,
            "editor_calls": 0,
            "editor_calls_recorded_n": 0,
            "instrumented": 0,
        }
    )

    present_arms: set[str] = set()
    present_langs: set[str] = set()
    present_kinds: set[str] = set()
    present_specs: set[str] = set()
    present_groups: set[tuple[str, str, str]] = set()

    models: set[str] = set()
    git_commits: set[str] = set()
    suites: set[str] = set()
    seeds: set[int] = set()
    backends: set[str] = set()
    read_ranges: set[bool] = set()
    project_facts_set: set[bool] = set()
    facts_backends_set: set[str] = set()

    for r, arm in zip(rows, labels):
        lang = r.get("language") or "python"
        kind = r["kind"]
        spec = r["spec"]
        passed = bool(r["passed"])
        outcomes = _extract_row_outcomes(r)

        present_arms.add(arm)
        present_langs.add(lang)
        present_kinds.add(kind)
        present_specs.add(spec)
        present_groups.add((lang, kind, spec))

        if r.get("model"):
            models.add(str(r["model"]))
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        if meta.get("git_commit"):
            git_commits.add(str(meta["git_commit"]))
        if meta.get("suite"):
            suites.add(str(meta["suite"]))
        if meta.get("seed") is not None:
            seeds.add(meta["seed"])
        if r.get("local_callers_backend") is not None:
            backends.add(str(r["local_callers_backend"]))
        if r.get("local_callers_read_range") is not None:
            read_ranges.add(bool(r["local_callers_read_range"]))
        if r.get("project_facts") is not None:
            project_facts_set.add(bool(r["project_facts"]))
        fb = r.get("facts_backend")
        if fb is not None and (r.get("project_facts") is not None or fb != "off"):
            facts_backends_set.add(str(fb))

        cell = cells[(lang, kind, spec, arm)]
        cell[0] += int(passed)
        cell[1] += 1

        pair_key = (lang, kind, spec, r["case"], r["round"])
        passed_pairs[pair_key][arm] = passed
        if outcomes["hidden_passed"] is not None:
            hidden_pairs[pair_key][arm] = outcomes["hidden_passed"]
        if outcomes["commit_passed"] is not None:
            commit_pairs[pair_key][arm] = outcomes["commit_passed"]
        if outcomes["tokens"] is not None:
            token_pairs[pair_key][arm] = float(outcomes["tokens"])
        if r.get("first_editor_prompt_tokens") is not None:
            first_prompt_pairs[pair_key][arm] = r["first_editor_prompt_tokens"]
        if outcomes["prompt_tokens"] is not None:
            prompt_pairs[pair_key][arm] = outcomes["prompt_tokens"]
        if r.get("seconds") is not None:
            seconds_pairs[pair_key][arm] = float(r["seconds"])

        stat = arm_stats[arm]
        stat["n"] += 1
        stat["passed"] += int(passed)
        if outcomes["hidden_passed"] is not None:
            stat["hidden_recorded"] += 1
            stat["hidden_passed"] += int(outcomes["hidden_passed"])
        if outcomes["smoke_passed"] is not None:
            stat["smoke_recorded"] += 1
            stat["smoke_passed"] += int(outcomes["smoke_passed"])
        if outcomes["commit_passed"] is not None:
            stat["commit_recorded"] += 1
            stat["commit_passed"] += int(outcomes["commit_passed"])
        if outcomes["commit_outcome"] is not None:
            stat["commit_outcomes"][outcomes["commit_outcome"]] += 1

        stat["audit_outcomes"][outcomes["audit_outcome"]] += 1
        if outcomes["audit_passed"] is True:
            stat["audit_passed"] += 1
        stat["audit_ran"] += int(bool(r.get("audit_ran")) or outcomes["audit_outcome"] != "not_run")
        stat["audit_failed"] += int(
            bool(r.get("audit_failed")) or outcomes["audit_outcome"] == "fail"
        )

        stat["evidence"] += int(bool(r.get("evidence")))
        stat["contract_rejected"] += int(bool(r.get("contract_rejected")))
        for field in ("unknowns", "errors", "false_approval", "extra_calls"):
            stat[field] += int(r.get(field) or 0)

        if outcomes["tokens"] is not None:
            stat["instrumented"] += 1
            stat["tokens_measured_n"] += 1
            stat["tokens"] += int(outcomes["tokens"])
            stat["token_values"].append(float(outcomes["tokens"]))
        if outcomes["editor_tokens"] is not None:
            stat["editor_tokens_measured_n"] += 1
            stat["editor_tokens"] += int(outcomes["editor_tokens"])
        if outcomes["audit_tokens"] is not None:
            stat["audit_tokens_measured_n"] += 1
            stat["audit_tokens"] += int(outcomes["audit_tokens"])
        if outcomes["prompt_tokens"] is not None:
            stat["prompt_tokens_measured_n"] += 1
            stat["prompt_tokens"] += int(outcomes["prompt_tokens"])
        if outcomes["completion_tokens"] is not None:
            stat["completion_tokens_measured_n"] += 1
            stat["completion_tokens"] += int(outcomes["completion_tokens"])
        if outcomes["editor_calls"] is not None:
            stat["editor_calls_recorded_n"] += 1
            stat["editor_calls"] += int(outcomes["editor_calls"])
        if r.get("seconds") is not None:
            sec = float(r["seconds"])
            stat["seconds"] += sec
            stat["second_values"].append(sec)

    arms = _order_arm_labels(present_arms, label_to_base)
    langs = ordered_values(present_langs, LANGUAGE_ORDER)
    kinds = ordered_values(present_kinds, KIND_ORDER)
    specs = ordered_values(present_specs, SPEC_ORDER)
    comparison_pairs = _select_comparison_pairs(arms, label_to_base)

    paired_groups: list[dict] = []
    for treatment, ref in comparison_pairs:
        for lang in langs:
            for kind in kinds:
                for spec in specs:
                    if (lang, kind, spec) not in present_groups:
                        continue
                    stats = _paired_binary_stats(
                        passed_pairs,
                        lambda k, lg=lang, kd=kind, sp=spec: k[:3] == (lg, kd, sp),
                        treatment,
                        ref,
                    )
                    if stats is None:
                        continue
                    paired_groups.append(
                        {
                            "scope": f"{lang}/{kind}/{spec}",
                            "treatment": treatment,
                            "reference": ref,
                            **stats,
                        }
                    )
        if len(langs) > 1:
            for kind in kinds:
                for spec in specs:
                    if not any((lang, kind, spec) in present_groups for lang in langs):
                        continue
                    stats = _paired_binary_stats(
                        passed_pairs,
                        lambda k, kd=kind, sp=spec: k[1:3] == (kd, sp),
                        treatment,
                        ref,
                    )
                    if stats is None:
                        continue
                    paired_groups.append(
                        {
                            "scope": f"ALL/{kind}/{spec}",
                            "treatment": treatment,
                            "reference": ref,
                            **stats,
                        }
                    )

    paired_overall: list[dict] = []
    for treatment, ref in comparison_pairs:
        overall_passed = _paired_binary_stats(passed_pairs, lambda _k: True, treatment, ref)
        if overall_passed is None:
            continue
        tokens_stat = _paired_numeric_stats(token_pairs, lambda _k: True, treatment, ref)
        seconds_stat = _paired_numeric_stats(seconds_pairs, lambda _k: True, treatment, ref)
        paired_overall.append(
            {
                "scope": "ALL",
                "treatment": treatment,
                "reference": ref,
                "passed": overall_passed,
                "hidden": _paired_binary_stats(hidden_pairs, lambda _k: True, treatment, ref),
                "commit": _paired_binary_stats(commit_pairs, lambda _k: True, treatment, ref),
                "first_prompt": _paired_numeric_stats(
                    first_prompt_pairs, lambda _k: True, treatment, ref
                ),
                "prompt": _paired_numeric_stats(prompt_pairs, lambda _k: True, treatment, ref),
                "tokens": tokens_stat,
                "seconds": seconds_stat,
                "delta_tokens_pct": tokens_stat["delta_pct"] if tokens_stat is not None else None,
                "delta_seconds_pct": (
                    seconds_stat["delta_pct"] if seconds_stat is not None else None
                ),
            }
        )

    primary_ref_by_arm: dict[str, dict] = {}
    for entry in paired_overall:
        primary_ref_by_arm.setdefault(entry["treatment"], entry)

    arm_outcomes: dict[str, dict] = {}
    for arm in arms:
        stat = arm_stats[arm]
        n = int(stat["n"])
        passed_n = int(stat["passed"])
        measured_n = int(stat["tokens_measured_n"])
        tokens_total = int(stat["tokens"])
        prompt_n = int(stat["prompt_tokens_measured_n"])
        completion_n = int(stat["completion_tokens_measured_n"])
        editor_calls_n = int(stat["editor_calls_recorded_n"])
        tokens_per_solved = (tokens_total / passed_n) if passed_n and measured_n == n else None
        ref_entry = primary_ref_by_arm.get(arm)
        arm_outcomes[arm] = {
            "n": n,
            "passed": passed_n,
            "hidden_passed": int(stat["hidden_passed"]),
            "hidden_recorded": int(stat["hidden_recorded"]),
            "smoke_passed": int(stat["smoke_passed"]),
            "smoke_recorded": int(stat["smoke_recorded"]),
            "commit_passed": int(stat["commit_passed"]),
            "commit_recorded": int(stat["commit_recorded"]),
            "commit_outcomes": dict(stat["commit_outcomes"]),
            "audit_ran": int(stat["audit_ran"]),
            "audit_passed": int(stat["audit_passed"]),
            "audit_failed": int(stat["audit_failed"]),
            "audit_outcomes": dict(stat["audit_outcomes"]),
            "evidence": int(stat["evidence"]),
            "contract_rejected": int(stat["contract_rejected"]),
            "unknowns": int(stat["unknowns"]),
            "errors": int(stat["errors"]),
            "false_approval": int(stat["false_approval"]),
            "extra_calls": int(stat["extra_calls"]),
            "seconds_total": stat["seconds"],
            "mean_seconds": (stat["seconds"] / n) if n else 0.0,
            "p50_seconds": _percentile(stat["second_values"], 50),
            "p95_seconds": _percentile(stat["second_values"], 95),
            "tokens_total": tokens_total,
            "tokens_measured_n": measured_n,
            "mean_tokens": (tokens_total / measured_n) if measured_n else None,
            "p50_tokens": _percentile(stat["token_values"], 50),
            "p95_tokens": _percentile(stat["token_values"], 95),
            "tokens_per_solved": tokens_per_solved,
            "prompt_tokens_per_solved": (
                stat["prompt_tokens"] / passed_n if passed_n and prompt_n == n else None
            ),
            "editor_tokens_total": int(stat["editor_tokens"]),
            "editor_tokens_measured_n": int(stat["editor_tokens_measured_n"]),
            "audit_tokens_total": int(stat["audit_tokens"]),
            "audit_tokens_measured_n": int(stat["audit_tokens_measured_n"]),
            "prompt_tokens_total": int(stat["prompt_tokens"]),
            "prompt_tokens_measured_n": prompt_n,
            "mean_prompt_tokens": (stat["prompt_tokens"] / prompt_n) if prompt_n else None,
            "completion_tokens_total": int(stat["completion_tokens"]),
            "completion_tokens_measured_n": completion_n,
            "mean_completion_tokens": (
                (stat["completion_tokens"] / completion_n) if completion_n else None
            ),
            "editor_calls_total": int(stat["editor_calls"]),
            "editor_calls_recorded_n": editor_calls_n,
            "mean_editor_calls": (
                (stat["editor_calls"] / editor_calls_n) if editor_calls_n else None
            ),
            "delta_tokens_pct": ref_entry["delta_tokens_pct"] if ref_entry is not None else None,
            "delta_seconds_pct": (
                ref_entry["delta_seconds_pct"] if ref_entry is not None else None
            ),
            "instrumented": int(stat["instrumented"]),
        }

    return {
        "metadata": {
            "row_count": len(rows),
            "models": sorted(models),
            "git_commits": sorted(git_commits),
            "suites": sorted(suites),
            "seeds": sorted(seeds),
            "backends": sorted(backends),
            "read_ranges": sorted(read_ranges),
            "facts": sorted(project_facts_set),
            "facts_backends": sorted(facts_backends_set),
        },
        "arms": arms,
        "languages": langs,
        "kinds": kinds,
        "specs": specs,
        "groups": sorted(present_groups),
        "cells": {f"{lg}/{kd}/{sp}/{a}": list(v) for (lg, kd, sp, a), v in cells.items()},
        "arm_outcomes": arm_outcomes,
        "paired_groups": paired_groups,
        "paired_overall": paired_overall,
    }


def _format_ci(lo: float | None, hi: float | None, precision: int = 3) -> str:
    if lo is None or hi is None:
        return "n/a"
    return f"[{lo:+.{precision}f}, {hi:+.{precision}f}]"


def _format_opt_num(val: float | None, precision: int = 1) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{precision}f}"


def _format_opt_pct(val: float | None, *, is_ref: bool = False) -> str:
    if val is None:
        return "ref" if is_ref else "n/a"
    return f"{val:+.1f}%"


def summarize(rows: list[dict]) -> dict:
    """Print grouped pass-rate table, paired sign tests/intervals, and separate outcomes."""
    summary = build_summary(rows)
    if not rows:
        print("No rows to summarize.")
        return summary

    meta = summary["metadata"]
    meta_parts = [f"rows={meta['row_count']}"]
    if meta["models"]:
        meta_parts.append(f"model={','.join(meta['models'])}")
    if meta["suites"]:
        meta_parts.append(f"suite={','.join(meta['suites'])}")
    if meta["seeds"]:
        meta_parts.append(f"seed={','.join(str(s) for s in meta['seeds'])}")
    if meta["git_commits"]:
        meta_parts.append(f"git={','.join(c[:10] for c in meta['git_commits'])}")
    if meta["backends"]:
        meta_parts.append(f"backend={','.join(meta['backends'])}")
    if meta["read_ranges"]:
        rr_str = ",".join("on" if v else "off" for v in meta["read_ranges"])
        meta_parts.append(f"read_range={rr_str}")
    if meta.get("facts"):
        facts_str = ",".join("on" if v else "off" for v in meta["facts"])
        meta_parts.append(f"facts={facts_str}")
    if meta.get("facts_backends"):
        meta_parts.append(f"facts_backend={','.join(meta['facts_backends'])}")
    print("\nRun metadata: " + " ".join(meta_parts))

    arms = summary["arms"]
    langs = summary["languages"]
    kinds = summary["kinds"]
    specs = summary["specs"]
    present_groups = set(summary["groups"])
    cells = summary["cells"]

    col_w = max(14, *(len(a) + 2 for a in arms))
    header = f"{'lang':7} {'kind':8} {'spec':9}" + "".join(f"{a:>{col_w}}" for a in arms)
    print("\n" + header)
    print("-" * len(header))
    for lang in langs:
        for kind in kinds:
            for spec in specs:
                if (lang, kind, spec) not in present_groups:
                    continue
                cols = []
                for arm in arms:
                    ok, total = cells.get(f"{lang}/{kind}/{spec}/{arm}", [0, 0])
                    cell_str = f"{ok}/{total}" if total else "-"
                    cols.append(f"{cell_str:>{col_w}}")
                print(f"{lang:7} {kind:8} {spec:9}" + "".join(cols))

    if len(langs) > 1:
        print("-" * len(header))
        for kind in kinds:
            for spec in specs:
                if not any((lang, kind, spec) in present_groups for lang in langs):
                    continue
                cols = []
                for arm in arms:
                    ok = sum(cells.get(f"{lang}/{kind}/{spec}/{arm}", [0, 0])[0] for lang in langs)
                    total = sum(
                        cells.get(f"{lang}/{kind}/{spec}/{arm}", [0, 0])[1] for lang in langs
                    )
                    cell_str = f"{ok}/{total}" if total else "-"
                    cols.append(f"{cell_str:>{col_w}}")
                print(f"{'ALL':7} {kind:8} {spec:9}" + "".join(cols))

    if summary["paired_groups"]:
        print("\nPaired exact two-sided sign tests & 95% paired intervals:")
        for entry in summary["paired_groups"]:
            print(
                f"  {entry['scope']}: {entry['treatment']} vs {entry['reference']} ->"
                f" +{entry['wins']}/-{entry['losses']}"
                f" (n={entry['n_pairs']}, delta={entry['delta']:+.3f},"
                f" 95% CI {_format_ci(entry['ci_low'], entry['ci_high'])},"
                f" p={entry['p_value']:.4f})"
            )

    if summary["paired_overall"]:
        print("\nPaired overall intervals (95% CI):")
        for entry in summary["paired_overall"]:
            p_stat = entry["passed"]
            parts = [
                f"passed={p_stat['delta']:+.3f} {_format_ci(p_stat['ci_low'], p_stat['ci_high'])}"
                f" (n={p_stat['n_pairs']}, p={p_stat['p_value']:.4f})"
            ]
            if entry["hidden"] is not None:
                h_stat = entry["hidden"]
                parts.append(
                    f"hidden={h_stat['delta']:+.3f}"
                    f" {_format_ci(h_stat['ci_low'], h_stat['ci_high'])}"
                )
            if entry["commit"] is not None:
                c_stat = entry["commit"]
                parts.append(
                    f"commit={c_stat['delta']:+.3f}"
                    f" {_format_ci(c_stat['ci_low'], c_stat['ci_high'])}"
                )
            for metric in ("first_prompt", "prompt"):
                stat = entry[metric]
                if stat is None:
                    parts.append(f"{metric}=n/a (n=0)")
                    continue
                pct = stat["delta_pct"]
                percent = f" ({pct:+.1f}%)" if pct is not None else ""
                parts.append(
                    f"{metric}={stat['mean_diff']:+.1f}{percent}"
                    f" {_format_ci(stat['ci_low'], stat['ci_high'], precision=1)}"
                    f" (n={stat['n_pairs']})"
                )
            if entry["tokens"] is not None:
                t_stat = entry["tokens"]
                t_pct = (
                    f" ({entry['delta_tokens_pct']:+.1f}%)"
                    if entry.get("delta_tokens_pct") is not None
                    else ""
                )
                parts.append(
                    f"tokens={t_stat['mean_diff']:+.1f}{t_pct}"
                    f" {_format_ci(t_stat['ci_low'], t_stat['ci_high'], precision=1)}"
                    f" (n={t_stat['n_pairs']})"
                )
            if entry["seconds"] is not None:
                s_stat = entry["seconds"]
                s_pct = (
                    f" ({entry['delta_seconds_pct']:+.1f}%)"
                    if entry.get("delta_seconds_pct") is not None
                    else ""
                )
                parts.append(
                    f"seconds={s_stat['mean_diff']:+.1f}{s_pct}"
                    f" {_format_ci(s_stat['ci_low'], s_stat['ci_high'], precision=1)}"
                )
            print(f"  {entry['treatment']} vs {entry['reference']}: " + " | ".join(parts))

    print("\nEconomy & efficiency:")
    arm_w = max(16, *(len(a) for a in arms))
    econ_header = (
        f"  {'arm':{arm_w}} {'pass':>6} {'tok_avg':>8} {'tok_p50':>8} {'tok_p95':>8}"
        f" {'tok/sol':>8} {'s_avg':>6} {'s_p50':>6} {'s_p95':>6}"
        f" {'prompt':>8} {'in/sol':>8} {'compl':>7} {'calls':>5} {'d_tok':>7} {'d_sec':>7}"
    )
    print(econ_header)
    print("  " + "-" * (len(econ_header) - 2))
    ref_arms = {entry["reference"] for entry in summary["paired_overall"]}
    treat_arms = {entry["treatment"] for entry in summary["paired_overall"]}
    for arm in arms:
        stat = summary["arm_outcomes"][arm]
        is_ref = arm in ref_arms and arm not in treat_arms
        pass_str = f"{stat['passed']}/{stat['n']}"
        print(
            f"  {arm:{arm_w}} {pass_str:>6}"
            f" {_format_opt_num(stat['mean_tokens']):>8}"
            f" {_format_opt_num(stat['p50_tokens']):>8}"
            f" {_format_opt_num(stat['p95_tokens']):>8}"
            f" {_format_opt_num(stat['tokens_per_solved']):>8}"
            f" {_format_opt_num(stat['mean_seconds']):>6}"
            f" {_format_opt_num(stat['p50_seconds']):>6}"
            f" {_format_opt_num(stat['p95_seconds']):>6}"
            f" {_format_opt_num(stat['mean_prompt_tokens']):>8}"
            f" {_format_opt_num(stat['prompt_tokens_per_solved']):>8}"
            f" {_format_opt_num(stat['mean_completion_tokens']):>7}"
            f" {_format_opt_num(stat['mean_editor_calls'], precision=2):>5}"
            f" {_format_opt_pct(stat['delta_tokens_pct'], is_ref=is_ref):>7}"
            f" {_format_opt_pct(stat['delta_seconds_pct'], is_ref=is_ref):>7}"
        )

    print("\nArm telemetry:")
    for arm in arms:
        stat = summary["arm_outcomes"][arm]
        n = stat["n"]
        hidden_str = (
            f"{stat['hidden_passed']}/{stat['hidden_recorded']}"
            if stat["hidden_recorded"]
            else "n/a"
        )
        commit_str = (
            f"{stat['commit_passed']}/{stat['commit_recorded']}"
            if stat["commit_recorded"]
            else "n/a"
        )
        audit_str = (
            f"{stat['audit_passed']}/{stat['audit_ran']} (fail={stat['audit_failed']})"
            if stat["audit_ran"]
            else f"0 (fail={stat['audit_failed']})"
        )
        token_detail = (
            f" extra_calls={stat['extra_calls']} tokens={stat['tokens_total']}"
            f" (mean={stat['mean_tokens']:.1f}"
            + (
                f", editor={stat['editor_tokens_total']}, audit={stat['audit_tokens_total']}"
                if stat["editor_tokens_measured_n"] == n and stat["audit_tokens_measured_n"] == n
                else ""
            )
            + ")"
            f" false_approvals={stat['false_approval']} unknowns={stat['unknowns']}"
            f" errors={stat['errors']}"
            if stat["instrumented"] == n
            else f" tokens_measured={stat['tokens_measured_n']}/{n} detailed telemetry incomplete"
        )
        print(
            f"  {arm:15} pass={stat['passed']}/{n}"
            f" hidden={hidden_str}"
            f" commit={commit_str}"
            f" audit={audit_str}"
            f" evidence={stat['evidence']}/{n}"
            f" contract_rej={stat['contract_rejected']}"
            f" mean_s={stat['mean_seconds']:.1f}"
            + token_detail
        )
    return summary


def parse_csv_arg(raw: str, allowed: tuple[str, ...], parser: argparse.ArgumentParser, flag: str):
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items:
        parser.error(f"{flag} must contain at least one value")
    out = []
    for item in items:
        normalized = normalize_arm(item) if flag == "--arms" else item
        if normalized not in allowed:
            parser.error(f"Invalid value {item!r} for {flag}; choose from {', '.join(allowed)}")
        if normalized not in out:
            out.append(normalized)
    return out


def select_cases(
    cases_arg: str | None,
    languages_arg: str | None,
    parser: argparse.ArgumentParser,
) -> list[run.CaseSpec]:
    selected = list(CASE_SPECS)
    if languages_arg:
        langs = set(parse_csv_arg(languages_arg, LANGUAGE_ORDER, parser, "--languages"))
        selected = [c for c in selected if c.language in langs]
    if cases_arg:
        allowed_names = tuple(CASE_BY_NAME.keys())
        names = set(parse_csv_arg(cases_arg, allowed_names, parser, "--cases"))
        selected = [c for c in selected if c.name in names]
    if not selected:
        parser.error("No cases matched the given --cases / --languages filters")
    return selected


BOOL_MODE_VALUES = {"default": None, "off": False, "on": True}


def parse_bool_modes(raw: str, parser: argparse.ArgumentParser, flag: str) -> list[bool | None]:
    items = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not items:
        parser.error(f"{flag} must contain at least one value")
    out: list[bool | None] = []
    for item in items:
        if item not in BOOL_MODE_VALUES:
            allowed = ", ".join(BOOL_MODE_VALUES.keys())
            parser.error(f"Invalid value {item!r} for {flag}; choose from {allowed}")
        val = BOOL_MODE_VALUES[item]
        if val not in out:
            out.append(val)
    return out


_UNSET = object()

BASE_DEFAULTS: dict[str, object] = {
    "rounds": 3,
    "arms": "baseline,passive",
    "specs": "vague",
    "suite": "synthetic",
    "read_range_modes": None,
    "facts_modes": None,
}

PRESET_DEFAULTS: dict[str, dict[str, object]] = {
    "issue5-economy": {
        "suite": "issue5",
        "arms": "passive",
        "specs": "vague",
        "rounds": 1,
        "read_range_modes": "off,on",
        "facts_modes": "off,on",
    },
}


def apply_preset_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Apply preset defaults for CLI options that were not explicitly provided."""
    preset_vals = PRESET_DEFAULTS.get(getattr(args, "preset", None) or "", {})
    for key, base_val in BASE_DEFAULTS.items():
        if getattr(args, key, _UNSET) is _UNSET:
            rr_explicit = getattr(args, "local_callers_read_range", None) is not None
            if key == "read_range_modes" and rr_explicit:
                setattr(args, key, None)
            elif key == "facts_modes" and getattr(args, "facts", None) is not None:
                setattr(args, key, None)
            else:
                setattr(args, key, preset_vals.get(key, base_val))
    return args


class _PresetArgumentParser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        ns, extras = super().parse_known_args(args, namespace)
        apply_preset_defaults(ns)
        return ns, extras


def resolve_control_grid(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> list[tuple[str | None, bool | None, bool | None]]:
    """Build the cartesian grid of optional independent (backend, read_range, facts) controls."""
    if args.local_callers_backend is None:
        backends: list[str | None] = [None]
    else:
        backends = [item.strip() for item in args.local_callers_backend.split(",") if item.strip()]
        if not backends:
            parser.error("--local-callers-backend must contain at least one non-empty value")
        backends = list(dict.fromkeys(backends))

    if args.read_range_modes is not None:
        if args.local_callers_read_range is not None:
            parser.error("Use either --local-callers-read-range or --read-range-modes, not both")
        read_ranges = parse_bool_modes(args.read_range_modes, parser, "--read-range-modes")
    else:
        read_ranges = [args.local_callers_read_range]

    if args.facts_modes is not None:
        if args.facts is not None:
            parser.error("Use either --facts/--no-facts or --facts-modes, not both")
        facts_list = parse_bool_modes(args.facts_modes, parser, "--facts-modes")
    else:
        facts_list = [args.facts]

    return [
        (backend, read_range, facts)
        for backend in backends
        for read_range in read_ranges
        for facts in facts_list
    ]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = _PresetArgumentParser(description=__doc__)
    parser.add_argument(
        "--preset",
        choices=sorted(PRESET_DEFAULTS.keys()),
        default=None,
        help="optional trial preset (e.g. issue5-economy for fast economy & efficiency runs)",
    )
    parser.add_argument("--rounds", type=int, default=_UNSET)
    parser.add_argument("--map-tokens", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument("--model", default=run.MODEL)
    parser.add_argument("--out", default=str(Path(__file__).with_name("heldout_results.jsonl")))
    parser.add_argument("--summarize", action="store_true", help="only summarize --out")
    parser.add_argument(
        "--check-fixtures",
        action="store_true",
        help="verify every fixture passes smoke and fails hidden without calling any LLM",
    )
    parser.add_argument(
        "--arms",
        default=_UNSET,
        help=f"comma-separated arms from {', '.join(ARM_ORDER)}",
    )
    parser.add_argument(
        "--specs",
        default=_UNSET,
        help=f"comma-separated specs from {', '.join(SPEC_ORDER)}",
    )
    parser.add_argument(
        "--cases",
        default=None,
        help="optional comma-separated list of case names to run",
    )
    parser.add_argument(
        "--languages",
        default=None,
        help=f"optional comma-separated language filter from {', '.join(LANGUAGE_ORDER)}",
    )
    parser.add_argument(
        "--suite",
        choices=["synthetic", "requests", "issue5", "replay"],
        default=_UNSET,
    )
    parser.add_argument(
        "--replay-manifest",
        default=None,
        metavar="PATH",
        help="path to a frozen replay corpus manifest JSON (for --suite replay)",
    )
    parser.add_argument(
        "--local-callers-backend",
        default=None,
        help="optional backend (or comma-separated list) passed via --local-callers-backend",
    )
    parser.add_argument(
        "--local-callers-read-range",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="optional override for --local-callers-read-range / --no-local-callers-read-range",
    )
    parser.add_argument(
        "--read-range-modes",
        default=_UNSET,
        help="optional comma-separated factorial modes from default,off,on for read-range",
    )
    parser.add_argument(
        "--facts",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="facts",
        help="optional override for Issue 5 project facts (--facts / --no-facts)",
    )
    parser.add_argument(
        "--facts-modes",
        default=_UNSET,
        help="optional comma-separated factorial modes from default,off,on for project facts",
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
    return parser


def resolve_suite_cases(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> list[run.CaseSpec]:
    """Resolve trial cases for the selected suite without running hidden commands."""
    if args.replay_manifest is not None and args.suite != "replay":
        parser.error("--replay-manifest is only valid with --suite replay")

    if args.suite == "requests":
        if args.cases or args.languages:
            parser.error("Use --suite requests without synthetic case or language filters")
        import requests_cases

        return requests_cases.cases()

    if args.suite == "issue5":
        if args.languages:
            parser.error("Use --suite issue5 without --languages filter")
        import issue5_cases

        suite_cases = issue5_cases.cases()
        if args.cases:
            allowed_names = tuple(case.name for case in suite_cases)
            names = set(parse_csv_arg(args.cases, allowed_names, parser, "--cases"))
            suite_cases = [case for case in suite_cases if case.name in names]
        return suite_cases

    if args.suite == "replay":
        if args.cases or args.languages:
            parser.error("Use --suite replay without synthetic case or language filters")
        import replay_corpus

        try:
            return replay_corpus.cases(args.replay_manifest, require_frozen=True)
        except (replay_corpus.FixtureValidationError, ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))

    return select_cases(args.cases, args.languages, parser)


def _build_error_row(
    *,
    case_spec: run.CaseSpec,
    arm: str,
    round_id: int,
    spec: str,
    model: str,
    map_tokens: int,
    backend: str | None,
    read_range: bool | None,
    facts: bool | None,
    facts_db: str | None,
    facts_file: str | None,
    local_callers_override: bool | None,
    exc: Exception,
    elapsed_seconds: float,
    workdir: Path,
    metadata: dict,
) -> dict:
    """Build a schema-compatible error row when run_spec raises unexpectedly."""
    local_callers_enabled = (
        bool(local_callers_override)
        if local_callers_override is not None
        else normalize_arm(arm) != "baseline"
    )
    facts_backend = "sqlite" if facts and facts_db else ("jsonl" if facts else "off")
    return {
        "schema_version": run.SCHEMA_VERSION,
        "model": model,
        "round": round_id,
        "spec": spec,
        "kind": case_spec.kind,
        "language": case_spec.language,
        "case": case_spec.name,
        "arm": arm,
        "local_callers_backend": backend,
        "local_callers_read_range": read_range,
        "local_callers_enabled": local_callers_enabled,
        "local_callers_facts": local_callers_enabled,
        "project_facts": facts,
        "facts_backend": facts_backend,
        "facts_db": facts_db,
        "facts_file": facts_file,
        "map_tokens": map_tokens,
        "exit": -1,
        "trial_error": f"{type(exc).__name__}: {exc}",
        "oracle": False,
        "evidence": False,
        "read_range_attached": False,
        "facts_attached": False,
        "contract_rejected": False,
        "audit_ran": False,
        "audit_verdicts": [],
        "audit_outcome": "not_run",
        "audit_passed": None,
        "audit_calls": 0,
        "audit_failed": False,
        "unknowns": 0,
        "errors": 0,
        "false_approval": False,
        "commit_outcome": "error",
        "commit_passed": False,
        "only_target_modified": False,
        "commits_added": 0,
        "smoke_outcome": "error",
        "smoke_passed": False,
        "hidden_outcome": "error",
        "hidden_passed": False,
        "editor_calls": 0,
        "extra_calls": 0,
        "tokens": None,
        "tokens_measured": False,
        "editor_tokens": None,
        "audit_tokens": None,
        "first_editor_prompt_tokens": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "calls_by_role": {},
        "tokens_by_role": {},
        "passed": False,
        "seconds": round(elapsed_seconds, 1),
        "patch": "",
        "workdir": str(workdir),
        "metadata": dict(metadata),
    }


def main(argv: list[str] | None = None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.replay_manifest is not None and args.suite != "replay":
        parser.error("--replay-manifest is only valid with --suite replay")

    out = Path(args.out)
    if args.summarize:
        rows = load_results(out)
        summarize(rows)
        return

    selected_cases = resolve_suite_cases(args, parser)
    if args.check_fixtures:
        raise SystemExit(check_fixtures(selected_cases))

    arms = parse_csv_arg(args.arms, ARM_ORDER, parser, "--arms")
    specs = parse_csv_arg(args.specs, SPEC_ORDER, parser, "--specs")
    control_grid = resolve_control_grid(args, parser)

    jobs = [
        (r, case_spec, spec, arm, backend, read_range, facts)
        for r in range(args.rounds)
        for case_spec in selected_cases
        for spec in specs
        for arm in arms
        for backend, read_range, facts in control_grid
    ]
    random.Random(args.seed).shuffle(jobs)

    run_meta = run.collect_run_metadata(
        model=args.model,
        suite=args.suite,
        map_tokens=args.map_tokens,
        rounds=args.rounds,
        seed=args.seed,
        arms=arms,
        specs=specs,
        cases=[case.name for case in selected_cases],
        controls={
            "backends": [b for b in dict.fromkeys(b for b, _, _ in control_grid)],
            "read_ranges": [rr for rr in dict.fromkeys(rr for _, rr, _ in control_grid)],
            "facts": [f for f in dict.fromkeys(f for _, _, f in control_grid)],
            "facts_db": args.facts_db,
            "facts_file": args.facts_file,
            "local_callers": args.local_callers_override,
            "preset": args.preset,
        },
    )
    run.write_run_metadata(out.with_suffix(".meta.json"), run_meta)
    row_meta = {
        "schema_version": run_meta["schema_version"],
        "run_id": run_meta["run_id"],
        "timestamp_utc": run_meta["timestamp_utc"],
        "git_commit": run_meta["git_commit"],
        "git_dirty": run_meta["git_dirty"],
        "python_version": run_meta["python_version"],
        "suite": run_meta["suite"],
        "seed": run_meta["seed"],
    }

    workdir = Path(tempfile.mkdtemp(prefix="aider-heldout-"))
    print(workdir, flush=True)
    rows = []
    for r, case_spec, spec, arm, backend, read_range, facts in jobs:
        job_start = time.monotonic()
        try:
            result = run.run_spec(
                output=workdir,
                case_spec=case_spec,
                arm=arm,
                round_id=r,
                map_tokens=args.map_tokens,
                spec=spec,
                model=args.model,
                backend=backend,
                read_range=read_range,
                facts=facts,
                facts_db=args.facts_db,
                facts_file=args.facts_file,
                local_callers=args.local_callers_override,
                metadata=row_meta,
            )
        except Exception as exc:
            result = _build_error_row(
                case_spec=case_spec,
                arm=arm,
                round_id=r,
                spec=spec,
                model=args.model,
                map_tokens=args.map_tokens,
                backend=backend,
                read_range=read_range,
                facts=facts,
                facts_db=args.facts_db,
                facts_file=args.facts_file,
                local_callers_override=args.local_callers_override,
                exc=exc,
                elapsed_seconds=time.monotonic() - job_start,
                workdir=workdir,
                metadata=row_meta,
            )
        rows.append(result)
        print(json.dumps({k: v for k, v in result.items() if k != "patch"}), flush=True)
        with out.open("a") as stream:
            stream.write(json.dumps(result) + "\n")
    summarize(rows)


if __name__ == "__main__":
    main()
