#!/usr/bin/env python3
"""Deterministic no-LLM retrieval benchmark harness for Issue #5 Section B.

Evaluates:
1. Per-category caller retrieval quality comparing ``builtin`` vs ``cymbal`` (v0.18.0)
   across all 10 Section B categories without collapsing heterogeneous categories into
   a single opaque average (Oracle ``beir-guide`` ``90905b05c622807ee5f8a78d``;
   ``ir-textbook`` p. 198 ``5ecfb939602f76b7331773f5``).
2. ``ReadRange`` vs existing 700-char caller excerpts (``aider.local_callers``) vs
   ``cymbal show`` on returned tokens, bytes scanned/returned, contract-critical
   condition coverage, and silent truncation detection.
3. Cold index build, warm query latency (p50/p95 ms over ``--warm-runs``), and
   incremental single-file update latency + index size + CPU/RSS + files reparsed
   on both a small fixture repo and a realistic large repo (isolated via a temporary
   git worktree/copy so the target repo is never dirtied).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aider.cymbal_callers import (  # noqa: E402
    BACKEND_BUILTIN,
    BACKEND_CYMBAL,
    PINNED_CYMBAL_VERSION,
    CallerLookupResult,
    CymbalCommandRunner,
    CymbalConfig,
    CymbalError,
    CymbalEvidenceReport,
    SubprocessCymbalRunner,
    build_caller_finder,
)
from aider.local_callers import (  # noqa: E402
    MAX_SOURCE_BYTES,
    find_related_callers,
)
from aider.read_range import (  # noqa: E402
    CymbalShowError,
    RangeReadMetrics,
    ReadRangeError,
    estimate_tokens,
    measure_read_range,
    read_range,
    validate_cymbal_show,
)

try:  # noqa: E402
    from scripts.local_context_trial.retrieval_fixtures import (
        CORPUS_VERSION,
        SECTION_B_CATEGORIES,
        ExpectedCaller,
        MaterializedCase,
        ReadRangeTarget,
        RetrievalCase,
        get_retrieval_corpus,
        materialize_case,
        validate_corpus,
    )
except ImportError:  # pragma: no cover - direct script invocation fallback
    from retrieval_fixtures import (  # type: ignore[no-redef]
        CORPUS_VERSION,
        SECTION_B_CATEGORIES,
        ExpectedCaller,
        MaterializedCase,
        ReadRangeTarget,
        RetrievalCase,
        get_retrieval_corpus,
        materialize_case,
        validate_corpus,
    )

VALID_BENCHMARK_BACKENDS = (BACKEND_BUILTIN, BACKEND_CYMBAL)

_INDEX_STATS_RE = re.compile(r"(\d+)\s+indexed\b.*?(\d+)\s+unchanged\b", re.IGNORECASE)
_INDEX_ONLY_RE = re.compile(r"(\d+)\s+indexed\b", re.IGNORECASE)


@dataclass(frozen=True)
class CaseRetrievalMetrics:
    """Per-case deterministic retrieval metrics for one backend."""

    case_id: str
    category: str
    backend: str
    status: str
    precision: float
    recall: float
    r_precision: float
    wrong_target_rate: float
    ambiguous_rate: float
    no_evidence_rate: float
    source_location_accuracy: float
    snippet_contract_coverage: float
    returned_tokens: int
    expected_count: int
    retrieved_count: int
    true_positives: int
    false_positives: int
    explicit_wrong_target_hits: int
    exact_line_matches: int
    retrieved_locations: tuple[str, ...]
    ambiguity_notes: tuple[str, ...]
    truncation_notes: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class CategorySummary:
    """Per-category summary for one backend (never collapsed across categories)."""

    category: str
    backend: str
    case_count: int
    precision: float
    recall: float
    r_precision: float
    wrong_target_rate: float
    ambiguous_rate: float
    no_evidence_rate: float
    source_location_accuracy: float
    snippet_contract_coverage: float
    returned_tokens: float


@dataclass(frozen=True)
class CategoryDelta:
    """Per-category pairwise delta (cymbal minus builtin)."""

    category: str
    precision_delta: float
    recall_delta: float
    r_precision_delta: float
    wrong_target_rate_delta: float
    ambiguous_rate_delta: float
    no_evidence_rate_delta: float
    source_location_accuracy_delta: float
    snippet_contract_coverage_delta: float
    returned_tokens_delta: float


@dataclass(frozen=True)
class SliceMethodMetrics:
    """Metrics for one slice-reading method on a single ReadRange target."""

    method: str
    available: bool
    returned_tokens: int
    bytes_scanned: int
    bytes_returned: int
    file_size_bytes: int
    net_tokens_saved: int
    contract_substrings_total: int
    contract_substrings_matched: int
    contract_coverage: float
    explicitly_truncated: bool
    silent_truncation: bool
    missing_substrings: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class ReadRangeComparison:
    """Side-by-side comparison of ReadRange vs 700-char excerpt vs cymbal show."""

    case_id: str
    category: str
    rel_path: str
    symbol: str
    range_start: int
    range_end: int
    methods: dict[str, SliceMethodMetrics]


@dataclass(frozen=True)
class PhasePerfMetrics:
    """Latency, resource, and index-size telemetry for cold index or incremental update."""

    wall_ms: float
    cpu_user_ms: float
    cpu_sys_ms: float
    max_rss_bytes: int
    index_size_bytes: int
    files_indexed: int
    files_reparsed: int
    files_unchanged: int
    note: str = ""


@dataclass(frozen=True)
class WarmQueryPerfMetrics:
    """Latency distribution and resource telemetry over repeated warm queries."""

    runs: int
    p50_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    mean_ms: float
    cpu_user_ms: float
    cpu_sys_ms: float
    max_rss_bytes: int
    samples_ms: tuple[float, ...]


@dataclass(frozen=True)
class RepoPerfBenchmark:
    """Cold index, warm query, and incremental update benchmark for one repo & backend."""

    repo_label: str
    repo_file_count: int
    backend: str
    target_symbol: str
    cold_index: PhasePerfMetrics
    warm_query: WarmQueryPerfMetrics
    incremental_update: PhasePerfMetrics
    error: str = ""


@dataclass(frozen=True)
class BenchmarkReport:
    """Complete structured Section B retrieval benchmark report."""

    corpus_version: str
    pinned_cymbal_version: str
    backends: tuple[str, ...]
    warm_runs: int
    case_metrics: tuple[CaseRetrievalMetrics, ...]
    category_summaries: dict[str, dict[str, CategorySummary]]
    category_deltas: dict[str, CategoryDelta]
    read_range_comparisons: tuple[ReadRangeComparison, ...]
    performance: tuple[RepoPerfBenchmark, ...]


def compute_percentile(samples: Sequence[float], pct: float) -> float:
    """Compute linear-interpolated percentile ``pct`` in ``[0, 100]`` over ``samples``."""
    if not samples:
        return 0.0
    ordered = sorted(float(x) for x in samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    frac = rank - low
    return ordered[low] + frac * (ordered[high] - ordered[low])


def _rss_to_bytes(raw_maxrss: int) -> int:
    """Normalize ``resource.getrusage(...).ru_maxrss`` to bytes across macOS and Linux."""
    if raw_maxrss <= 0:
        return 0
    if sys.platform == "darwin":
        return int(raw_maxrss)
    return int(raw_maxrss) * 1024


def _snapshot_rusage(who: int) -> tuple[float, float, int]:
    usage = resource.getrusage(who)
    return (
        float(usage.ru_utime) * 1000.0,
        float(usage.ru_stime) * 1000.0,
        _rss_to_bytes(int(usage.ru_maxrss)),
    )


def _norm_posix(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def compute_case_metrics(
    case: RetrievalCase,
    result: CallerLookupResult,
    *,
    backend: str,
) -> CaseRetrievalMetrics:
    """Compute exact per-case IR and contract metrics from a ``CallerLookupResult``.

    R-precision follows Manning et al., *Introduction to Information Retrieval*,
    p. 198 (Oracle passage ``5ecfb939602f76b7331773f5``): when ``|Rel| > 0``,
    evaluate precision over the top ``|Rel|`` retrieved results.
    """
    relevant_by_path: dict[str, list[ExpectedCaller]] = {}
    for expected in case.expected_callers:
        norm = _norm_posix(expected.rel_path)
        relevant_by_path.setdefault(norm, []).append(expected)
    relevant_paths = set(relevant_by_path.keys())
    wrong_paths = {_norm_posix(p) for p in case.wrong_target_paths}

    retrieved_order: list[str] = []
    excerpts_by_path: dict[str, list[Any]] = {}
    retrieved_locations: list[str] = []
    for excerpt in result.excerpts:
        norm = _norm_posix(excerpt.path)
        excerpts_by_path.setdefault(norm, []).append(excerpt)
        retrieved_locations.append(f"{norm}:{excerpt.line}")
        if norm not in retrieved_order:
            retrieved_order.append(norm)

    tp_paths = [p for p in retrieved_order if p in relevant_paths]
    fp_paths = [p for p in retrieved_order if p not in relevant_paths]
    explicit_wrong_hits = sum(1 for p in retrieved_order if p in wrong_paths)

    tp = len(tp_paths)
    fp = len(fp_paths)
    rel_count = len(relevant_paths)

    if rel_count > 0:
        precision = (tp / len(retrieved_order)) if retrieved_order else 0.0
        recall = tp / rel_count
        top_r = retrieved_order[:rel_count]
        r_precision = sum(1 for p in top_r if p in relevant_paths) / rel_count
        wrong_target_rate = (fp / len(retrieved_order)) if retrieved_order else 0.0
    else:
        precision = 1.0 if not retrieved_order else 0.0
        recall = 1.0 if not retrieved_order else 0.0
        r_precision = 1.0 if not retrieved_order else 0.0
        wrong_target_rate = 0.0 if not retrieved_order else 1.0

    has_ambiguity = bool(result.ambiguity) or any(
        bool(getattr(e, "ambiguity", ())) for e in result.excerpts
    )
    ambiguous_rate = 1.0 if has_ambiguity else 0.0
    no_evidence_rate = 1.0 if (result.status == "NO_EVIDENCE" or not result.excerpts) else 0.0

    exact_line_matches = 0
    for path in tp_paths:
        expected_lines = {c.line for c in relevant_by_path[path]}
        if any(e.line in expected_lines for e in excerpts_by_path.get(path, ())):
            exact_line_matches += 1

    if rel_count == 0:
        source_location_accuracy = 1.0 if not retrieved_order else 0.0
    elif tp == 0:
        source_location_accuracy = 0.0
    else:
        source_location_accuracy = exact_line_matches / tp

    total_subs = 0
    matched_subs = 0
    for expected in case.expected_callers:
        norm = _norm_posix(expected.rel_path)
        combined_code = "\n".join(e.code for e in excerpts_by_path.get(norm, ()))
        for sub in expected.contract_substrings:
            total_subs += 1
            if sub in combined_code:
                matched_subs += 1

    if total_subs == 0:
        snippet_contract_coverage = 1.0 if not retrieved_order else 0.0
    else:
        snippet_contract_coverage = matched_subs / total_subs

    returned_tokens = sum(estimate_tokens(e.format()) for e in result.excerpts)

    return CaseRetrievalMetrics(
        case_id=case.case_id,
        category=case.category,
        backend=backend,
        status=result.status,
        precision=round(precision, 6),
        recall=round(recall, 6),
        r_precision=round(r_precision, 6),
        wrong_target_rate=round(wrong_target_rate, 6),
        ambiguous_rate=round(ambiguous_rate, 6),
        no_evidence_rate=round(no_evidence_rate, 6),
        source_location_accuracy=round(source_location_accuracy, 6),
        snippet_contract_coverage=round(snippet_contract_coverage, 6),
        returned_tokens=returned_tokens,
        expected_count=rel_count,
        retrieved_count=len(retrieved_order),
        true_positives=tp,
        false_positives=fp,
        explicit_wrong_target_hits=explicit_wrong_hits,
        exact_line_matches=exact_line_matches,
        retrieved_locations=tuple(retrieved_locations),
        ambiguity_notes=tuple(result.ambiguity),
        truncation_notes=tuple(result.truncation),
    )


def summarize_by_category(
    case_metrics: Sequence[CaseRetrievalMetrics],
) -> dict[str, dict[str, CategorySummary]]:
    """Aggregate case metrics strictly within each (category, backend) bucket.

    Never collapses heterogeneous categories into a single overall average
    (Oracle ``beir-guide`` ``90905b05c622807ee5f8a78d``).
    """
    grouped: dict[str, dict[str, list[CaseRetrievalMetrics]]] = {}
    for item in case_metrics:
        grouped.setdefault(item.category, {}).setdefault(item.backend, []).append(item)

    ordered_categories = list(SECTION_B_CATEGORIES) + [
        cat for cat in grouped if cat not in SECTION_B_CATEGORIES
    ]
    summaries: dict[str, dict[str, CategorySummary]] = {}
    for category in ordered_categories:
        by_backend = grouped.get(category)
        if not by_backend:
            continue
        summaries[category] = {}
        for backend, items in sorted(by_backend.items()):
            n = len(items)
            summaries[category][backend] = CategorySummary(
                category=category,
                backend=backend,
                case_count=n,
                precision=round(sum(m.precision for m in items) / n, 6),
                recall=round(sum(m.recall for m in items) / n, 6),
                r_precision=round(sum(m.r_precision for m in items) / n, 6),
                wrong_target_rate=round(sum(m.wrong_target_rate for m in items) / n, 6),
                ambiguous_rate=round(sum(m.ambiguous_rate for m in items) / n, 6),
                no_evidence_rate=round(sum(m.no_evidence_rate for m in items) / n, 6),
                source_location_accuracy=round(
                    sum(m.source_location_accuracy for m in items) / n, 6
                ),
                snippet_contract_coverage=round(
                    sum(m.snippet_contract_coverage for m in items) / n, 6
                ),
                returned_tokens=round(sum(m.returned_tokens for m in items) / n, 2),
            )
    return summaries


def compute_category_deltas(
    summaries: dict[str, dict[str, CategorySummary]],
) -> dict[str, CategoryDelta]:
    """Compute per-category pairwise differences (cymbal minus builtin)."""
    deltas: dict[str, CategoryDelta] = {}
    for category, by_backend in summaries.items():
        if BACKEND_BUILTIN in by_backend and BACKEND_CYMBAL in by_backend:
            b = by_backend[BACKEND_BUILTIN]
            c = by_backend[BACKEND_CYMBAL]
            deltas[category] = CategoryDelta(
                category=category,
                precision_delta=round(c.precision - b.precision, 6),
                recall_delta=round(c.recall - b.recall, 6),
                r_precision_delta=round(c.r_precision - b.r_precision, 6),
                wrong_target_rate_delta=round(c.wrong_target_rate - b.wrong_target_rate, 6),
                ambiguous_rate_delta=round(c.ambiguous_rate - b.ambiguous_rate, 6),
                no_evidence_rate_delta=round(c.no_evidence_rate - b.no_evidence_rate, 6),
                source_location_accuracy_delta=round(
                    c.source_location_accuracy - b.source_location_accuracy, 6
                ),
                snippet_contract_coverage_delta=round(
                    c.snippet_contract_coverage - b.snippet_contract_coverage, 6
                ),
                returned_tokens_delta=round(c.returned_tokens - b.returned_tokens, 2),
            )
    return deltas


def _parse_cymbal_index_stats(stdout: bytes, stderr: bytes) -> tuple[int, int, int | None]:
    """Parse ``(files_reparsed, files_unchanged, optional_index_size_bytes)``."""
    out_text = stdout.decode("utf-8", errors="replace").strip()
    if out_text.startswith("{"):
        try:
            payload = json.loads(out_text)
            if isinstance(payload, dict):
                raw_results = payload.get("results")
                body = raw_results if isinstance(raw_results, dict) else payload
                reparsed = int(
                    body.get("files_reparsed", body.get("indexed", body.get("files_indexed", 0)))
                )
                unchanged = int(body.get("files_unchanged", body.get("unchanged", 0)))
                raw_size = body.get("index_size_bytes")
                size_bytes = int(raw_size) if isinstance(raw_size, int) else None
                return reparsed, unchanged, size_bytes
        except (ValueError, TypeError):
            pass

    combined = out_text + "\n" + stderr.decode("utf-8", errors="replace")
    match = _INDEX_STATS_RE.search(combined)
    if match:
        return int(match.group(1)), int(match.group(2)), None
    only = _INDEX_ONLY_RE.search(combined)
    if only:
        return int(only.group(1)), 0, None
    return 0, 0, None


def _get_cymbal_index_size_and_path(
    repo_root: Path,
    runner: CymbalCommandRunner,
    config: CymbalConfig,
) -> tuple[int, Path | None]:
    """Query ``cymbal ls --repos --json --no-federate`` to determine index DB size."""
    try:
        res = runner.run(
            (config.executable, "ls", "--repos", "--json", "--no-federate"),
            cwd=repo_root,
            timeout_seconds=config.timeout_seconds,
            max_output_bytes=config.max_output_bytes,
        )
    except CymbalError:
        return 0, None
    if res.returncode != 0 or not res.stdout.strip():
        return 0, None
    try:
        payload = json.loads(res.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return 0, None

    entries: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        entries = [e for e in payload["results"] if isinstance(e, dict)]
    elif isinstance(payload, list):
        entries = [e for e in payload if isinstance(e, dict)]

    resolved_target = repo_root.resolve()
    for entry in entries:
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            continue
        try:
            entry_path = Path(raw_path).resolve()
        except OSError:
            continue
        if entry_path != resolved_target:
            continue
        if isinstance(entry.get("db_size_bytes"), int):
            return int(entry["db_size_bytes"]), None
        raw_db = entry.get("db_path")
        if isinstance(raw_db, str) and raw_db:
            db_file = Path(raw_db)
            total = 0
            for suffix in ("", "-wal", "-shm"):
                cand = Path(f"{raw_db}{suffix}")
                try:
                    if cand.is_file():
                        total += cand.stat().st_size
                except OSError:
                    pass
            return total, db_file
    return 0, None


def _cleanup_cymbal_db(db_path: Path | None) -> None:
    """Remove temporary benchmark cymbal DB directory if it is inside a cymbal cache."""
    if db_path is None:
        return
    try:
        resolved = db_path.resolve()
        parent = resolved.parent
        parts_lower = [part.lower() for part in parent.parts]
        if "cymbal" in parts_lower and "repos" in parts_lower:
            shutil.rmtree(parent, ignore_errors=True)
    except OSError:
        pass


def _run_cymbal_index(
    repo_root: Path,
    runner: CymbalCommandRunner,
    config: CymbalConfig,
    *,
    force: bool = False,
) -> tuple[PhasePerfMetrics, Path | None]:
    argv: list[str] = [config.executable, "index", "--no-federate"]
    if force:
        argv.append("--force")

    u0, s0, _ = _snapshot_rusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    res = runner.run(
        tuple(argv),
        cwd=repo_root,
        timeout_seconds=max(config.timeout_seconds, 30.0),
        max_output_bytes=config.max_output_bytes,
    )
    wall_ms = (time.perf_counter() - t0) * 1000.0
    u1, s1, rss1 = _snapshot_rusage(resource.RUSAGE_CHILDREN)

    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"cymbal index failed ({res.returncode}): {err}")

    reparsed, unchanged, parsed_size = _parse_cymbal_index_stats(res.stdout, res.stderr)
    db_size, db_path = _get_cymbal_index_size_and_path(repo_root, runner, config)
    effective_size = parsed_size if parsed_size is not None else db_size
    return (
        PhasePerfMetrics(
            wall_ms=round(wall_ms, 3),
            cpu_user_ms=round(max(0.0, u1 - u0), 3),
            cpu_sys_ms=round(max(0.0, s1 - s0), 3),
            max_rss_bytes=rss1,
            index_size_bytes=effective_size,
            files_indexed=reparsed + unchanged,
            files_reparsed=reparsed,
            files_unchanged=unchanged,
        ),
        db_path,
    )


def evaluate_read_range_target(
    repo_root: Path,
    case: RetrievalCase,
    target: ReadRangeTarget,
    *,
    include_cymbal_show: bool,
    cymbal_runner: CymbalCommandRunner | None = None,
    cymbal_config: CymbalConfig | None = None,
) -> ReadRangeComparison:
    """Compare ``read_range`` vs 700-char ``local_callers`` vs ``cymbal show``."""
    methods: dict[str, SliceMethodMetrics] = {}
    target_file = repo_root / target.rel_path
    file_size_bytes = target_file.stat().st_size if target_file.is_file() else 0
    full_file_tokens = estimate_tokens("x" * file_size_bytes) if file_size_bytes else 0

    # 1. Native streaming ReadRange
    try:
        rr_excerpt = read_range(
            repo_root,
            target.rel_path,
            target.range_start,
            target.range_end,
            symbol=target.symbol,
        )
        rr_metrics: RangeReadMetrics = measure_read_range(rr_excerpt)
        missing = tuple(
            sub for sub in target.contract_substrings if sub not in rr_excerpt.code
        )
        total_subs = len(target.contract_substrings)
        matched_subs = total_subs - len(missing)
        coverage = (matched_subs / total_subs) if total_subs else 1.0
        methods["read_range"] = SliceMethodMetrics(
            method="read_range",
            available=True,
            returned_tokens=rr_metrics.returned_tokens,
            bytes_scanned=rr_metrics.bytes_scanned,
            bytes_returned=rr_metrics.bytes_returned,
            file_size_bytes=rr_metrics.file_size_bytes,
            net_tokens_saved=rr_metrics.net_tokens_saved,
            contract_substrings_total=total_subs,
            contract_substrings_matched=matched_subs,
            contract_coverage=round(coverage, 6),
            explicitly_truncated=rr_excerpt.truncated,
            silent_truncation=bool(missing and not rr_excerpt.truncated),
            missing_substrings=missing,
        )
    except ReadRangeError as exc:
        methods["read_range"] = SliceMethodMetrics(
            method="read_range",
            available=False,
            returned_tokens=0,
            bytes_scanned=0,
            bytes_returned=0,
            file_size_bytes=file_size_bytes,
            net_tokens_saved=0,
            contract_substrings_total=len(target.contract_substrings),
            contract_substrings_matched=0,
            contract_coverage=0.0,
            explicitly_truncated=False,
            silent_truncation=True,
            missing_substrings=target.contract_substrings,
            error=str(exc),
        )

    # 2. Existing 700-char caller excerpts (aider.local_callers)
    excerpts = find_related_callers(
        repo_root,
        case.message,
        case.editable,
        case.candidates,
        max_files=max(3, len(case.candidates)),
        max_chars=700,
        context_lines=3,
    )
    norm_target = _norm_posix(target.rel_path)
    matched_excerpts = [e for e in excerpts if _norm_posix(e.path) == norm_target]
    lc_scanned = min(file_size_bytes, MAX_SOURCE_BYTES + 1)
    if matched_excerpts:
        lc_code = "\n".join(e.code for e in matched_excerpts)
        lc_formatted = "\n\n".join(e.format() for e in matched_excerpts)
        lc_bytes_returned = len(lc_code.encode("utf-8"))
        lc_tokens = estimate_tokens(lc_formatted)
        lc_explicit_trunc = any(
            bool(e.truncation) or any("truncat" in lim.lower() for lim in e.limitations)
            for e in matched_excerpts
        )
    else:
        lc_code = ""
        lc_bytes_returned = 0
        lc_tokens = 0
        lc_explicit_trunc = False

    lc_missing = tuple(sub for sub in target.contract_substrings if sub not in lc_code)
    total_subs = len(target.contract_substrings)
    lc_matched = total_subs - len(lc_missing)
    lc_cov = (lc_matched / total_subs) if total_subs else 1.0
    methods["local_callers_700"] = SliceMethodMetrics(
        method="local_callers_700",
        available=True,
        returned_tokens=lc_tokens,
        bytes_scanned=lc_scanned,
        bytes_returned=lc_bytes_returned,
        file_size_bytes=file_size_bytes,
        net_tokens_saved=full_file_tokens - lc_tokens,
        contract_substrings_total=total_subs,
        contract_substrings_matched=lc_matched,
        contract_coverage=round(lc_cov, 6),
        explicitly_truncated=lc_explicit_trunc,
        silent_truncation=bool(lc_missing and not lc_explicit_trunc),
        missing_substrings=lc_missing,
    )

    # 3. Validated cymbal show
    if include_cymbal_show:
        cfg = cymbal_config or CymbalConfig()
        runner = cymbal_runner or SubprocessCymbalRunner(max_stderr_bytes=cfg.max_stderr_bytes)
        show_spec = f"{target.rel_path}:{target.range_start}-{target.range_end}"
        try:
            cmd_res = runner.run(
                (cfg.executable, "show", "--json", "--no-federate", show_spec),
                cwd=repo_root,
                timeout_seconds=cfg.timeout_seconds,
                max_output_bytes=cfg.max_output_bytes,
            )
            if cmd_res.returncode != 0:
                raise CymbalShowError(
                    cmd_res.stderr.decode("utf-8", errors="replace").strip()
                    or f"cymbal show exited {cmd_res.returncode}"
                )
            payload_text = cmd_res.stdout.decode("utf-8", errors="replace")
            validated = validate_cymbal_show(repo_root, payload_text)
            cs_metrics = measure_read_range(validated)
            cs_missing = tuple(
                sub for sub in target.contract_substrings if sub not in validated.code
            )
            cs_matched = total_subs - len(cs_missing)
            cs_cov = (cs_matched / total_subs) if total_subs else 1.0
            methods["cymbal_show"] = SliceMethodMetrics(
                method="cymbal_show",
                available=True,
                returned_tokens=cs_metrics.returned_tokens,
                bytes_scanned=cs_metrics.bytes_scanned,
                bytes_returned=cs_metrics.bytes_returned,
                file_size_bytes=cs_metrics.file_size_bytes,
                net_tokens_saved=cs_metrics.net_tokens_saved,
                contract_substrings_total=total_subs,
                contract_substrings_matched=cs_matched,
                contract_coverage=round(cs_cov, 6),
                explicitly_truncated=validated.truncated,
                silent_truncation=bool(cs_missing and not validated.truncated),
                missing_substrings=cs_missing,
            )
        except (CymbalError, CymbalShowError, ReadRangeError, OSError) as exc:
            methods["cymbal_show"] = SliceMethodMetrics(
                method="cymbal_show",
                available=False,
                returned_tokens=0,
                bytes_scanned=0,
                bytes_returned=0,
                file_size_bytes=file_size_bytes,
                net_tokens_saved=0,
                contract_substrings_total=total_subs,
                contract_substrings_matched=0,
                contract_coverage=0.0,
                explicitly_truncated=False,
                silent_truncation=False,
                missing_substrings=target.contract_substrings,
                error=str(exc),
            )
    else:
        methods["cymbal_show"] = SliceMethodMetrics(
            method="cymbal_show",
            available=False,
            returned_tokens=0,
            bytes_scanned=0,
            bytes_returned=0,
            file_size_bytes=file_size_bytes,
            net_tokens_saved=0,
            contract_substrings_total=total_subs,
            contract_substrings_matched=0,
            contract_coverage=0.0,
            explicitly_truncated=False,
            silent_truncation=False,
            missing_substrings=(),
            error="cymbal backend not selected",
        )

    return ReadRangeComparison(
        case_id=case.case_id,
        category=case.category,
        rel_path=target.rel_path,
        symbol=target.symbol,
        range_start=target.range_start,
        range_end=target.range_end,
        methods=methods,
    )


def _evaluate_case_for_backend(
    mat: MaterializedCase,
    *,
    backend: str,
    cymbal_runner: CymbalCommandRunner | None = None,
    cymbal_config: CymbalConfig | None = None,
) -> CaseRetrievalMetrics:
    case = mat.case
    cfg = cymbal_config or CymbalConfig()
    finder = build_caller_finder(
        backend=backend,  # type: ignore[arg-type]
        cymbal_config=cfg,
        runner=cymbal_runner,
    )
    report = CymbalEvidenceReport(backend_requested=backend)  # type: ignore[arg-type]
    max_files = max(3, len(case.expected_callers))
    try:
        result = finder.lookup(
            mat.repo_root,
            case.message,
            case.editable,
            case.candidates,
            max_files=max_files,
            max_chars=700,
            context_lines=3,
            report=report,
        )
        return compute_case_metrics(case, result, backend=backend)
    except CymbalError as exc:
        rel_count = len({c.rel_path for c in case.expected_callers})
        return CaseRetrievalMetrics(
            case_id=case.case_id,
            category=case.category,
            backend=backend,
            status="ERROR",
            precision=0.0,
            recall=0.0,
            r_precision=0.0,
            wrong_target_rate=0.0,
            ambiguous_rate=0.0,
            no_evidence_rate=1.0,
            source_location_accuracy=0.0,
            snippet_contract_coverage=0.0,
            returned_tokens=0,
            expected_count=rel_count,
            retrieved_count=0,
            true_positives=0,
            false_positives=0,
            explicit_wrong_target_hits=0,
            exact_line_matches=0,
            retrieved_locations=(),
            ambiguity_notes=(),
            truncation_notes=(),
            error=str(exc),
        )


def _discover_repo_query_target(
    repo_root: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], str, str, Path | None]:
    """Choose a deterministic ``(editable, candidates, symbol, message, edit_target)``."""
    py_files: list[str] = []
    skip_dirs = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
    }
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs and not d.startswith("."))
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                rel = Path(dirpath, fname).relative_to(repo_root).as_posix()
                py_files.append(rel)

    if "aider/local_callers.py" in py_files:
        editable = ("aider/local_callers.py",)
        candidates = tuple(p for p in py_files if p != "aider/local_callers.py")
        edit_candidate = (
            repo_root / "aider/caller_audit.py"
            if (repo_root / "aider/caller_audit.py").is_file()
            else (repo_root / candidates[0] if candidates else None)
        )
        return (
            editable,
            candidates,
            "find_related_callers",
            "Inspect callers of find_related_callers across the repository.",
            edit_candidate,
        )

    for rel in py_files:
        path = repo_root / rel
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and not node.name.startswith("_"):
                candidates = tuple(p for p in py_files if p != rel)
                edit_candidate = repo_root / candidates[0] if candidates else path
                return (
                    (rel,),
                    candidates,
                    node.name,
                    f"Inspect callers of {node.name} across the repository.",
                    edit_candidate,
                )

    fallback_edit = repo_root / py_files[0] if py_files else None
    return (
        (py_files[0],) if py_files else (),
        tuple(py_files[1:]),
        "main",
        "Inspect callers of main.",
        fallback_edit,
    )


@contextlib.contextmanager
def _isolated_repo_workspace(source_repo: Path) -> Iterator[Path]:
    """Create an isolated temporary worktree or copy of ``source_repo`` for safe edits."""
    resolved_src = source_repo.resolve()
    tmp_root = Path(tempfile.mkdtemp(prefix="aider_retbench_iso_"))
    work_dir = tmp_root / "repo"
    used_worktree = False
    try:
        if (resolved_src / ".git").exists():
            proc = subprocess.run(
                ["git", "worktree", "add", "--detach", "-q", str(work_dir), "HEAD"],
                cwd=str(resolved_src),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and work_dir.is_dir():
                used_worktree = True

        if not used_worktree:
            shutil.copytree(
                resolved_src,
                work_dir,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    "venv",
                    "__pycache__",
                    "node_modules",
                    ".pytest_cache",
                    "*.pyc",
                ),
            )
            env = dict(os.environ)
            env.update(
                {
                    "GIT_AUTHOR_NAME": "Aider Benchmark",
                    "GIT_AUTHOR_EMAIL": "bench@aider.local",
                    "GIT_COMMITTER_NAME": "Aider Benchmark",
                    "GIT_COMMITTER_EMAIL": "bench@aider.local",
                }
            )
            subprocess.run(["git", "init", "-q"], cwd=str(work_dir), env=env, check=True)
            subprocess.run(["git", "add", "."], cwd=str(work_dir), env=env, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "isolated benchmark copy"],
                cwd=str(work_dir),
                env=env,
                check=True,
            )

        yield work_dir.resolve()
    finally:
        if used_worktree:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(work_dir)],
                cwd=str(resolved_src),
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(resolved_src),
                capture_output=True,
                text=True,
                check=False,
            )
        shutil.rmtree(tmp_root, ignore_errors=True)


def _benchmark_repo_backend(
    repo_root: Path,
    *,
    repo_label: str,
    backend: str,
    warm_runs: int,
    editable: tuple[str, ...],
    candidates: tuple[str, ...],
    symbol: str,
    message: str,
    edit_file: Path | None,
    cymbal_runner: CymbalCommandRunner | None = None,
    cymbal_config: CymbalConfig | None = None,
) -> RepoPerfBenchmark:
    repo_file_count = len(editable) + len(candidates)
    cfg = cymbal_config or CymbalConfig()
    runner = cymbal_runner or SubprocessCymbalRunner(max_stderr_bytes=cfg.max_stderr_bytes)
    finder = build_caller_finder(
        backend=backend,  # type: ignore[arg-type]
        cymbal_config=cfg,
        runner=runner if backend == BACKEND_CYMBAL else None,
    )
    created_db_path: Path | None = None

    try:
        # 1. Cold index build (cymbal builds persistent SQLite index; builtin is stateless)
        if backend == BACKEND_CYMBAL:
            cold_metrics, created_db_path = _run_cymbal_index(
                repo_root, runner, cfg, force=True
            )
        else:
            u0, s0, _ = _snapshot_rusage(resource.RUSAGE_SELF)
            t0 = time.perf_counter()
            finder.lookup(repo_root, message, editable, candidates, max_files=5)
            wall_ms = (time.perf_counter() - t0) * 1000.0
            u1, s1, rss1 = _snapshot_rusage(resource.RUSAGE_SELF)
            cold_metrics = PhasePerfMetrics(
                wall_ms=round(wall_ms, 3),
                cpu_user_ms=round(max(0.0, u1 - u0), 3),
                cpu_sys_ms=round(max(0.0, s1 - s0), 3),
                max_rss_bytes=rss1,
                index_size_bytes=0,
                files_indexed=repo_file_count,
                files_reparsed=repo_file_count,
                files_unchanged=0,
                note="stateless on-demand scan (no persistent index)",
            )

        # 2. Warm queries over --warm-runs
        rusage_who = (
            resource.RUSAGE_CHILDREN if backend == BACKEND_CYMBAL else resource.RUSAGE_SELF
        )
        wu0, ws0, _ = _snapshot_rusage(rusage_who)
        samples_ms: list[float] = []
        for _ in range(max(1, warm_runs)):
            t0 = time.perf_counter()
            finder.lookup(repo_root, message, editable, candidates, max_files=5)
            samples_ms.append(round((time.perf_counter() - t0) * 1000.0, 3))
        wu1, ws1, wrss = _snapshot_rusage(rusage_who)
        warm_metrics = WarmQueryPerfMetrics(
            runs=len(samples_ms),
            p50_ms=round(compute_percentile(samples_ms, 50.0), 3),
            p95_ms=round(compute_percentile(samples_ms, 95.0), 3),
            min_ms=round(min(samples_ms), 3),
            max_ms=round(max(samples_ms), 3),
            mean_ms=round(sum(samples_ms) / len(samples_ms), 3),
            cpu_user_ms=round(max(0.0, wu1 - wu0), 3),
            cpu_sys_ms=round(max(0.0, ws1 - ws0), 3),
            max_rss_bytes=wrss,
            samples_ms=tuple(samples_ms),
        )

        # 3. Single-file incremental edit + update measurement
        if edit_file is not None and edit_file.is_file():
            original = edit_file.read_text(encoding="utf-8", errors="replace")
            edit_file.write_text(
                original + f"\n# retrieval_benchmark incremental edit marker ({symbol})\n",
                encoding="utf-8",
            )

        if backend == BACKEND_CYMBAL:
            incr_metrics, db2 = _run_cymbal_index(repo_root, runner, cfg, force=False)
            if created_db_path is None:
                created_db_path = db2
        else:
            iu0, is0, _ = _snapshot_rusage(resource.RUSAGE_SELF)
            t0 = time.perf_counter()
            finder.lookup(repo_root, message, editable, candidates, max_files=5)
            wall_ms = (time.perf_counter() - t0) * 1000.0
            iu1, is1, irss = _snapshot_rusage(resource.RUSAGE_SELF)
            incr_metrics = PhasePerfMetrics(
                wall_ms=round(wall_ms, 3),
                cpu_user_ms=round(max(0.0, iu1 - iu0), 3),
                cpu_sys_ms=round(max(0.0, is1 - is0), 3),
                max_rss_bytes=irss,
                index_size_bytes=0,
                files_indexed=repo_file_count,
                files_reparsed=repo_file_count,
                files_unchanged=0,
                note="stateless re-scan after single-file edit",
            )

        return RepoPerfBenchmark(
            repo_label=repo_label,
            repo_file_count=repo_file_count,
            backend=backend,
            target_symbol=symbol,
            cold_index=cold_metrics,
            warm_query=warm_metrics,
            incremental_update=incr_metrics,
        )
    except Exception as exc:
        empty_phase = PhasePerfMetrics(
            wall_ms=0.0,
            cpu_user_ms=0.0,
            cpu_sys_ms=0.0,
            max_rss_bytes=0,
            index_size_bytes=0,
            files_indexed=0,
            files_reparsed=0,
            files_unchanged=0,
        )
        empty_warm = WarmQueryPerfMetrics(
            runs=0,
            p50_ms=0.0,
            p95_ms=0.0,
            min_ms=0.0,
            max_ms=0.0,
            mean_ms=0.0,
            cpu_user_ms=0.0,
            cpu_sys_ms=0.0,
            max_rss_bytes=0,
            samples_ms=(),
        )
        return RepoPerfBenchmark(
            repo_label=repo_label,
            repo_file_count=repo_file_count,
            backend=backend,
            target_symbol=symbol,
            cold_index=empty_phase,
            warm_query=empty_warm,
            incremental_update=empty_phase,
            error=str(exc),
        )
    finally:
        if cymbal_runner is None:
            _cleanup_cymbal_db(created_db_path)


def run_benchmark(
    *,
    backends: Sequence[str] = (BACKEND_BUILTIN, BACKEND_CYMBAL),
    warm_runs: int = 10,
    large_repo: Path | None = None,
    cases: Sequence[RetrievalCase] | None = None,
    cymbal_runner: CymbalCommandRunner | None = None,
    cymbal_config: CymbalConfig | None = None,
) -> BenchmarkReport:
    """Execute the full deterministic Section B retrieval benchmark."""
    selected_backends = tuple(backends)
    for backend in selected_backends:
        if backend not in VALID_BENCHMARK_BACKENDS:
            raise ValueError(
                f"Unsupported backend {backend!r}; expected subset of {VALID_BENCHMARK_BACKENDS}"
            )
    if warm_runs < 1:
        raise ValueError("warm_runs must be >= 1")

    corpus = tuple(cases) if cases is not None else get_retrieval_corpus()
    cfg = cymbal_config or CymbalConfig()
    include_cymbal = BACKEND_CYMBAL in selected_backends
    live_runner: CymbalCommandRunner | None = (
        cymbal_runner
        if cymbal_runner is not None
        else (
            SubprocessCymbalRunner(max_stderr_bytes=cfg.max_stderr_bytes)
            if include_cymbal
            else None
        )
    )

    all_case_metrics: list[CaseRetrievalMetrics] = []
    read_range_comparisons: list[ReadRangeComparison] = []

    for case in corpus:
        with materialize_case(case) as mat:
            db_paths_to_clean: list[Path | None] = []
            try:
                if include_cymbal and live_runner is not None:
                    try:
                        _, main_db = _run_cymbal_index(mat.repo_root, live_runner, cfg, force=True)
                        db_paths_to_clean.append(main_db)
                        for wt_root in mat.worktree_roots:
                            _, wt_db = _run_cymbal_index(wt_root, live_runner, cfg, force=True)
                            db_paths_to_clean.append(wt_db)
                    except (RuntimeError, CymbalError):
                        pass

                for backend in selected_backends:
                    metrics = _evaluate_case_for_backend(
                        mat,
                        backend=backend,
                        cymbal_runner=live_runner if backend == BACKEND_CYMBAL else None,
                        cymbal_config=cfg,
                    )
                    all_case_metrics.append(metrics)

                for target in case.read_range_targets:
                    comparison = evaluate_read_range_target(
                        mat.repo_root,
                        case,
                        target,
                        include_cymbal_show=include_cymbal,
                        cymbal_runner=live_runner,
                        cymbal_config=cfg,
                    )
                    read_range_comparisons.append(comparison)
            finally:
                if cymbal_runner is None:
                    for db_path in db_paths_to_clean:
                        _cleanup_cymbal_db(db_path)

    # Performance benchmarks on BOTH small fixture repo and realistic large repo
    perf_results: list[RepoPerfBenchmark] = []
    small_case = next((c for c in corpus if c.category == "multi_file"), corpus[0])
    for backend in selected_backends:
        with materialize_case(small_case) as small_mat:
            edit_rel = small_case.candidates[0] if small_case.candidates else small_case.editable[0]
            perf_results.append(
                _benchmark_repo_backend(
                    small_mat.repo_root,
                    repo_label=f"small_fixture ({small_case.case_id})",
                    backend=backend,
                    warm_runs=warm_runs,
                    editable=small_case.editable,
                    candidates=small_case.candidates,
                    symbol=small_case.target_symbol,
                    message=small_case.message,
                    edit_file=small_mat.repo_root / edit_rel,
                    cymbal_runner=live_runner if backend == BACKEND_CYMBAL else None,
                    cymbal_config=cfg,
                )
            )

    target_large = (large_repo or _REPO_ROOT).resolve()
    for backend in selected_backends:
        with _isolated_repo_workspace(target_large) as iso_large:
            editable, candidates, sym, msg, edit_target = _discover_repo_query_target(iso_large)
            perf_results.append(
                _benchmark_repo_backend(
                    iso_large,
                    repo_label=f"large_repo ({target_large.name})",
                    backend=backend,
                    warm_runs=warm_runs,
                    editable=editable,
                    candidates=candidates,
                    symbol=sym,
                    message=msg,
                    edit_file=edit_target,
                    cymbal_runner=live_runner if backend == BACKEND_CYMBAL else None,
                    cymbal_config=cfg,
                )
            )

    summaries = summarize_by_category(all_case_metrics)
    deltas = compute_category_deltas(summaries)

    return BenchmarkReport(
        corpus_version=CORPUS_VERSION,
        pinned_cymbal_version=PINNED_CYMBAL_VERSION,
        backends=selected_backends,
        warm_runs=warm_runs,
        case_metrics=tuple(all_case_metrics),
        category_summaries=summaries,
        category_deltas=deltas,
        read_range_comparisons=tuple(read_range_comparisons),
        performance=tuple(perf_results),
    )


def report_to_dict(report: BenchmarkReport) -> dict[str, Any]:
    """Serialize ``BenchmarkReport`` into a plain JSON-compatible dictionary."""
    return asdict(report)


def render_markdown_report(report: BenchmarkReport) -> str:
    """Render a concise Markdown benchmark report with per-category tables."""
    lines: list[str] = [
        "# Issue #5 Section B — Deterministic No-LLM Retrieval Benchmark",
        "",
        f"- **Corpus version:** `{report.corpus_version}`",
        f"- **Pinned Cymbal version:** `v{report.pinned_cymbal_version}`",
        f"- **Backends:** `{', '.join(report.backends)}`",
        f"- **Warm query runs:** `{report.warm_runs}`",
        "- **Methodology:** Metrics are reported strictly **per category** (never collapsed "
        "into a single opaque average across heterogeneous categories; Oracle `beir-guide` "
        "`90905b05c622807ee5f8a78d`) with R-precision over the top `|Rel|` results "
        "(Oracle `ir-textbook` p. 198 `5ecfb939602f76b7331773f5`).",
        "",
        "## 1. Per-Category Retrieval Quality",
        "",
        "| Category | Backend | Precision | Recall | R-Prec | Wrong Target | Ambiguous | "
        "No Evidence | Loc Acc | Contract Cov | Tokens |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for category, by_backend in report.category_summaries.items():
        for backend in report.backends:
            summary = by_backend.get(backend)
            if summary is None:
                continue
            lines.append(
                f"| `{category}` | `{backend}` | {summary.precision:.3f} | "
                f"{summary.recall:.3f} | {summary.r_precision:.3f} | "
                f"{summary.wrong_target_rate:.3f} | {summary.ambiguous_rate:.3f} | "
                f"{summary.no_evidence_rate:.3f} | {summary.source_location_accuracy:.3f} | "
                f"{summary.snippet_contract_coverage:.3f} | {summary.returned_tokens:.1f} |"
            )

    if report.category_deltas:
        lines.extend(
            [
                "",
                "## 2. Per-Category Deltas (`cymbal - builtin`)",
                "",
                "| Category | dPrecision | dRecall | dR-Prec | dWrong Target | "
                "dLoc Acc | dContract Cov | dTokens |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for category, delta in report.category_deltas.items():
            lines.append(
                f"| `{category}` | {delta.precision_delta:+.3f} | {delta.recall_delta:+.3f} | "
                f"{delta.r_precision_delta:+.3f} | {delta.wrong_target_rate_delta:+.3f} | "
                f"{delta.source_location_accuracy_delta:+.3f} | "
                f"{delta.snippet_contract_coverage_delta:+.3f} | "
                f"{delta.returned_tokens_delta:+.1f} |"
            )

    lines.extend(
        [
            "",
            "## 3. ReadRange vs 700-char Excerpt (`local_callers`) vs `cymbal show`",
            "",
            "| Case | Target | Method | Tokens | Bytes Scanned | Bytes Returned | "
            "Contract Cov | Silent Truncation |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for comp in report.read_range_comparisons:
        target_label = f"`{comp.rel_path}:{comp.range_start}-{comp.range_end}`"
        for method_name in ("read_range", "local_callers_700", "cymbal_show"):
            m = comp.methods.get(method_name)
            if m is None:
                continue
            if not m.available:
                lines.append(
                    f"| `{comp.case_id}` | {target_label} | `{method_name}` | "
                    f"n/a | n/a | n/a | n/a | n/a ({m.error or 'unavailable'}) |"
                )
            else:
                silent = "YES" if m.silent_truncation else "no"
                lines.append(
                    f"| `{comp.case_id}` | {target_label} | `{method_name}` | "
                    f"{m.returned_tokens} | {m.bytes_scanned} | {m.bytes_returned} | "
                    f"{m.contract_coverage:.3f} | {silent} |"
                )

    lines.extend(
        [
            "",
            "## 4. Indexing, Warm Query & Incremental Update Latency",
            "",
            "| Repo | Files | Backend | Cold Build (ms) | Warm p50 (ms) | Warm p95 (ms) | "
            "Incr Update (ms) | Index Bytes | Incr Reparsed | CPU (ms) | Peak RSS (MiB) |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for perf in report.performance:
        if perf.error:
            lines.append(
                f"| `{perf.repo_label}` | {perf.repo_file_count} | `{perf.backend}` | "
                f"ERR | ERR | ERR | ERR | 0 | 0 | 0 | 0 ({perf.error}) |"
            )
            continue
        total_cpu = (
            perf.cold_index.cpu_user_ms
            + perf.cold_index.cpu_sys_ms
            + perf.warm_query.cpu_user_ms
            + perf.warm_query.cpu_sys_ms
            + perf.incremental_update.cpu_user_ms
            + perf.incremental_update.cpu_sys_ms
        )
        peak_rss_mib = (
            max(
                perf.cold_index.max_rss_bytes,
                perf.warm_query.max_rss_bytes,
                perf.incremental_update.max_rss_bytes,
            )
            / (1024.0 * 1024.0)
        )
        lines.append(
            f"| `{perf.repo_label}` | {perf.repo_file_count} | `{perf.backend}` | "
            f"{perf.cold_index.wall_ms:.2f} | {perf.warm_query.p50_ms:.2f} | "
            f"{perf.warm_query.p95_ms:.2f} | {perf.incremental_update.wall_ms:.2f} | "
            f"{perf.incremental_update.index_size_bytes} | "
            f"{perf.incremental_update.files_reparsed} | "
            f"{total_cpu:.2f} | {peak_rss_mib:.2f} |"
        )

    lines.append("")
    return "\n".join(lines)


def _parse_backends(raw: str) -> tuple[str, ...]:
    items = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not items:
        raise argparse.ArgumentTypeError("At least one backend must be specified")
    deduped: list[str] = []
    for item in items:
        if item not in VALID_BENCHMARK_BACKENDS:
            raise argparse.ArgumentTypeError(
                f"Unknown backend {item!r}; choose from {', '.join(VALID_BENCHMARK_BACKENDS)}"
            )
        if item not in deduped:
            deduped.append(item)
    return tuple(deduped)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic no-LLM retrieval benchmark (Issue #5 Section B)."
    )
    parser.add_argument(
        "--backends",
        default="builtin,cymbal",
        help="Comma-separated backends to evaluate (builtin,cymbal).",
    )
    parser.add_argument(
        "--warm-runs",
        type=int,
        default=10,
        help="Number of repeated warm queries for p50/p95 latency (default: 10).",
    )
    parser.add_argument(
        "--large-repo",
        type=Path,
        default=_REPO_ROOT,
        help="Path to realistic large repository (isolated via temp worktree/copy).",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=None,
        help="Optional path to write full JSON benchmark report.",
    )
    parser.add_argument(
        "--out-md",
        type=Path,
        default=None,
        help="Optional path to write Markdown benchmark report.",
    )
    parser.add_argument(
        "--check-corpus",
        action="store_true",
        help="Validate the versioned Section B fixture corpus and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.check_corpus:
        corpus = get_retrieval_corpus()
        errors = validate_corpus(corpus)
        if errors:
            for err in errors:
                print(f"CORPUS ERROR: {err}", file=sys.stderr)
            return 1
        print(
            f"Corpus v{CORPUS_VERSION} OK: {len(corpus)} cases covering all "
            f"{len(SECTION_B_CATEGORIES)} Section B categories."
        )
        return 0

    backends = _parse_backends(args.backends)
    report = run_benchmark(
        backends=backends,
        warm_runs=args.warm_runs,
        large_repo=args.large_repo,
    )
    md_text = render_markdown_report(report)

    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(
            json.dumps(report_to_dict(report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.out_md is not None:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        args.out_md.write_text(md_text, encoding="utf-8")

    print(md_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
