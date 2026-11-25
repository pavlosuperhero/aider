"""Fast deterministic unit tests for Issue #5 Section B retrieval benchmarks.

No live ``cymbal`` binary is required; all Cymbal interactions are verified via
an in-memory fake ``CymbalCommandRunner`` implementing the pinned v0.18.0 protocol.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Sequence

from aider.cymbal_callers import (
    BACKEND_BUILTIN,
    BACKEND_CYMBAL,
    PINNED_CYMBAL_VERSION,
    CallerLookupResult,
    CommandResult,
    CymbalConfig,
)
from aider.local_callers import CallerExcerpt
from scripts.local_context_trial import retrieval_benchmark as bench
from scripts.local_context_trial import retrieval_fixtures as fixtures


class FakeBenchmarkCymbalRunner:
    """Deterministic fake ``CymbalCommandRunner`` supporting index, ls, refs, impact, show."""

    def __init__(self, *, corpus: Sequence[fixtures.RetrievalCase]):
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self._index_counts: dict[Path, int] = {}
        self._case_by_symbol: dict[str, fixtures.RetrievalCase] = {}
        for case in corpus:
            self._case_by_symbol[case.target_symbol] = case
            for caller in case.expected_callers:
                self._case_by_symbol[caller.symbol] = case

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandResult:
        argv_tuple = tuple(argv)
        resolved_cwd = cwd.resolve()
        self.calls.append((argv_tuple, resolved_cwd))

        if len(argv_tuple) >= 2 and argv_tuple[1] == "--version":
            return CommandResult(
                argv=argv_tuple,
                returncode=0,
                stdout=f"cymbal v{PINNED_CYMBAL_VERSION}\n".encode("utf-8"),
                stderr=b"",
            )

        subcmd = argv_tuple[1] if len(argv_tuple) >= 2 else ""
        assert "--no-federate" in argv_tuple, f"Missing --no-federate in {argv_tuple}"

        if subcmd == "index":
            py_js_files = [
                p
                for p in resolved_cwd.rglob("*")
                if p.is_file() and ".git" not in p.parts and p.suffix in (".py", ".ts", ".js")
            ]
            total = max(1, len(py_js_files))
            force = "--force" in argv_tuple
            prev = self._index_counts.get(resolved_cwd, 0)
            if force or prev == 0:
                reparsed = total
                unchanged = 0
            else:
                reparsed = 1
                unchanged = max(0, total - 1)
            self._index_counts[resolved_cwd] = total
            msg = f"Done in 3ms — {reparsed} indexed, {total} symbols, {unchanged} unchanged\n"
            return CommandResult(
                argv=argv_tuple,
                returncode=0,
                stdout=b"",
                stderr=msg.encode("utf-8"),
            )

        if subcmd == "ls":
            payload = {
                "version": "0.1",
                "results": [
                    {
                        "path": str(resolved_cwd),
                        "file_count": self._index_counts.get(resolved_cwd, 1),
                        "symbol_count": self._index_counts.get(resolved_cwd, 1),
                        "db_size_bytes": 65536,
                    }
                ],
            }
            return self._json(argv_tuple, payload)

        if subcmd == "changed":
            return self._json(argv_tuple, {"version": "0.1", "changed": [], "stale": False})

        target = argv_tuple[-1]
        if subcmd == "refs":
            return self._json(argv_tuple, self._build_refs_payload(resolved_cwd, target))
        if subcmd == "impact":
            return self._json(argv_tuple, self._build_impact_payload(resolved_cwd, target))
        if subcmd == "show":
            return self._json(argv_tuple, self._build_show_payload(resolved_cwd, target))

        return CommandResult(
            argv=argv_tuple,
            returncode=2,
            stdout=b"",
            stderr=f"Unsupported fake subcommand: {subcmd}".encode("utf-8"),
        )

    def _build_refs_payload(self, cwd: Path, symbol: str) -> dict[str, object]:
        case = self._case_by_symbol.get(symbol)
        if case is None:
            return {"version": "0.1", "symbol": symbol, "references": []}
        refs = []
        for caller in case.expected_callers:
            if caller.symbol != symbol or caller.kind == "transitive":
                continue
            file_path = cwd / caller.rel_path
            if not file_path.is_file():
                continue
            lines = file_path.read_text(encoding="utf-8").splitlines()
            line_text = lines[caller.line - 1] if 1 <= caller.line <= len(lines) else ""
            refs.append(
                {
                    "rel_path": caller.rel_path,
                    "line": caller.line,
                    "symbol": symbol,
                    "kind": "alias" if caller.kind == "alias" else "call",
                    "snippet": line_text,
                }
            )
        defs = [
            {"rel_path": rel, "line": 1, "name": symbol}
            for rel in case.editable
            if (cwd / rel).is_file()
        ]
        return {
            "version": "0.1",
            "symbol": symbol,
            "definitions": defs,
            "references": refs,
            "ambiguous": case.expect_ambiguous,
        }

    def _build_impact_payload(self, cwd: Path, symbol: str) -> dict[str, object]:
        case = self._case_by_symbol.get(symbol)
        if case is None:
            return {"version": "0.1", "symbol": symbol, "direct_callers": []}
        direct = []
        transitive = []
        for caller in case.expected_callers:
            if caller.symbol != symbol:
                continue
            file_path = cwd / caller.rel_path
            if not file_path.is_file():
                continue
            entry = {
                "rel_path": caller.rel_path,
                "line": caller.line,
                "symbol": symbol,
                "kind": "impact",
                "transitive": caller.kind == "transitive",
            }
            if caller.kind == "transitive":
                transitive.append(entry)
            else:
                direct.append(entry)
        return {
            "version": "0.1",
            "symbol": symbol,
            "direct_callers": direct,
            "transitive_callers": transitive,
            "ambiguous": case.expect_ambiguous,
        }

    @staticmethod
    def _build_show_payload(cwd: Path, spec: str) -> dict[str, object]:
        rel_path, span = spec.rsplit(":", 1)
        start_str, end_str = span.split("-", 1)
        start_line = int(start_str)
        end_line = int(end_str)
        lines = (cwd / rel_path).read_text(encoding="utf-8").splitlines()
        selected = [
            {"line": ln, "content": lines[ln - 1]}
            for ln in range(start_line, min(end_line, len(lines)) + 1)
        ]
        return {
            "version": "0.1",
            "results": {
                "rel_path": rel_path,
                "start_line": start_line,
                "end_line": min(end_line, len(lines)),
                "lines": selected,
            },
        }

    @staticmethod
    def _json(argv_tuple: tuple[str, ...], payload: object) -> CommandResult:
        return CommandResult(
            argv=argv_tuple,
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )


def test_corpus_validation_and_all_ten_categories():
    assert fixtures.CORPUS_VERSION == "1.0"
    corpus = fixtures.get_retrieval_corpus()
    assert fixtures.validate_corpus(corpus) == []

    categories = {case.category for case in corpus}
    assert categories == set(fixtures.SECTION_B_CATEGORIES)
    assert len(fixtures.SECTION_B_CATEGORIES) == 10

    large_cases = fixtures.get_cases_by_category("large_file")
    assert len(large_cases) == 1
    big_src = large_cases[0].files["catalog/big_router.py"]
    assert len(big_src.encode("utf-8")) > 140 * 1024
    assert large_cases[0].expected_callers[0].line > 4000

    neg_cases = fixtures.get_cases_by_category("negative_control")
    assert len(neg_cases) == 1
    assert neg_cases[0].expected_callers == ()


def test_corpus_materialization_and_worktree_cleanup():
    corpus = fixtures.get_retrieval_corpus()
    for case in corpus:
        with fixtures.materialize_case(case) as mat:
            assert mat.repo_root.is_dir()
            assert (mat.repo_root / ".git").exists()
            for rel in case.files:
                assert (mat.repo_root / rel).is_file()

            if case.category == "multi_worktree":
                assert len(mat.worktree_roots) == 1
                wt_root = mat.worktree_roots[0]
                assert wt_root.is_dir()
                assert (wt_root / "shipping/sibling_caller.py").is_file()
                proc = subprocess.run(
                    ["git", "worktree", "list", "--porcelain"],
                    cwd=str(mat.repo_root),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                worktree_lines = [
                    line for line in proc.stdout.splitlines() if line.startswith("worktree ")
                ]
                assert len(worktree_lines) == 2

            base_dir = mat.base_dir
            wt_paths = mat.worktree_roots

        assert not base_dir.exists()
        for wt_path in wt_paths:
            assert not wt_path.exists()


def test_metric_calculations_precision_recall_r_precision_and_negative_control():
    case = fixtures.get_cases_by_category("multi_file")[0]
    inv = next(c for c in case.expected_callers if c.rel_path == "services/inventory.py")
    orders = next(c for c in case.expected_callers if c.rel_path == "services/orders.py")

    # Rank order: wrong target first, then exact inventory match, then orders with wrong line.
    excerpts = (
        CallerExcerpt(
            path="services/unrelated.py",
            start_line=1,
            line=1,
            symbol="normalize_sku",
            code="def format_warehouse_label(name): pass",
        ),
        CallerExcerpt(
            path="services/inventory.py",
            start_line=max(1, inv.line - 2),
            line=inv.line,
            symbol="normalize_sku",
            code="prefix, code = result",
        ),
        CallerExcerpt(
            path="services/orders.py",
            start_line=max(1, orders.line - 2),
            line=orders.line + 5,
            symbol="normalize_sku",
            code="parsed = normalize_sku(raw_sku)",
        ),
    )
    result = CallerLookupResult(
        status="INCOMPLETE",
        excerpts=excerpts,
        items=(),
        definitions=(("core/normalize.py", "normalize_sku"),),
        source="builtin",
        backend_requested=BACKEND_BUILTIN,
        fallback_used=False,
        scope=("production_only",),
        ambiguity=("ambiguous symbol binding",),
        truncation=(),
    )
    metrics = bench.compute_case_metrics(case, result, backend=BACKEND_BUILTIN)
    assert metrics.true_positives == 2
    assert metrics.false_positives == 1
    assert metrics.explicit_wrong_target_hits == 1
    assert metrics.precision == round(2 / 3, 6)
    assert metrics.recall == round(2 / 3, 6)
    assert metrics.r_precision == round(2 / 3, 6)
    assert metrics.wrong_target_rate == round(1 / 3, 6)
    assert metrics.ambiguous_rate == 1.0
    assert metrics.no_evidence_rate == 0.0
    # 1 of 2 true positives matched exact line
    assert metrics.source_location_accuracy == 0.5
    # 1 of 3 contract substrings matched ("prefix, code = result")
    assert metrics.snippet_contract_coverage == round(1 / 3, 6)
    assert metrics.returned_tokens > 0

    # Negative control: empty retrieval vs false positive
    neg_case = fixtures.get_cases_by_category("negative_control")[0]
    empty_res = CallerLookupResult(
        status="NO_EVIDENCE",
        excerpts=(),
        items=(),
        definitions=(("utils/orphan.py", "legacy_checksum"),),
        source="builtin",
        backend_requested=BACKEND_BUILTIN,
        fallback_used=False,
        scope=("production_only",),
        ambiguity=(),
        truncation=(),
    )
    neg_ok = bench.compute_case_metrics(neg_case, empty_res, backend=BACKEND_BUILTIN)
    assert neg_ok.precision == 1.0
    assert neg_ok.recall == 1.0
    assert neg_ok.r_precision == 1.0
    assert neg_ok.wrong_target_rate == 0.0
    assert neg_ok.no_evidence_rate == 1.0

    fp_res = CallerLookupResult(
        status="COMPLETE_WITHIN_SCOPE",
        excerpts=(
            CallerExcerpt(
                path="utils/active.py",
                start_line=1,
                line=3,
                symbol="legacy_checksum",
                code="label = 'legacy_checksum'",
            ),
        ),
        items=(),
        definitions=(("utils/orphan.py", "legacy_checksum"),),
        source="cymbal",
        backend_requested=BACKEND_CYMBAL,
        fallback_used=False,
        scope=("production_only",),
        ambiguity=(),
        truncation=(),
    )
    neg_bad = bench.compute_case_metrics(neg_case, fp_res, backend=BACKEND_CYMBAL)
    assert neg_bad.precision == 0.0
    assert neg_bad.r_precision == 0.0
    assert neg_bad.wrong_target_rate == 1.0
    assert neg_bad.explicit_wrong_target_hits == 1

    # Percentile edge cases
    assert bench.compute_percentile([], 50.0) == 0.0
    assert bench.compute_percentile([4.2], 95.0) == 4.2
    assert bench.compute_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50.0) == 3.0
    assert bench.compute_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95.0) == 4.8


def test_read_range_vs_700_char_excerpt_and_cymbal_show():
    corpus = fixtures.get_retrieval_corpus()
    fake_runner = FakeBenchmarkCymbalRunner(corpus=corpus)

    # Single-file case: downstream invariants sit past +/-3 context lines from call site
    single_case = fixtures.get_cases_by_category("single_file")[0]
    with fixtures.materialize_case(single_case) as mat:
        comp = bench.evaluate_read_range_target(
            mat.repo_root,
            single_case,
            single_case.read_range_targets[0],
            include_cymbal_show=True,
            cymbal_runner=fake_runner,
        )
    rr = comp.methods["read_range"]
    lc = comp.methods["local_callers_700"]
    cs = comp.methods["cymbal_show"]
    assert rr.available and rr.contract_coverage == 1.0 and not rr.silent_truncation
    assert cs.available and cs.contract_coverage == 1.0 and not cs.silent_truncation
    assert lc.available and lc.contract_coverage < 1.0 and lc.silent_truncation
    assert "COMP_ORDER" in lc.missing_substrings

    # Large-file case (>140 KiB, past line 4000): local_callers silently skips entire file
    large_case = fixtures.get_cases_by_category("large_file")[0]
    with fixtures.materialize_case(large_case) as mat:
        large_comp = bench.evaluate_read_range_target(
            mat.repo_root,
            large_case,
            large_case.read_range_targets[0],
            include_cymbal_show=True,
            cymbal_runner=fake_runner,
        )
    rr_big = large_comp.methods["read_range"]
    lc_big = large_comp.methods["local_callers_700"]
    cs_big = large_comp.methods["cymbal_show"]
    assert rr_big.available and rr_big.contract_coverage == 1.0 and not rr_big.silent_truncation
    assert rr_big.bytes_returned < rr_big.file_size_bytes
    assert rr_big.net_tokens_saved > 0
    assert cs_big.available and cs_big.contract_coverage == 1.0 and not cs_big.silent_truncation
    assert lc_big.contract_coverage == 0.0 and lc_big.silent_truncation


def test_full_benchmark_with_builtin_and_fake_cymbal_outputs_json_and_md(tmp_path):
    corpus = fixtures.get_retrieval_corpus()
    fake_runner = FakeBenchmarkCymbalRunner(corpus=corpus)

    # Build a small realistic temp git repo to stand in for --large-repo without slowing tests
    synth_large = tmp_path / "synth_large_repo"
    synth_large.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(synth_large), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(synth_large), check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=str(synth_large), check=True)
    (synth_large / "core.py").write_text("def target_fn(x):\n    return x + 1\n", encoding="utf-8")
    (synth_large / "app.py").write_text(
        "from core import target_fn\n\ndef run(v):\n    return target_fn(v)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(synth_large), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(synth_large), check=True)
    status_before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(synth_large),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    report = bench.run_benchmark(
        backends=(BACKEND_BUILTIN, BACKEND_CYMBAL),
        warm_runs=3,
        large_repo=synth_large,
        cases=corpus,
        cymbal_runner=fake_runner,
        cymbal_config=CymbalConfig(enforce_pinned_version=True),
    )

    # Target --large-repo was never dirtied by incremental update benchmarks
    status_after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(synth_large),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status_after == status_before == ""

    # All 10 categories present separately (never collapsed) for both backends
    assert set(report.category_summaries.keys()) == set(fixtures.SECTION_B_CATEGORIES)
    assert set(report.category_deltas.keys()) == set(fixtures.SECTION_B_CATEGORIES)
    for cat in fixtures.SECTION_B_CATEGORIES:
        assert set(report.category_summaries[cat].keys()) == {BACKEND_BUILTIN, BACKEND_CYMBAL}

    # Verify expected backend contrasts on transitive & large_file categories
    assert report.category_summaries["transitive"][BACKEND_BUILTIN].recall == 0.5
    assert report.category_summaries["transitive"][BACKEND_CYMBAL].recall == 1.0
    assert report.category_summaries["large_file"][BACKEND_BUILTIN].recall == 0.0
    assert report.category_summaries["large_file"][BACKEND_CYMBAL].recall == 1.0
    assert report.category_summaries["multi_worktree"][BACKEND_CYMBAL].wrong_target_rate == 0.0
    assert report.category_summaries["negative_control"][BACKEND_BUILTIN].wrong_target_rate == 0.0
    assert report.category_summaries["negative_control"][BACKEND_CYMBAL].wrong_target_rate == 0.0

    # Performance benchmarks cover small + large repos for both backends
    assert len(report.performance) == 4
    for perf in report.performance:
        assert perf.error == ""
        assert perf.warm_query.runs == 3
        assert perf.warm_query.p50_ms >= 0.0
        assert perf.warm_query.p95_ms >= perf.warm_query.p50_ms
        if perf.backend == BACKEND_CYMBAL:
            assert perf.cold_index.index_size_bytes == 65536
            assert perf.incremental_update.files_reparsed == 1

    # JSON & Markdown outputs
    payload = bench.report_to_dict(report)
    encoded = json.dumps(payload, indent=2)
    decoded = json.loads(encoded)
    assert decoded["corpus_version"] == "1.0"
    assert decoded["pinned_cymbal_version"] == PINNED_CYMBAL_VERSION
    assert len(decoded["read_range_comparisons"]) >= 4

    md = bench.render_markdown_report(report)
    assert "## 1. Per-Category Retrieval Quality" in md
    assert "## 2. Per-Category Deltas (`cymbal - builtin`)" in md
    assert "## 3. ReadRange vs 700-char Excerpt (`local_callers`) vs `cymbal show`" in md
    assert "## 4. Indexing, Warm Query & Incremental Update Latency" in md
    assert "90905b05c622807ee5f8a78d" in md
    assert "5ecfb939602f76b7331773f5" in md
    for cat in fixtures.SECTION_B_CATEGORIES:
        assert f"`{cat}`" in md


def test_cli_check_corpus_and_builtin_json_md_export(tmp_path):
    assert bench.main(["--check-corpus"]) == 0

    mini_repo = tmp_path / "mini_repo"
    mini_repo.mkdir()
    (mini_repo / "mod.py").write_text("def helper(v):\n    return v * 2\n", encoding="utf-8")
    (mini_repo / "caller.py").write_text(
        "from mod import helper\n\ndef entry(v):\n    return helper(v)\n",
        encoding="utf-8",
    )

    out_json = tmp_path / "out" / "report.json"
    out_md = tmp_path / "out" / "report.md"
    rc = bench.main(
        [
            "--backends",
            "builtin",
            "--warm-runs",
            "2",
            "--large-repo",
            str(mini_repo),
            "--out-json",
            str(out_json),
            "--out-md",
            str(out_md),
        ]
    )
    assert rc == 0
    assert out_json.is_file()
    assert out_md.is_file()
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["backends"] == ["builtin"]
    assert data["warm_runs"] == 2
    assert set(data["category_summaries"].keys()) == set(fixtures.SECTION_B_CATEGORIES)
    assert "Issue #5 Section B" in out_md.read_text(encoding="utf-8")
