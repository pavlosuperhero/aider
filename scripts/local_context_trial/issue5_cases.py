"""Self-contained Issue 5 evaluation suite for local context features.

Tests project facts (JSONL + chat_id and SQLite FTS5), source-hash stale fact
invalidation, bounded read-range beyond the 128 KiB file cap, and scoped multi-
consumer caller discovery with a duplicate local helper.
"""

import run


def _build_large_consumer() -> str:
    """Build a >140 KiB, >3600-line valid Python consumer with a bottom call site."""
    header = [
        "from api import normalize_code\n",
        "\n",
        "_CATALOG_ROWS = (\n",
    ]
    padding = [
        f'    "catalog_row_{idx:04d}_padding_payload_0123456789",\n'
        for idx in range(3650)
    ]
    footer = [
        ")\n",
        "\n",
        "\n",
        "def catalog_size():\n",
        "    return len(_CATALOG_ROWS)\n",
        "\n",
        "\n",
        "def route_code(raw):\n",
        "    code = normalize_code(raw)\n",
        "    if code == 'MISSING_CODE':\n",
        "        return 'fallback-queue'\n",
        "    return f'queue:{code}'\n",
    ]
    return "".join(header + padding + footer)


def cases() -> list[run.CaseSpec]:
    """Return the 5 focused Issue 5 CaseSpec fixtures."""
    return [
        run.py_case(
            name="facts_currency",
            files={
                "api.py": (
                    "def format_total(cents):\n"
                    '    return f"{cents / 100:.2f}"\n'
                ),
                "consumer.py": (
                    "from api import format_total\n\n"
                    "def invoice_line(item, cents):\n"
                    '    return f"{item}: {format_total(cents)}"\n'
                ),
                "smoke.py": (
                    "from api import format_total\n"
                    "assert format_total(1250) == '12.50'\n"
                ),
            },
            task=(
                "Update format_total to follow the project's billing display convention"
                " for zero and non-zero totals. Keep non-zero formatting consistent with"
                " existing smoke checks."
            ),
            hidden=(
                "from consumer import invoice_line\n"
                "assert invoice_line('Widget', 1250) == 'Widget: 12.50'\n"
                "assert invoice_line('Bonus', 0) == 'Bonus: FREE'\n"
            ),
            explicit=(
                "format_total(0) must return 'FREE' and format_total(1250) must return"
                " '12.50' as a plain decimal with no '$' prefix."
            ),
            kind="facts",
            seed_facts=(
                {
                    "statement": (
                        "format_total(cents) must format non-zero amounts as plain"
                        " decimals with two decimal places and no '$' prefix"
                        " (e.g. 1250 -> '12.50'), and must return 'FREE' instead of"
                        " '0.00' when cents == 0."
                    ),
                    "source_paths": ("api.py",),
                    "tags": ("currency", "formatting", "format_total"),
                    "note": "Confirmed billing display rule for invoice totals.",
                },
            ),
        ),
        run.py_case(
            name="facts_sentinel",
            files={
                "api.py": (
                    "PRIORITIES = {'low': 1, 'medium': 2, 'high': 3}\n\n\n"
                    "def parse_priority(raw):\n"
                    "    return PRIORITIES[raw.strip().lower()]\n"
                ),
                "consumer.py": (
                    "from api import parse_priority\n\n\n"
                    "def priority_bucket(raw):\n"
                    "    score = parse_priority(raw)\n"
                    "    if score < 0:\n"
                    "        return 'unranked'\n"
                    '    return f"p{score}"\n'
                ),
                "smoke.py": (
                    "from api import parse_priority\n"
                    "assert parse_priority('high') == 3\n"
                ),
            },
            task=(
                "Update parse_priority to handle blank and unknown priority inputs using"
                " the project's sentinel convention instead of raising. Keep known"
                " priority labels working."
            ),
            hidden=(
                "from api import parse_priority\n"
                "from consumer import priority_bucket\n"
                "assert parse_priority('high') == 3\n"
                "assert parse_priority('') == -99\n"
                "assert parse_priority('   ') == -99\n"
                "assert parse_priority('urgent') == -99\n"
                "assert priority_bucket('unknown') == 'unranked'\n"
                "assert priority_bucket('low') == 'p1'\n"
            ),
            explicit=(
                "parse_priority must return -99 for blank or unknown labels and preserve"
                " 1/2/3 for low/medium/high."
            ),
            kind="facts",
            seed_facts=(
                {
                    "statement": (
                        "parse_priority(raw) must return the integer sentinel -99 for"
                        " blank, whitespace-only, None, or unknown priority inputs"
                        " instead of raising."
                    ),
                    "source_paths": ("api.py",),
                    "tags": ("priority", "sentinel", "parse_priority"),
                    "note": "Confirmed triage sentinel value for missing/unknown labels.",
                },
            ),
        ),
        run.py_case(
            name="facts_stale_control",
            files={
                "api.py": (
                    "def compute_tax(amount_cents, rate_percent=10):\n"
                    "    return (amount_cents * rate_percent) // 100\n"
                ),
                "policy.py": (
                    "# Legacy tax policy v1: flat 5% tax rate.\n"
                    "TAX_POLICY_VERSION = 1\n"
                ),
                "consumer.py": (
                    "from api import compute_tax\n\n\n"
                    "def total_with_tax(amount_cents, rate_percent=10):\n"
                    "    return amount_cents + compute_tax(amount_cents, rate_percent)\n"
                ),
                "smoke.py": (
                    "from api import compute_tax\n"
                    "assert compute_tax(1000, 10) == 100\n"
                ),
            },
            task=(
                "Update compute_tax(amount_cents, rate_percent=10) to round half-up to"
                " the nearest cent using (amount_cents * rate_percent + 50) // 100 and"
                " return 0 when amount_cents <= 0 or rate_percent <= 0."
            ),
            hidden=(
                "from api import compute_tax\n"
                "from consumer import total_with_tax\n"
                "assert compute_tax(1000, 10) == 100\n"
                "assert compute_tax(105, 10) == 11\n"
                "assert compute_tax(104, 10) == 10\n"
                "assert compute_tax(-50, 10) == 0\n"
                "assert compute_tax(100, 0) == 0\n"
                "assert total_with_tax(105, 10) == 116\n"
            ),
            explicit=(
                "compute_tax must use rate_percent with half-up rounding and return 0"
                " for non-positive amounts or rates."
            ),
            kind="control",
            seed_facts=(
                {
                    "statement": (
                        "compute_tax(amount_cents) must ignore rate_percent and always"
                        " charge a legacy flat 5% rate: (amount_cents * 5) // 100."
                    ),
                    "source_paths": ("policy.py",),
                    "tags": ("tax", "compute_tax", "policy"),
                    "note": "Legacy v1 policy superseded when policy.py changes.",
                    "mutate_after_seed": {
                        "policy.py": (
                            "# Updated tax policy v2: honor explicit rate_percent.\n"
                            "TAX_POLICY_VERSION = 2\n"
                        ),
                    },
                },
            ),
        ),
        run.py_case(
            name="read_range_large_file",
            files={
                "api.py": (
                    "def normalize_code(raw):\n"
                    "    return raw.strip().upper()\n"
                ),
                "consumer.py": _build_large_consumer(),
                "smoke.py": (
                    "from api import normalize_code\n"
                    "assert normalize_code(' ab-1 ') == 'AB-1'\n"
                ),
            },
            task=(
                "Update normalize_code to return the consumer's expected sentinel string"
                " when raw is blank or whitespace-only. Preserve uppercase trimming for"
                " non-blank codes."
            ),
            hidden=(
                "from consumer import route_code\n"
                "assert route_code(' ab-1 ') == 'queue:AB-1'\n"
                "assert route_code('   ') == 'fallback-queue'\n"
                "assert route_code('') == 'fallback-queue'\n"
            ),
            explicit=(
                "normalize_code must return 'MISSING_CODE' for blank or whitespace-only"
                " input so route_code returns 'fallback-queue'."
            ),
            kind="read_range",
        ),
        run.py_case(
            name="caller_multi_scope",
            files={
                "api.py": (
                    "def parse_window(spec):\n"
                    "    left, right = spec.split(':', 1)\n"
                    "    return (int(left), int(right))\n"
                ),
                "scheduler.py": (
                    "from api import parse_window\n\n\n"
                    "def duration_seconds(spec):\n"
                    "    start_sec, end_sec = parse_window(spec)\n"
                    "    if (start_sec, end_sec) == (-1, -1):\n"
                    "        return 0\n"
                    "    return end_sec - start_sec\n"
                ),
                "formatter.py": (
                    "from api import parse_window\n\n\n"
                    "def format_window(spec):\n"
                    "    start_sec, end_sec = parse_window(spec)\n"
                    "    if (start_sec, end_sec) == (-1, -1):\n"
                    "        return 'unbounded'\n"
                    '    return f"{start_sec}s..{end_sec}s"\n'
                ),
                "local_helper.py": (
                    "def parse_window(spec):\n"
                    "    return {'raw': spec}\n\n\n"
                    "def preview_window(spec):\n"
                    "    info = parse_window(spec)\n"
                    "    return info['raw']\n"
                ),
                "smoke.py": (
                    "from api import parse_window\n"
                    "assert parse_window('10:25') == (10, 25)\n"
                ),
            },
            task=(
                "Update parse_window to handle blank or whitespace-only specs using the"
                " sentinel tuple its production callers expect. Preserve 'start:end'"
                " integer parsing."
            ),
            hidden=(
                "from scheduler import duration_seconds\n"
                "from formatter import format_window\n"
                "assert duration_seconds('10:25') == 15\n"
                "assert duration_seconds('') == 0\n"
                "assert format_window('10:25') == '10s..25s'\n"
                "assert format_window('  ') == 'unbounded'\n"
            ),
            explicit=(
                "parse_window must return (-1, -1) for blank or whitespace-only input"
                " so duration_seconds returns 0 and format_window returns 'unbounded'."
            ),
            kind="caller",
        ),
    ]
