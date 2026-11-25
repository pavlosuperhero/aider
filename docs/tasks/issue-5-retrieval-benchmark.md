# Issue #5 Section B — Deterministic No-LLM Retrieval Benchmark

- **Corpus version:** `1.0`
- **Pinned Cymbal version:** `v0.18.0`
- **Backends:** `builtin, cymbal`
- **Warm query runs:** `5`
- **Methodology:** Metrics are reported strictly **per category** (never collapsed into a single opaque average across heterogeneous categories; Oracle `beir-guide` `90905b05c622807ee5f8a78d`) with R-precision over the top `|Rel|` results (Oracle `ir-textbook` p. 198 `5ecfb939602f76b7331773f5`).

## 1. Per-Category Retrieval Quality

| Category | Backend | Precision | Recall | R-Prec | Wrong Target | Ambiguous | No Evidence | Loc Acc | Contract Cov | Tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_file` | `builtin` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 74.0 |
| `single_file` | `cymbal` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 75.0 |
| `multi_file` | `builtin` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 0.667 | 143.0 |
| `multi_file` | `cymbal` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 0.667 | 143.0 |
| `multi_language` | `builtin` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 167.0 |
| `multi_language` | `cymbal` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 167.0 |
| `aliases` | `builtin` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 90.0 |
| `aliases` | `cymbal` | 1.000 | 0.500 | 0.500 | 0.000 | 0.000 | 0.000 | 1.000 | 0.500 | 46.0 |
| `same_name` | `builtin` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 49.0 |
| `same_name` | `cymbal` | 0.500 | 1.000 | 1.000 | 0.500 | 1.000 | 0.000 | 1.000 | 1.000 | 106.0 |
| `prod_vs_test` | `builtin` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 60.0 |
| `prod_vs_test` | `cymbal` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 60.0 |
| `transitive` | `builtin` | 1.000 | 0.500 | 0.500 | 0.000 | 0.000 | 0.000 | 1.000 | 0.500 | 31.0 |
| `transitive` | `cymbal` | 1.000 | 1.000 | 1.000 | 0.000 | 1.000 | 0.000 | 1.000 | 1.000 | 92.0 |
| `large_file` | `builtin` | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | 0.000 | 0.000 | 0.0 |
| `large_file` | `cymbal` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 57.0 |
| `multi_worktree` | `builtin` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 53.0 |
| `multi_worktree` | `cymbal` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 53.0 |
| `negative_control` | `builtin` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 0.0 |
| `negative_control` | `cymbal` | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 0.0 |

## 2. Per-Category Deltas (`cymbal - builtin`)

| Category | dPrecision | dRecall | dR-Prec | dWrong Target | dLoc Acc | dContract Cov | dTokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_file` | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +1.0 |
| `multi_file` | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.0 |
| `multi_language` | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.0 |
| `aliases` | +0.000 | -0.500 | -0.500 | +0.000 | +0.000 | -0.500 | -44.0 |
| `same_name` | -0.500 | +0.000 | +0.000 | +0.500 | +0.000 | +0.000 | +57.0 |
| `prod_vs_test` | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.0 |
| `transitive` | +0.000 | +0.500 | +0.500 | +0.000 | +0.000 | +0.500 | +61.0 |
| `large_file` | +1.000 | +1.000 | +1.000 | +0.000 | +1.000 | +1.000 | +57.0 |
| `multi_worktree` | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.0 |
| `negative_control` | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 | +0.0 |

## 3. ReadRange vs 700-char Excerpt (`local_callers`) vs `cymbal show`

| Case | Target | Method | Tokens | Bytes Scanned | Bytes Returned | Contract Cov | Silent Truncation |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `b01_single_file` | `billing/checkout.py:4-25` | `read_range` | 282 | 1041 | 993 | 1.000 | no |
| `b01_single_file` | `billing/checkout.py:4-25` | `local_callers_700` | 74 | 1041 | 274 | 0.333 | YES |
| `b01_single_file` | `billing/checkout.py:4-25` | `cymbal_show` | 278 | 1041 | 993 | 1.000 | no |
| `b02_multi_file` | `services/orders.py:4-19` | `read_range` | 178 | 654 | 610 | 1.000 | no |
| `b02_multi_file` | `services/orders.py:4-19` | `local_callers_700` | 61 | 654 | 222 | 0.333 | YES |
| `b02_multi_file` | `services/orders.py:4-19` | `cymbal_show` | 176 | 654 | 610 | 1.000 | no |
| `b03_multi_language` | `web/cart.ts:3-19` | `read_range` | 141 | 514 | 463 | 1.000 | no |
| `b03_multi_language` | `web/cart.ts:3-19` | `local_callers_700` | 64 | 514 | 241 | 0.000 | YES |
| `b03_multi_language` | `web/cart.ts:3-19` | `cymbal_show` | 138 | 514 | 463 | 1.000 | no |
| `b08_large_file` | `catalog/big_router.py:4061-4074` | `read_range` | 161 | 207181 | 514 | 1.000 | no |
| `b08_large_file` | `catalog/big_router.py:4061-4074` | `local_callers_700` | 0 | 131073 | 0 | 0.000 | YES |
| `b08_large_file` | `catalog/big_router.py:4061-4074` | `cymbal_show` | 160 | 207181 | 514 | 1.000 | no |

