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

sys.path.insert(0, str(Path(__file__).parent))
import run  # noqa: E402

ARM_ORDER = run.VALID_ARMS
LANGUAGE_ORDER = ("python", "js")
KIND_ORDER = ("caller", "noisy", "control")
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


def sign_test(wins: int, losses: int) -> float:
    n = wins + losses
    if not n:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def check_fixtures(cases: list[run.CaseSpec]) -> int:
    """Verify every fixture passes smoke and fails hidden without invoking any LLM."""
    failures = 0
    with tempfile.TemporaryDirectory(prefix="aider-fixture-check-") as tmp:
        root = Path(tmp)
        for case in cases:
            repo = root / case.name
            repo.mkdir()
            run.write_fixture_files(repo, case.files)
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
            total += 1
            win += int(bool(outcomes[treatment]) and not bool(outcomes[reference]))
            loss += int(bool(outcomes[reference]) and not bool(outcomes[treatment]))
    return win, loss, total


def summarize(rows: list[dict]) -> None:
    """Print grouped pass-rate table and paired exact sign-test p-values."""
    if not rows:
        print("No rows to summarize.")
        return

    cells: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    pairs: dict[tuple[str, str, str, str, object], dict[str, bool]] = defaultdict(dict)
    arm_telemetry: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "n": 0,
            "passed": 0,
            "evidence": 0,
            "contract_rejected": 0,
            "audit_ran": 0,
            "audit_failed": 0,
            "seconds": 0.0,
            "unknowns": 0,
            "errors": 0,
            "false_approval": 0,
            "extra_calls": 0,
            "tokens": 0,
            "instrumented": 0,
        }
    )

    present_arms: set[str] = set()
    present_langs: set[str] = set()
    present_kinds: set[str] = set()
    present_specs: set[str] = set()
    present_groups: set[tuple[str, str, str]] = set()

    for r in rows:
        lang = r.get("language") or "python"
        kind = r["kind"]
        spec = r["spec"]
        arm = normalize_arm(r["arm"])
        passed = bool(r["passed"])

        present_arms.add(arm)
        present_langs.add(lang)
        present_kinds.add(kind)
        present_specs.add(spec)
        present_groups.add((lang, kind, spec))

        cell = cells[(lang, kind, spec, arm)]
        cell[0] += int(passed)
        cell[1] += 1

        pairs[(lang, kind, spec, r["case"], r["round"])][arm] = passed

        stat = arm_telemetry[arm]
        stat["n"] += 1
        stat["passed"] += int(passed)
        stat["evidence"] += int(bool(r.get("evidence")))
        stat["contract_rejected"] += int(bool(r.get("contract_rejected")))
        stat["audit_ran"] += int(bool(r.get("audit_ran")))
        stat["audit_failed"] += int(bool(r.get("audit_failed")))
        if r.get("tokens") is not None:
            stat["instrumented"] += 1
        for field in ("unknowns", "errors", "false_approval", "extra_calls", "tokens"):
            stat[field] += r.get(field) or 0
        stat["seconds"] += float(r.get("seconds", 0.0))

    arms = ordered_values(present_arms, ARM_ORDER)
    langs = ordered_values(present_langs, LANGUAGE_ORDER)
    kinds = ordered_values(present_kinds, KIND_ORDER)
    specs = ordered_values(present_specs, SPEC_ORDER)

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
                    ok, total = cells[(lang, kind, spec, arm)]
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
                    ok = sum(cells[(lang, kind, spec, arm)][0] for lang in langs)
                    total = sum(cells[(lang, kind, spec, arm)][1] for lang in langs)
                    cell_str = f"{ok}/{total}" if total else "-"
                    cols.append(f"{cell_str:>{col_w}}")
                print(f"{'ALL':7} {kind:8} {spec:9}" + "".join(cols))

    references = [ref for ref in ("baseline", "passive") if ref in present_arms]
    if references:
        print("\nPaired exact two-sided sign tests:")
        for ref in references:
            treatments = [
                a for a in arms if a != ref and not (ref == "passive" and a == "baseline")
            ]
            if not treatments:
                continue
            for lang in langs:
                for kind in kinds:
                    for spec in specs:
                        if (lang, kind, spec) not in present_groups:
                            continue
                        for arm in treatments:
                            win, loss, n_pairs = _paired_comparison(
                                pairs,
                                lambda k, lg=lang, kd=kind, sp=spec: k[:3] == (lg, kd, sp),
                                arm,
                                ref,
                            )
                            if not n_pairs:
                                continue
                            pval = sign_test(win, loss)
                            print(
                                f"  {lang}/{kind}/{spec}: {arm} vs {ref} ->"
                                f" +{win}/-{loss} (n={n_pairs}, p={pval:.4f})"
                            )
            if len(langs) > 1:
                for kind in kinds:
                    for spec in specs:
                        if not any((lang, kind, spec) in present_groups for lang in langs):
                            continue
                        for arm in treatments:
                            win, loss, n_pairs = _paired_comparison(
                                pairs,
                                lambda k, kd=kind, sp=spec: k[1:3] == (kd, sp),
                                arm,
                                ref,
                            )
                            if not n_pairs:
                                continue
                            pval = sign_test(win, loss)
                            print(
                                f"  ALL/{kind}/{spec}: {arm} vs {ref} ->"
                                f" +{win}/-{loss} (n={n_pairs}, p={pval:.4f})"
                            )

    print("\nArm telemetry:")
    for arm in arms:
        stat = arm_telemetry[arm]
        n = int(stat["n"])
        mean_sec = stat["seconds"] / n if n else 0.0
        print(
            f"  {arm:15} pass={int(stat['passed'])}/{n}"
            f" evidence={int(stat['evidence'])}/{n}"
            f" contract_rej={int(stat['contract_rejected'])}"
            f" audit={int(stat['audit_ran'])} (fail={int(stat['audit_failed'])})"
            f" mean_s={mean_sec:.1f}"
            + (
                f" extra_calls={stat['extra_calls']} tokens={stat['tokens']}"
                f" false_approvals={stat['false_approval']} unknowns={stat['unknowns']}"
                f" errors={stat['errors']}"
                if stat["instrumented"] == n
                else " detailed telemetry incomplete"
            )
        )


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
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
        default="baseline,passive",
        help=f"comma-separated arms from {', '.join(ARM_ORDER)}",
    )
    parser.add_argument(
        "--specs",
        default="vague",
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
    parser.add_argument("--suite", choices=["synthetic", "requests"], default="synthetic")
    args = parser.parse_args()

    out = Path(args.out)
    if args.summarize:
        rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        summarize(rows)
        return

    if args.suite == "requests":
        import requests_cases

        selected_cases = requests_cases.cases()
        if args.cases or args.languages:
            parser.error("Use --suite requests without synthetic case or language filters")
    else:
        selected_cases = select_cases(args.cases, args.languages, parser)
    if args.check_fixtures:
        raise SystemExit(check_fixtures(selected_cases))

    arms = parse_csv_arg(args.arms, ARM_ORDER, parser, "--arms")
    specs = parse_csv_arg(args.specs, SPEC_ORDER, parser, "--specs")

    jobs = [
        (r, case_spec, spec, arm)
        for r in range(args.rounds)
        for case_spec in selected_cases
        for spec in specs
        for arm in arms
    ]
    random.Random(args.seed).shuffle(jobs)

    workdir = Path(tempfile.mkdtemp(prefix="aider-heldout-"))
    print(workdir, flush=True)
    rows = []
    for r, case_spec, spec, arm in jobs:
        result = run.run_spec(
            output=workdir,
            case_spec=case_spec,
            arm=arm,
            round_id=r,
            map_tokens=args.map_tokens,
            spec=spec,
            model=args.model,
        )
        rows.append(result)
        print(json.dumps({k: v for k, v in result.items() if k != "patch"}), flush=True)
        with out.open("a") as stream:
            stream.write(json.dumps(result) + "\n")
    summarize(rows)


if __name__ == "__main__":
    main()
