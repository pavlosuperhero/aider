"""Deterministic, versioned fixture corpus for Issue #5 Section B retrieval benchmarks.

Provides 10 reproducible fixture categories covering single-file, multi-file,
multi-language (Python + JS/TS), import aliases, same-name disambiguation,
production-vs-test prioritization, transitive callers, large files (>140 KiB,
target past line 4000), real sibling git worktrees (--no-federate isolation),
and negative controls (zero callers).

Each fixture materializes into a clean temporary git repository (and sibling
git worktree when applicable) via a context manager with guaranteed teardown.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

CORPUS_VERSION = "1.0"

SECTION_B_CATEGORIES: tuple[str, ...] = (
    "single_file",
    "multi_file",
    "multi_language",
    "aliases",
    "same_name",
    "prod_vs_test",
    "transitive",
    "large_file",
    "multi_worktree",
    "negative_control",
)

LARGE_FILE_MIN_BYTES = 140 * 1024
LARGE_FILE_MIN_LINE = 4000

_GIT_ENV_OVERRIDES = {
    "GIT_AUTHOR_NAME": "Aider Retrieval Benchmark",
    "GIT_AUTHOR_EMAIL": "bench@aider.local",
    "GIT_COMMITTER_NAME": "Aider Retrieval Benchmark",
    "GIT_COMMITTER_EMAIL": "bench@aider.local",
    "GIT_AUTHOR_DATE": "2026-01-15T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-15T00:00:00Z",
}


@dataclass(frozen=True)
class ExpectedCaller:
    """Ground-truth caller location and contract-critical substrings."""

    rel_path: str
    line: int
    symbol: str
    kind: str = "direct"
    is_test: bool = False
    contract_substrings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadRangeTarget:
    """Ground-truth target for ReadRange vs 700-char excerpt vs cymbal show comparison."""

    rel_path: str
    call_line: int
    range_start: int
    range_end: int
    symbol: str
    contract_substrings: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class RetrievalCase:
    """Immutable deterministic benchmark case for Section B retrieval evaluation."""

    case_id: str
    category: str
    description: str
    files: dict[str, str]
    editable: tuple[str, ...]
    candidates: tuple[str, ...]
    message: str
    target_symbol: str
    expected_callers: tuple[ExpectedCaller, ...]
    wrong_target_paths: tuple[str, ...] = ()
    expect_ambiguous: bool = False
    worktree_files: dict[str, str] = field(default_factory=dict)
    worktree_branch: str = ""
    read_range_targets: tuple[ReadRangeTarget, ...] = ()
    corpus_version: str = CORPUS_VERSION


@dataclass(frozen=True)
class MaterializedCase:
    """Live materialized temporary git repository (and optional sibling worktree)."""

    case: RetrievalCase
    base_dir: Path
    repo_root: Path
    worktree_roots: tuple[Path, ...] = ()


def _find_line(source: str, needle: str) -> int:
    """Return the 1-based line number of the first line containing ``needle``."""
    for idx, line in enumerate(source.splitlines(), start=1):
        if needle in line:
            return idx
    raise ValueError(f"Needle not found in fixture source: {needle!r}")


def _build_single_file_case() -> RetrievalCase:
    pricing_src = (
        "def compute_discount(subtotal_cents, tier):\n"
        "    if tier == 'vip':\n"
        "        return subtotal_cents // 10\n"
        "    return 0\n"
    )
    checkout_src = (
        "from billing.pricing import compute_discount\n"
        "\n"
        "\n"
        "def apply_checkout_discount(subtotal_cents, tier, coupon_note=''):\n"
        "    discount_cents = compute_discount(subtotal_cents, tier)\n"
        "    if discount_cents < 0:\n"
        "        raise ValueError('NEGATIVE_DISCOUNT')\n"
        "    # Audit logging and ledger normalization lines separate the call site\n"
        "    # from the final contract-critical sentinel check so fixed 700-char\n"
        "    # +/-3-line caller excerpts can silently truncate downstream invariants.\n"
        "    audit_entries = [\n"
        "        f'subtotal_cents={subtotal_cents}',\n"
        "        f'discount_cents={discount_cents}',\n"
        "        f'tier={tier}',\n"
        "        f'coupon_note={coupon_note.strip()}',\n"
        "        'currency=USD_CENTS',\n"
        "        'rounding_policy=FLOOR_INT',\n"
        "        'ledger_channel=checkout_v2',\n"
        "    ]\n"
        "    if not isinstance(discount_cents, int):\n"
        "        raise TypeError('DISCOUNT_MUST_BE_INT_CENTS')\n"
        "    net_cents = subtotal_cents - discount_cents\n"
        "    if net_cents == 0:\n"
        "        return ('COMP_ORDER', 0, tuple(audit_entries))\n"
        "    return ('CHARGED', net_cents, tuple(audit_entries))\n"
    )
    call_line = _find_line(checkout_src, "discount_cents = compute_discount(")
    end_line = len(checkout_src.splitlines())
    return RetrievalCase(
        case_id="b01_single_file",
        category="single_file",
        description="Single production caller file with downstream contract invariants.",
        files={
            "billing/__init__.py": "",
            "billing/pricing.py": pricing_src,
            "billing/checkout.py": checkout_src,
        },
        editable=("billing/pricing.py",),
        candidates=("billing/checkout.py",),
        message="Update compute_discount to preserve integer cents and checkout invariants.",
        target_symbol="compute_discount",
        expected_callers=(
            ExpectedCaller(
                rel_path="billing/checkout.py",
                line=call_line,
                symbol="compute_discount",
                kind="direct",
                contract_substrings=("discount_cents < 0",),
            ),
        ),
        read_range_targets=(
            ReadRangeTarget(
                rel_path="billing/checkout.py",
                call_line=call_line,
                range_start=4,
                range_end=end_line,
                symbol="apply_checkout_discount",
                contract_substrings=(
                    "discount_cents < 0",
                    "DISCOUNT_MUST_BE_INT_CENTS",
                    "COMP_ORDER",
                ),
                description="Full caller function spans >700 chars past call line.",
            ),
        ),
    )


def _build_multi_file_case() -> RetrievalCase:
    normalize_src = (
        "def normalize_sku(raw_sku):\n"
        "    cleaned = (raw_sku or '').strip().upper()\n"
        "    if not cleaned:\n"
        "        return None\n"
        "    return ('SKU', cleaned)\n"
    )
    orders_src = (
        "from core.normalize import normalize_sku\n"
        "\n"
        "\n"
        "def create_order_line(raw_sku, quantity):\n"
        "    parsed = normalize_sku(raw_sku)\n"
        "    if parsed is None:\n"
        "        raise ValueError('EMPTY_SKU')\n"
        "    # Additional telemetry and packing metadata between call site and tuple check.\n"
        "    metadata = {\n"
        "        'warehouse': 'us-east-primary',\n"
        "        'quantity': int(quantity),\n"
        "        'channel': 'retail_web_checkout',\n"
        "        'validation_mode': 'strict_prefix',\n"
        "        'notes': 'verify tuple prefix ordering before persistence',\n"
        "    }\n"
        "    prefix, code = parsed\n"
        "    if prefix != 'SKU':\n"
        "        raise ValueError('INVALID_SKU_PREFIX')\n"
        "    return f'{prefix}-{code}', metadata\n"
    )
    inventory_src = (
        "from core.normalize import normalize_sku\n"
        "\n"
        "\n"
        "def reserve_stock(raw_sku, count):\n"
        "    result = normalize_sku(raw_sku)\n"
        "    if result is None:\n"
        "        return 'SKIP_UNKNOWN'\n"
        "    prefix, code = result\n"
        "    return f'RESERVE:{prefix}:{code}:{count}'\n"
    )
    returns_src = (
        "from core.normalize import normalize_sku\n"
        "\n"
        "\n"
        "def register_return(raw_sku):\n"
        "    item = normalize_sku(raw_sku)\n"
        "    if item is None:\n"
        "        return None\n"
        "    prefix, code = item\n"
        "    return f'RMA-{prefix}-{code}'\n"
    )
    unrelated_src = (
        "def format_warehouse_label(name):\n"
        "    return name.strip().upper()\n"
    )
    orders_line = _find_line(orders_src, "parsed = normalize_sku(")
    inv_line = _find_line(inventory_src, "result = normalize_sku(")
    ret_line = _find_line(returns_src, "item = normalize_sku(")
    return RetrievalCase(
        case_id="b02_multi_file",
        category="multi_file",
        description="Three distinct production caller files plus one unrelated module.",
        files={
            "core/__init__.py": "",
            "core/normalize.py": normalize_src,
            "services/__init__.py": "",
            "services/orders.py": orders_src,
            "services/inventory.py": inventory_src,
            "services/returns.py": returns_src,
            "services/unrelated.py": unrelated_src,
        },
        editable=("core/normalize.py",),
        candidates=(
            "services/orders.py",
            "services/inventory.py",
            "services/returns.py",
            "services/unrelated.py",
        ),
        message="Update normalize_sku while keeping all service callers compatible.",
        target_symbol="normalize_sku",
        expected_callers=(
            ExpectedCaller(
                rel_path="services/inventory.py",
                line=inv_line,
                symbol="normalize_sku",
                kind="direct",
                contract_substrings=("prefix, code = result",),
            ),
            ExpectedCaller(
                rel_path="services/orders.py",
                line=orders_line,
                symbol="normalize_sku",
                kind="direct",
                contract_substrings=("parsed is None",),
            ),
            ExpectedCaller(
                rel_path="services/returns.py",
                line=ret_line,
                symbol="normalize_sku",
                kind="direct",
                contract_substrings=("RMA-",),
            ),
        ),
        wrong_target_paths=("services/unrelated.py",),
        read_range_targets=(
            ReadRangeTarget(
                rel_path="services/orders.py",
                call_line=orders_line,
                range_start=4,
                range_end=len(orders_src.splitlines()),
                symbol="create_order_line",
                contract_substrings=(
                    "parsed is None",
                    "prefix, code = parsed",
                    "INVALID_SKU_PREFIX",
                ),
                description="Order caller validates tuple prefix past 700-char boundary.",
            ),
        ),
    )


def _build_multi_language_case() -> RetrievalCase:
    py_currency_src = (
        "def format_ledger_amount(cents):\n"
        "    if cents == 0:\n"
        "        return 'FREE'\n"
        "    return f'{cents / 100:.2f}'\n"
    )
    py_report_src = (
        "from py.currency import format_ledger_amount\n"
        "\n"
        "\n"
        "def render_ledger_row(label, cents):\n"
        "    formatted = format_ledger_amount(cents)\n"
        "    if formatted == 'FREE':\n"
        "        return f'{label}: COMP'\n"
        "    return f'{label}: {formatted}'\n"
    )
    ts_currency_src = (
        "export function formatLedgerAmount(cents: number): string {\n"
        "  if (cents === 0) {\n"
        "    return 'FREE';\n"
        "  }\n"
        "  return (cents / 100).toFixed(2);\n"
        "}\n"
    )
    ts_cart_src = (
        "import { formatLedgerAmount } from './currency';\n"
        "\n"
        "export function renderCartSummary(item: string, cents: number): string {\n"
        "  const display = formatLedgerAmount(cents);\n"
        "  const auditTags = [\n"
        "    'channel=web_cart',\n"
        "    'currency=USD_DECIMAL',\n"
        "    'prefix=NONE',\n"
        "    'rounding=HALF_UP',\n"
        "    `item=${item}`,\n"
        "  ];\n"
        "  if (display.startsWith('$')) {\n"
        "    throw new Error('UNEXPECTED_CURRENCY_PREFIX');\n"
        "  }\n"
        "  if (display === 'FREE') {\n"
        "    return `${item}: ZERO (${auditTags.length})`;\n"
        "  }\n"
        "  return `${item}: ${display}`;\n"
        "}\n"
    )
    js_receipt_src = (
        "import { formatLedgerAmount } from './currency';\n"
        "\n"
        "export function buildReceiptLine(name, cents) {\n"
        "  const rendered = formatLedgerAmount(cents);\n"
        "  if (rendered === 'FREE') {\n"
        "    return `${name}=0`;\n"
        "  }\n"
        "  return `${name}=${rendered}`;\n"
        "}\n"
    )
    ts_unrelated_src = (
        "export function computeTaxRate(region: string): number {\n"
        "  return region === 'OR' ? 0 : 0.08;\n"
        "}\n"
    )
    py_line = _find_line(py_report_src, "formatted = format_ledger_amount(")
    ts_line = _find_line(ts_cart_src, "const display = formatLedgerAmount(")
    js_line = _find_line(js_receipt_src, "const rendered = formatLedgerAmount(")
    return RetrievalCase(
        case_id="b03_multi_language",
        category="multi_language",
        description="Polyglot Python + TypeScript + JavaScript callers in one repo.",
        files={
            "py/__init__.py": "",
            "py/currency.py": py_currency_src,
            "py/report.py": py_report_src,
            "web/currency.ts": ts_currency_src,
            "web/cart.ts": ts_cart_src,
            "web/receipt.js": js_receipt_src,
            "web/unrelated.ts": ts_unrelated_src,
        },
        editable=("py/currency.py", "web/currency.ts"),
        candidates=(
            "py/report.py",
            "web/cart.ts",
            "web/receipt.js",
            "web/unrelated.ts",
        ),
        message=(
            "Update format_ledger_amount and formatLedgerAmount to preserve zero-amount "
            "and plain-decimal formatting across Python and web callers."
        ),
        target_symbol="formatLedgerAmount",
        expected_callers=(
            ExpectedCaller(
                rel_path="py/report.py",
                line=py_line,
                symbol="format_ledger_amount",
                kind="direct",
                contract_substrings=("formatted == 'FREE'",),
            ),
            ExpectedCaller(
                rel_path="web/cart.ts",
                line=ts_line,
                symbol="formatLedgerAmount",
                kind="direct",
                contract_substrings=("display = formatLedgerAmount",),
            ),
            ExpectedCaller(
                rel_path="web/receipt.js",
                line=js_line,
                symbol="formatLedgerAmount",
                kind="direct",
                contract_substrings=("rendered === 'FREE'",),
            ),
        ),
        wrong_target_paths=("web/unrelated.ts",),
        read_range_targets=(
            ReadRangeTarget(
                rel_path="web/cart.ts",
                call_line=ts_line,
                range_start=3,
                range_end=len(ts_cart_src.splitlines()),
                symbol="renderCartSummary",
                contract_substrings=(
                    "UNEXPECTED_CURRENCY_PREFIX",
                    "display === 'FREE'",
                ),
                description="TypeScript caller checks prefix ban after audit tags.",
            ),
        ),
    )


def _build_aliases_case() -> RetrievalCase:
    tokens_src = (
        "def issue_session_token(user_id):\n"
        "    return f'tok_{user_id:06d}_v1'\n"
    )
    login_src = (
        "from core.tokens import issue_session_token as mint_token\n"
        "\n"
        "\n"
        "def handle_login(user_id):\n"
        "    token = mint_token(user_id)\n"
        "    if not token.startswith('tok_'):\n"
        "        raise ValueError('BAD_TOKEN_PREFIX')\n"
        "    return token\n"
    )
    refresh_src = (
        "import core.tokens as tok_mod\n"
        "\n"
        "\n"
        "def handle_refresh(user_id):\n"
        "    token = tok_mod.issue_session_token(user_id)\n"
        "    if len(token) < 12:\n"
        "        raise ValueError('TOKEN_TOO_SHORT')\n"
        "    return token\n"
    )
    reexport_src = (
        "from core.tokens import issue_session_token as create_session_token\n"
        "\n"
        "__all__ = ['create_session_token']\n"
    )
    login_line = _find_line(login_src, "token = mint_token(")
    refresh_line = _find_line(refresh_src, "token = tok_mod.issue_session_token(")
    return RetrievalCase(
        case_id="b04_aliases",
        category="aliases",
        description="Import symbol alias (`as mint_token`) and module alias (`as tok_mod`).",
        files={
            "core/__init__.py": "",
            "core/tokens.py": tokens_src,
            "auth/__init__.py": "",
            "auth/login.py": login_src,
            "auth/refresh.py": refresh_src,
            "auth/reexport.py": reexport_src,
        },
        editable=("core/tokens.py",),
        candidates=("auth/login.py", "auth/refresh.py", "auth/reexport.py"),
        message="Update issue_session_token without breaking aliased login and refresh callers.",
        target_symbol="issue_session_token",
        expected_callers=(
            ExpectedCaller(
                rel_path="auth/login.py",
                line=login_line,
                symbol="issue_session_token",
                kind="alias",
                contract_substrings=("token.startswith('tok_')",),
            ),
            ExpectedCaller(
                rel_path="auth/refresh.py",
                line=refresh_line,
                symbol="issue_session_token",
                kind="alias",
                contract_substrings=("len(token) < 12",),
            ),
        ),
        expect_ambiguous=True,
    )


def _build_same_name_case() -> RetrievalCase:
    ledger_parser_src = (
        "def parse_record(raw_line):\n"
        "    account, cents = raw_line.split(':')\n"
        "    return account.strip(), int(cents)\n"
    )
    metrics_parser_src = (
        "def parse_record(payload):\n"
        "    metric, value = payload.split('=')\n"
        "    return {'metric': metric, 'value': float(value)}\n"
    )
    ledger_ingest_src = (
        "from ledger.parser import parse_record\n"
        "\n"
        "\n"
        "def ingest_ledger_entry(raw_line):\n"
        "    account, cents = parse_record(raw_line)\n"
        "    if cents < 0:\n"
        "        raise ValueError('NEGATIVE_LEDGER_ENTRY')\n"
        "    return (account, cents)\n"
    )
    metrics_collector_src = (
        "from metrics.parser import parse_record\n"
        "\n"
        "\n"
        "def collect_metric_event(payload):\n"
        "    parsed = parse_record(payload)\n"
        "    return parsed['metric'], parsed['value']\n"
    )
    ingest_line = _find_line(ledger_ingest_src, "account, cents = parse_record(")
    return RetrievalCase(
        case_id="b05_same_name",
        category="same_name",
        description="Two unrelated modules define parse_record; only ledger caller is relevant.",
        files={
            "ledger/__init__.py": "",
            "ledger/parser.py": ledger_parser_src,
            "ledger/ingest.py": ledger_ingest_src,
            "metrics/__init__.py": "",
            "metrics/parser.py": metrics_parser_src,
            "metrics/collector.py": metrics_collector_src,
        },
        editable=("ledger/parser.py",),
        candidates=(
            "ledger/ingest.py",
            "metrics/parser.py",
            "metrics/collector.py",
        ),
        message="Update parse_record in ledger/parser.py to validate account and cents.",
        target_symbol="parse_record",
        expected_callers=(
            ExpectedCaller(
                rel_path="ledger/ingest.py",
                line=ingest_line,
                symbol="parse_record",
                kind="direct",
                contract_substrings=("account, cents = parse_record",),
            ),
        ),
        wrong_target_paths=("metrics/collector.py", "metrics/parser.py"),
        expect_ambiguous=True,
    )


def _build_prod_vs_test_case() -> RetrievalCase:
    refund_src = (
        "def calculate_refund(amount_cents, days_elapsed):\n"
        "    if days_elapsed > 30:\n"
        "        return None\n"
        "    return amount_cents\n"
    )
    gateway_src = (
        "from payments.refund import calculate_refund\n"
        "\n"
        "\n"
        "def process_refund_request(amount_cents, days_elapsed):\n"
        "    refund_cents = calculate_refund(amount_cents, days_elapsed)\n"
        "    if refund_cents is None:\n"
        "        return 'REFUND_DENIED'\n"
        "    return f'REFUND:{refund_cents}'\n"
    )
    test_refund_src = (
        "from payments.refund import calculate_refund\n"
        "\n"
        "\n"
        "def test_calculate_refund_basic():\n"
        "    assert calculate_refund(500, 10) == 500\n"
    )
    smoke_refund_src = (
        "from payments.refund import calculate_refund\n"
        "\n"
        "assert calculate_refund(100, 1) == 100\n"
    )
    gateway_line = _find_line(gateway_src, "refund_cents = calculate_refund(")
    return RetrievalCase(
        case_id="b06_prod_vs_test",
        category="prod_vs_test",
        description="Production gateway caller must be prioritized over test/smoke callers.",
        files={
            "payments/__init__.py": "",
            "payments/refund.py": refund_src,
            "payments/gateway.py": gateway_src,
            "tests/test_refund.py": test_refund_src,
            "tests/smoke_refund.py": smoke_refund_src,
        },
        editable=("payments/refund.py",),
        candidates=(
            "payments/gateway.py",
            "tests/test_refund.py",
            "tests/smoke_refund.py",
        ),
        message="Update calculate_refund to enforce the 30-day window and None sentinel.",
        target_symbol="calculate_refund",
        expected_callers=(
            ExpectedCaller(
                rel_path="payments/gateway.py",
                line=gateway_line,
                symbol="calculate_refund",
                kind="direct",
                contract_substrings=("refund_cents is None",),
            ),
        ),
        wrong_target_paths=("tests/test_refund.py", "tests/smoke_refund.py"),
    )


def _build_transitive_case() -> RetrievalCase:
    hasher_src = (
        "import hashlib\n"
        "\n"
        "\n"
        "def compute_digest(payload):\n"
        "    raw = hashlib.sha256(payload.encode('utf-8')).hexdigest()\n"
        "    return f'sha256:{raw}'\n"
    )
    envelope_src = (
        "from crypto.hasher import compute_digest\n"
        "\n"
        "\n"
        "def sign_envelope(body):\n"
        "    digest = compute_digest(body)\n"
        "    return {'body': body, 'digest': digest}\n"
    )
    dispatch_src = (
        "from crypto.envelope import sign_envelope\n"
        "\n"
        "\n"
        "def dispatch_webhook(body):\n"
        "    envelope = sign_envelope(body)\n"
        "    if not envelope['digest'].startswith('sha256:'):\n"
        "        raise ValueError('UNEXPECTED_DIGEST_PREFIX')\n"
        "    return envelope\n"
    )
    envelope_line = _find_line(envelope_src, "digest = compute_digest(")
    dispatch_line = _find_line(dispatch_src, "envelope = sign_envelope(")
    return RetrievalCase(
        case_id="b07_transitive",
        category="transitive",
        description=(
            "Direct caller (sign_envelope) plus 2-hop transitive caller (dispatch_webhook)."
        ),
        files={
            "crypto/__init__.py": "",
            "crypto/hasher.py": hasher_src,
            "crypto/envelope.py": envelope_src,
            "api/__init__.py": "",
            "api/dispatch.py": dispatch_src,
        },
        editable=("crypto/hasher.py",),
        candidates=("crypto/envelope.py", "api/dispatch.py"),
        message="Update compute_digest while keeping envelope signing and webhook dispatch valid.",
        target_symbol="compute_digest",
        expected_callers=(
            ExpectedCaller(
                rel_path="crypto/envelope.py",
                line=envelope_line,
                symbol="compute_digest",
                kind="direct",
                contract_substrings=("digest = compute_digest",),
            ),
            ExpectedCaller(
                rel_path="api/dispatch.py",
                line=dispatch_line,
                symbol="compute_digest",
                kind="transitive",
                contract_substrings=("startswith('sha256:')",),
            ),
        ),
    )


def _build_large_file_source() -> str:
    """Build a deterministic >140 KiB Python file with a target caller past line 4000."""
    header = [
        "from catalog.codes import normalize_code\n",
        "\n",
        "_CATALOG_ROWS = (\n",
    ]
    padding = [
        f'    "catalog_row_{idx:04d}_padding_payload_0123456789",\n'
        for idx in range(4050)
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
        "    # Additional routing metadata lines to verify full-function ReadRange capture.\n",
        "    routing_trace = (\n"
        "        'pipeline=catalog_router_v3',\n",
        "        'size_bucket=over_140kib',\n",
        "        'validation=prefix_strict',\n",
        "        f'catalog_len={len(_CATALOG_ROWS)}',\n",
        "    )\n",
        "    if not code.startswith('CAT-'):\n",
        "        raise ValueError('INVALID_CATALOG_PREFIX')\n",
        "    return f'queue:{code}:{len(routing_trace)}'\n",
    ]
    return "".join(header + padding + footer)


def _build_large_file_case() -> RetrievalCase:
    codes_src = (
        "def normalize_code(raw):\n"
        "    cleaned = (raw or '').strip().upper()\n"
        "    if not cleaned:\n"
        "        return 'MISSING_CODE'\n"
        "    return cleaned if cleaned.startswith('CAT-') else f'CAT-{cleaned}'\n"
    )
    big_router_src = _build_large_file_source()
    call_line = _find_line(big_router_src, "code = normalize_code(raw)")
    func_start = _find_line(big_router_src, "def route_code(raw):")
    func_end = len(big_router_src.splitlines())
    return RetrievalCase(
        case_id="b08_large_file",
        category="large_file",
        description="Caller inside a >140 KiB file past line 4000 (exceeds 128 KiB builtin cap).",
        files={
            "catalog/__init__.py": "",
            "catalog/codes.py": codes_src,
            "catalog/big_router.py": big_router_src,
        },
        editable=("catalog/codes.py",),
        candidates=("catalog/big_router.py",),
        message="Update normalize_code to preserve catalog routing and sentinel handling.",
        target_symbol="normalize_code",
        expected_callers=(
            ExpectedCaller(
                rel_path="catalog/big_router.py",
                line=call_line,
                symbol="normalize_code",
                kind="direct",
                contract_substrings=("code == 'MISSING_CODE'",),
            ),
        ),
        read_range_targets=(
            ReadRangeTarget(
                rel_path="catalog/big_router.py",
                call_line=call_line,
                range_start=func_start,
                range_end=func_end,
                symbol="route_code",
                contract_substrings=(
                    "code == 'MISSING_CODE'",
                    "INVALID_CATALOG_PREFIX",
                    "queue:{code}",
                ),
                description="Streaming ReadRange past line 4000 in >140 KiB file.",
            ),
        ),
    )


def _build_multi_worktree_case() -> RetrievalCase:
    rates_src = (
        "def quote_parcel(weight_grams, zone):\n"
        "    if zone not in ('US', 'EU'):\n"
        "        return -1\n"
        "    return max(500, weight_grams * 2)\n"
    )
    checkout_src = (
        "from shipping.rates import quote_parcel\n"
        "\n"
        "\n"
        "def checkout_shipping(weight_grams, zone):\n"
        "    quote_cents = quote_parcel(weight_grams, zone)\n"
        "    if quote_cents < 0:\n"
        "        raise ValueError('UNSUPPORTED_ZONE')\n"
        "    return quote_cents\n"
    )
    sibling_rates_src = (
        "def quote_parcel(weight_grams, zone):\n"
        "    return weight_grams * 5\n"
    )
    sibling_caller_src = (
        "from shipping.rates import quote_parcel\n"
        "\n"
        "\n"
        "def sibling_worktree_quote(weight_grams):\n"
        "    return quote_parcel(weight_grams, 'APAC')\n"
    )
    checkout_line = _find_line(checkout_src, "quote_cents = quote_parcel(")
    return RetrievalCase(
        case_id="b09_multi_worktree",
        category="multi_worktree",
        description="Real sibling git worktree with same-named symbol verifies --no-federate.",
        files={
            "shipping/__init__.py": "",
            "shipping/rates.py": rates_src,
            "shipping/checkout.py": checkout_src,
        },
        editable=("shipping/rates.py",),
        candidates=("shipping/checkout.py",),
        message="Update quote_parcel for supported zones without leaking sibling worktrees.",
        target_symbol="quote_parcel",
        expected_callers=(
            ExpectedCaller(
                rel_path="shipping/checkout.py",
                line=checkout_line,
                symbol="quote_parcel",
                kind="direct",
                contract_substrings=("UNSUPPORTED_ZONE",),
            ),
        ),
        wrong_target_paths=("shipping/sibling_caller.py",),
        worktree_files={
            "shipping/rates.py": sibling_rates_src,
            "shipping/sibling_caller.py": sibling_caller_src,
        },
        worktree_branch="wt-sibling",
    )


def _build_negative_control_case() -> RetrievalCase:
    orphan_src = (
        "def legacy_checksum(data):\n"
        "    return sum(ord(ch) for ch in data) % 256\n"
    )
    active_src = (
        "def compute_crc(data):\n"
        "    # Previously replaced legacy_checksum with CRC-like accumulator.\n"
        "    label = 'legacy_checksum'\n"
        "    return len(data) + len(label)\n"
    )
    docs_src = (
        '"""Migration notes mentioning legacy_checksum without invoking it."""\n'
        "\n"
        "DEPRECATED_NAMES = ('legacy_checksum',)\n"
    )
    return RetrievalCase(
        case_id="b10_negative_control",
        category="negative_control",
        description="Zero callers anywhere; only comments and string literals mention symbol.",
        files={
            "utils/__init__.py": "",
            "utils/orphan.py": orphan_src,
            "utils/active.py": active_src,
            "utils/docs_comments.py": docs_src,
        },
        editable=("utils/orphan.py",),
        candidates=("utils/active.py", "utils/docs_comments.py"),
        message="Refactor legacy_checksum safely after checking whether any callers remain.",
        target_symbol="legacy_checksum",
        expected_callers=(),
        wrong_target_paths=("utils/active.py", "utils/docs_comments.py"),
    )


def get_retrieval_corpus() -> tuple[RetrievalCase, ...]:
    """Return the complete deterministic Section B retrieval fixture corpus."""
    return (
        _build_single_file_case(),
        _build_multi_file_case(),
        _build_multi_language_case(),
        _build_aliases_case(),
        _build_same_name_case(),
        _build_prod_vs_test_case(),
        _build_transitive_case(),
        _build_large_file_case(),
        _build_multi_worktree_case(),
        _build_negative_control_case(),
    )


def get_cases_by_category(category: str) -> tuple[RetrievalCase, ...]:
    """Return all corpus cases matching ``category``."""
    return tuple(case for case in get_retrieval_corpus() if case.category == category)


def validate_corpus(cases: Sequence[RetrievalCase] | None = None) -> list[str]:
    """Validate corpus completeness, ground-truth coordinates, and invariants.

    Returns a list of error messages (empty list means the corpus is valid).
    """
    corpus = tuple(cases) if cases is not None else get_retrieval_corpus()
    errors: list[str] = []

    categories_seen = {case.category for case in corpus}
    missing_categories = [cat for cat in SECTION_B_CATEGORIES if cat not in categories_seen]
    if missing_categories:
        errors.append(f"Missing Section B categories: {', '.join(missing_categories)}")

    seen_ids: set[str] = set()
    for case in corpus:
        if case.case_id in seen_ids:
            errors.append(f"Duplicate case_id: {case.case_id}")
        seen_ids.add(case.case_id)

        if case.corpus_version != CORPUS_VERSION:
            errors.append(
                f"{case.case_id}: corpus_version {case.corpus_version!r} != {CORPUS_VERSION!r}"
            )

        for rel in (*case.editable, *case.candidates):
            if rel not in case.files:
                errors.append(f"{case.case_id}: path {rel!r} missing from files")

        for caller in case.expected_callers:
            if caller.rel_path not in case.files:
                errors.append(
                    f"{case.case_id}: expected caller path {caller.rel_path!r} missing"
                )
                continue
            file_lines = case.files[caller.rel_path].splitlines()
            if caller.line < 1 or caller.line > len(file_lines):
                errors.append(
                    f"{case.case_id}: expected caller line {caller.line} out of range "
                    f"for {caller.rel_path} (1..{len(file_lines)})"
                )
            for substr in caller.contract_substrings:
                if substr not in case.files[caller.rel_path]:
                    errors.append(
                        f"{case.case_id}: contract substring {substr!r} missing in "
                        f"{caller.rel_path}"
                    )

        for target in case.read_range_targets:
            if target.rel_path not in case.files:
                errors.append(
                    f"{case.case_id}: read_range target {target.rel_path!r} missing"
                )
                continue
            file_lines = case.files[target.rel_path].splitlines()
            if (
                target.range_start < 1
                or target.range_end < target.range_start
                or target.range_end > len(file_lines)
            ):
                errors.append(
                    f"{case.case_id}: invalid read_range {target.range_start}..{target.range_end} "
                    f"for {target.rel_path} (1..{len(file_lines)})"
                )
            else:
                slice_text = "\n".join(
                    file_lines[target.range_start - 1 : target.range_end]
                )
                for substr in target.contract_substrings:
                    if substr not in slice_text:
                        errors.append(
                            f"{case.case_id}: read_range substring {substr!r} not in "
                            f"{target.rel_path}:{target.range_start}-{target.range_end}"
                        )

        if case.category == "large_file":
            large_paths = [
                rel
                for rel, src in case.files.items()
                if len(src.encode("utf-8")) > LARGE_FILE_MIN_BYTES
            ]
            if not large_paths:
                errors.append(
                    f"{case.case_id}: large_file requires a file > {LARGE_FILE_MIN_BYTES} bytes"
                )
            if not any(c.line > LARGE_FILE_MIN_LINE for c in case.expected_callers):
                errors.append(
                    f"{case.case_id}: large_file requires an expected caller past "
                    f"line {LARGE_FILE_MIN_LINE}"
                )

        if case.category == "multi_worktree":
            if not case.worktree_files or not case.worktree_branch:
                errors.append(
                    f"{case.case_id}: multi_worktree requires worktree_files and "
                    "worktree_branch"
                )

        if case.category == "negative_control":
            if case.expected_callers:
                errors.append(
                    f"{case.case_id}: negative_control must have zero expected callers"
                )

    return errors


def _run_git(cwd: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update(_GIT_ENV_OVERRIDES)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd} ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@contextlib.contextmanager
def materialize_case(
    case: RetrievalCase,
    *,
    base_dir: Path | None = None,
) -> Iterator[MaterializedCase]:
    """Materialize ``case`` into a temporary git repo (and optional sibling git worktree).

    Guarantees clean removal of any created git worktrees and temporary directories
    when the context exits.
    """
    parent = str(base_dir) if base_dir is not None else None
    tmp_root = Path(tempfile.mkdtemp(prefix=f"aider_retbench_{case.case_id}_", dir=parent))
    repo_root = tmp_root / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    created_worktrees: list[Path] = []
    try:
        _run_git(repo_root, "init", "-q")
        _run_git(repo_root, "config", "user.name", _GIT_ENV_OVERRIDES["GIT_AUTHOR_NAME"])
        _run_git(repo_root, "config", "user.email", _GIT_ENV_OVERRIDES["GIT_AUTHOR_EMAIL"])
        _write_tree(repo_root, case.files)
        _run_git(repo_root, "add", ".")
        _run_git(repo_root, "commit", "-q", "-m", f"fixture {case.case_id} v{case.corpus_version}")

        if case.worktree_files:
            branch = case.worktree_branch or "wt-sibling"
            wt_dir = tmp_root / "worktree_sibling"
            _run_git(repo_root, "worktree", "add", "-q", "-b", branch, str(wt_dir))
            created_worktrees.append(wt_dir)
            _write_tree(wt_dir, case.worktree_files)
            _run_git(wt_dir, "add", ".")
            _run_git(wt_dir, "commit", "-q", "-m", f"sibling worktree {case.case_id}")

        yield MaterializedCase(
            case=case,
            base_dir=tmp_root,
            repo_root=repo_root.resolve(),
            worktree_roots=tuple(wt.resolve() for wt in created_worktrees),
        )
    finally:
        for wt_dir in reversed(created_worktrees):
            try:
                _run_git(repo_root, "worktree", "remove", "--force", str(wt_dir))
            except RuntimeError:
                pass
        if created_worktrees:
            try:
                _run_git(repo_root, "worktree", "prune")
            except RuntimeError:
                pass
        shutil.rmtree(tmp_root, ignore_errors=True)