## 4. Indexing, Warm Query & Incremental Update Latency

| Repo | Files | Backend | Cold Build (ms) | Warm p50 (ms) | Warm p95 (ms) | Incr Update (ms) | Index Bytes | Incr Reparsed | CPU (ms) | Peak RSS (MiB) |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `small_fixture (b02_multi_file)` | 5 | `builtin` | 1.10 | 0.73 | 0.79 | 0.68 | 0 | 5 | 5.41 | 34.95 |
| `small_fixture (b02_multi_file)` | 5 | `cymbal` | 14.05 | 69.38 | 88.43 | 14.01 | 122880 | 1 | 206.76 | 21.91 |
| `large_repo (aider)` | 169 | `builtin` | 25.89 | 23.98 | 31.77 | 24.07 | 0 | 169 | 166.60 | 45.72 |
| `large_repo (aider)` | 169 | `cymbal` | 218.58 | 98.80 | 118.06 | 24.28 | 2719744 | 1 | 1155.67 | 76.95 |

## 5. Observed Takeaways & Environment

- **Recorded on:** `main` base `f90135b` (`feat/issue-5-b1-d1`), macOS Apple Silicon, `/opt/homebrew/bin/cymbal v0.18.0`, `.venv/bin/python` (CPython 3.12.13).
- **Reproduce command:** `HOME=/tmp/cymbal-bench-home .venv/bin/python scripts/local_context_trial/retrieval_benchmark.py --backends builtin,cymbal --warm-runs 5 --out-md docs/tasks/issue-5-retrieval-benchmark.md`
- **Where `cymbal v0.18.0` improves over `builtin`:**
  - `large_file` (`>140 KiB`, line > 4000): `cymbal` achieves `1.000` precision/recall/location accuracy vs `builtin` `0.000` (`NO_EVIDENCE` due to the 128 KiB file-size cap).
  - `transitive`: `cymbal` discovers indirect callers (`1.000` recall, `1.000` contract coverage, explicitly flagged `INCOMPLETE` / `Ambiguous = 1.000`) vs `builtin` `0.500` recall (direct callers only).
  - `multi_worktree` (`--no-federate`) and `negative_control`: `0.000` wrong-target leakage on both backends.
  - Incremental re-indexing on `aider` (169 tracked files): `cymbal` reparses `1` file (`24.28 ms`, `2.72 MiB` SQLite index) vs `builtin` scanning all `169` files.
- **Where `builtin` outperforms `cymbal v0.18.0` (documented limitations):**
  - `aliases`: `builtin` resolves aliased imports (`1.000` recall) whereas `cymbal v0.18.0` missed the aliased call site (`0.500` recall).
  - `same_name`: `builtin` stayed at `1.000` precision (`0.000` wrong target), whereas `cymbal v0.18.0` returned a same-name collision (`0.500` precision / `0.500` wrong target) and surfaced `Ambiguous = 1.000` (`INCOMPLETE`).
  - Warm query latency on 5-file and 169-file repositories: `cymbal`'s multi-command subprocess round trips (`69.38 ms` / `98.80 ms` `p50`) are slower than in-process `builtin` scans (`0.73 ms` / `23.98 ms` `p50`).
- **ReadRange vs 700-char excerpts vs `cymbal show`:** Both `read_range` and `cymbal_show` preserved `1.000` contract-critical condition coverage across all four targets (`138–282` tokens, `0/4` silent truncations), whereas `local_callers_700` silently truncated contract-critical lines on `4/4` targets (`0.000–0.333` coverage, including `0` tokens on `b08_large_file`).
