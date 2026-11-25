# Issue 5 implementation plan and tracker

[Issue #5](https://github.com/pavlosuperhero/aider/issues/5) builds on the [issue #7 pre-commit verifier](https://github.com/pavlosuperhero/aider/issues/7). Keep Cymbal, range-based prompt changes and facts opt-in during evaluation. Use `feat/caller-context-cymbal-readrange-facts` as the consolidated branch for all seven iterations. Each iteration has a headless `agy` NEON worker. The lead supplies the Oracle passage via `fetch(id)`, reviews the diff and verifies the observed behavior before closing the task.

| Task | Status | Observed result or remaining work | PR |
| --- | --- | --- | --- |
| 1 | In progress | Baseline reproduced (`13/13` synthetic, `2/2` Requests, `5/5` Issue 5). The 30-replay frozen corpus manifest and preregistration are outstanding. | — |
| 2 | Verified locally | Scoped evidence API and tests verified (`test_local_callers.py`). | — |
| 3 | Verified locally | Cymbal adapter unit/fake-runner tests AND live pinned `cymbal v0.18.0` smoke test pass (`test_cymbal_callers.py`: `refs`, `impact`, clean/dirty `changed`, `show`, `build_caller_finder('cymbal').lookup`). | — |
| 4 | Verified locally | Bounded source reader, validated live `cymbal v0.18.0` `show`, and tests verified (`test_read_range.py`). | — |
| 5 | Verified locally | Project facts behind `FactRepository` port: `--facts` defaults to `.aider.facts.jsonl` (or `--facts-file`) with deterministic `chat_id` hash (`chat_history_file` + worktree), `--facts-db PATH|sqlite://URL` uses SQLite/FTS5 with `chat_id`, source-hash revalidation, `/fact`, and tests verified (`test_project_facts.py`). | — |
| 6 | Verified locally | CLI flags, `/read-range`, `/fact`, automatic call-site ranges, strict verifier evidence, and integration tests verified (`test_local_context_integration.py`, `test_caller_audit.py`). | — |
| 7 | In progress | Trial runner, `--suite issue5` (`5/5` fixtures), `--preset issue5-economy`, prompt/completion token telemetry, `--suite replay --replay-manifest PATH`, and initial 106-attempt Qwen 3.6 35B-A3B trials completed. 30-task frozen unseen replay corpus, 8B-model evaluation, and default-on shipping decision remain outstanding. | — |

### Implementation verified on `feat/caller-context-cymbal-readrange-facts`

- **All Issue #5 unit, integration, replay, and trial suites** (`.venv/bin/python -m pytest -q tests/basic/test_cymbal_callers.py tests/basic/test_read_range.py tests/basic/test_project_facts.py tests/basic/test_local_callers.py tests/basic/test_caller_audit.py tests/basic/test_local_context_integration.py tests/basic/test_local_context_trial.py scripts/local_context_trial/test_replay_corpus.py`): **179 passed, 0 skipped** (including live `test_pinned_v0_18_0_cymbal_smoke` against `/opt/homebrew/bin/cymbal` `v0.18.0`, `/arch` handoff + dry-run/no-op outcome reporting, `/ask` contract exemption, named `/drop` on ranges, `/read` alias, and `check_for_file_mentions` large-file regression).
- **Regression suites** (`test_coder.py`, `test_commands.py`, `test_main.py`, `test_oracle.py`, `test_oracle_integration.py`, `test_tdd.py`, `test_tdd_integration.py`): passed with zero regressions.
- **Fixture self-checks:** `heldout.py --suite issue5 --check-fixtures` (**5/5**), `--check-fixtures` (**13/13**), and `--suite requests --check-fixtures` (**2/2**) passed.
- **Lint (`.venv/bin/flake8`):** **0 warnings** across all new/modified issue #5 files.
- **Observed Qwen 3.6 35B-A3B local trial results (all features kept opt-in):**
  - **72-attempt `builtin` trial (`d8bedb1`, `issue5-qwen36-35b-run/`):** On `issue5` (60 attempts), enabling JSONL `--facts` raised passes from **6/15 to 12/15** (`+6/−0` across 2 distinct convention fixtures repeated 3×, `facts_stale_control` **6/6** with `0/6` stale attachment). Across the **12/15 matched non-timeout attempts** with recorded usage in both arms (`3/3` `facts_currency` `facts=off` runs hit the 360s timeout), cumulative input dropped **−16.7%** (`−1,306` tokens) and mean latency including timeouts dropped from `102.1s` to `31.0s`. On `requests` (12 attempts, `builtin`, `--no-facts`), `requests_copy` stayed **3/3** while `requests_json` (`0/3` evidence attached) scored 2/3 (`range=off`) vs 0/3 (`range=on`, unresolved variance with slightly different input counts).
  - **18-attempt `builtin` vs `cymbal` trial (`d8bedb1`, `issue5-qwen36-35b-cymbal-run/`):** On `read_range_large_file` (`>140 KiB`), `cymbal` solved **3/3** vs `builtin` **0/3** (`+34` / `+0.7%` first-prompt tokens), and exposed a `check_for_file_mentions` bug that triggered an avoidable 92,998-token second call. On `requests_copy`, `cymbal` matched **3/3** with slightly lower input (`13,020` vs `13,165`) and latency (`56.7s` vs `64.9s`).
  - **Post-fix live confirmation (`7d6af8d`, `large_file_after_fix.jsonl` & `fast_post_fix.jsonl`):** After fixing `check_for_file_mentions` and tightening auto call-site ranges (`context_lines=5`), `cymbal` on `read_range_large_file` (`n = 3` rounds) stayed **3/3** while dropping from **98,181 → 5,183** cumulative input tokens (**−94.7%**, 1.00 call/attempt, `25.1s` vs `143.7s`). In the fast 10-attempt full-suite check (`--local-callers-read-range --facts`, `n = 1` round), `cymbal` passed **5/5** (`5,275` input tokens/solve, `0` extra calls) vs `builtin` **4/5** (`6,603` input tokens/solve, `0` extra calls); `caller_multi_scope` dropped from `11,165` → `5,437` input tokens (`−51.3%`).
- **Still outstanding before default-on shipping:** building/freezing a 20–30 task unseen real-repo replay corpus, testing smaller models (e.g., 8B), and follow-up UX issues [#10](https://github.com/pavlosuperhero/aider/issues/10) (`cmd_test` output bounding) and [#11](https://github.com/pavlosuperhero/aider/issues/11) (semi-auto fact extraction & first-run `cymbal index` prompt).

### Baseline verified on d2f5965

- `python scripts/local_context_trial/heldout.py --check-fixtures` passed 13/13. Adding `--suite requests` passed 2/2. These self-checks verify that the starting code passes its smoke test and fails its independent hidden test.
- A separate call to `find_related_callers` with each Requests fixture’s task, both editable paths and all candidate files returned three ambiguous excerpts for `requests_copy` (models.py:456, sessions.py:203, utils.py:928) and zero excerpts for `requests_json`. These coordinates mark excerpt starts. Known contract locations are in task 1.
- `python -m pytest -q tests/basic/test_local_callers.py tests/basic/test_caller_audit.py` passed all 70 tests. The [issue #7 report](../../scripts/local_context_trial/issue7-report.md) provides the historical model results. No new model trial was run for this plan.

## 1. Freeze the baseline

- **Original task branch:** `codex/5-baseline-tracker`
- **Dependency:** None
- **Reason:** On `d2f5965`, 13/13 synthetic and 2/2 Requests 2.34.2 fixtures passed smoke and failed hidden checks. A separate discovery probe found `requests_copy` returned `INCOMPLETE` (expected calls `models.py:458-459`), and `requests_json` returned `NO_EVIDENCE` (alias `models.py:67`, exception `__reduce__` `exceptions.py:55`).
- **Expected behavior:** Establish the baseline tracker for `d2f5965` and freeze 30 unseen real replays and negative controls before tuning.
- **Acceptance checks:**
  - Baseline tracker must record `d2f5965` smoke-pass and hidden-fail outcomes for 13/13 synthetic and 2/2 Requests 2.34.2 fixtures, including `requests_copy` (`models.py:458-459`) and `requests_json` (`models.py:67`, `exceptions.py:55`).
  - 30 unseen real replays and negative controls must be frozen before tuning begins.
- **Oracle fetch passage ID:** IR p198 `5ecfb939602f76b7331773f5`

## 2. Report caller evidence and scope

- **Original task branch:** `codex/5-caller-evidence`
- **Dependency:** Task 1 baseline
- **Reason:** Selection by path and a 3-file cap can hide limited coverage.
- **Expected behavior:** Extend the existing report and expose an immutable typed result with states `NO_EVIDENCE`, `INCOMPLETE`, and `COMPLETE_WITHIN_SCOPE`, including fields for `path`, `line`, `symbol`, `kind`, `source`, `scope`, `ambiguity`, and `truncation`.
- **Acceptance checks:**
  - Verify the public result is immutable, typed, and supports `NO_EVIDENCE`, `INCOMPLETE`, and `COMPLETE_WITHIN_SCOPE` with all specified fields.
  - Add planned test coverage for duplicate cases plus capped production and test cases to validate reported `scope`, `ambiguity`, and `truncation`.
- **Oracle fetch passage ID:** `0284bd306f4d5bc7d5237220` (Think Python, p81)

## 3. Integrate the Cymbal adapter

- **Original task branch:** `codex/5-cymbal-adapter`
- **Dependency:** Task 2
- **Reason:** The composite currently passes report data only to recognized built-in classes. A third adapter needs the same reporting path or its uncertainty can disappear.
- **Expected behavior:**
  - Propagate scope, ambiguity and truncation from both built-in and Cymbal adapters through the same result contract.
  - Pin `v0.18.0` `refs`/`impact`/`changed`/`show` JSON handling using `--no-federate` via a safe, bounded subprocess.
  - Handle stale, alias, and duplicate ambiguity explicitly.
  - Default to `builtin`, provide diagnostic fallback in `auto` mode, and surface an explicit error when `cymbal` is explicitly requested and fails.
- **Acceptance checks:**
  - Fake runner tests will verify `v0.18.0` `refs`/`impact`/`changed`/`show` JSON handling, `--no-federate`, bounded subprocess safety, stale/alias/duplicate ambiguity, `builtin`/`auto`/explicit `cymbal` behavior, and report propagation from both backends.
  - A pinned `v0.18.0` smoke test will validate Cymbal compatibility.
- **Oracle fetch passage ID:** `b49ccf962abe98bdd54642f9` (Repository Pattern)

## 4. Read bounded source ranges

- **Original task branch:** `codex/5-read-range`
- **Dependency:** Task 2 for native reads, Task 3 for Cymbal show
- **Reason:** `read_source` currently rejects files larger than 131072 bytes, and existing excerpts are limited to 700 characters.
- **Expected behavior:**
  - Return immutable, numbered slices via native `read_range(root, rel_path, start_line, end_line, *, max_lines=120, max_bytes=16384)` or validated Cymbal `show`.
  - Guard paths, binary inputs, and UTF-8 handling.
  - Provide exact provenance and truncation reporting.
- **Acceptance checks:**
  - Test reading line 4000 in a `>128KiB` file.
  - Measure bytes scanned vs. returned, net tokens, and coverage.
- **Oracle fetch passage ID:** `0284bd306f4d5bc7d5237220` (*Think Python*, p. 81)

## 5. Persist project facts

- **Original task branch:** `codex/5-project-facts`
- **Dependency:** Task 1 baseline
- **Reason:** `ChatSummary` may drop original conventions.
- **Expected Behavior:**
  - With `--facts`, persist confirmed records in the local worktree file `.aider.facts.sqlite3`, ignored by Git. Accept `--facts-db PATH` for an alternate location. With facts disabled, skip opening or creating the store.
  - Use Python’s embedded SQLite driver and FTS5. The standalone SQLite executable and a database server are optional. If the driver or FTS5 is missing, report that facts are disabled, preserve any existing database, and continue the Aider session. Do not silently save records to another store. Expose explicit recording via `/fact`.
  - Provide fake and SQLite FTS5 implementations (Repository Pattern) for `add`, `search`, and `list_for_paths`.
  - Track source hashes and invalidate entries on source change.
  - Keep model candidates marked unverified.
  - Enforce worktree isolation and bounded top-K results.
- **Acceptance Checks:**
  - Test disabled mode, default and custom paths, and a missing SQLite executable with a working Python driver. Simulate missing Python SQLite and missing FTS5 separately. Check the diagnostic, unchanged existing file, and absence of a fallback file.
  - Verify `add`, `search`, and `list_for_paths` against both fake and SQLite FTS5 backends.
  - Verify source-hash invalidation on source change, unverified model candidates, worktree isolation, and bounded top-K limits.
  - Record a decision, force it outside the retained chat tail, compact, and restart. Assert the original source hash and provenance survive. An edited or missing source must mark the fact stale.
- **Oracle Fetch Passage ID:** Repository Pattern (`b49ccf962abe98bdd54642f9`)

## 6. Verify the edited working tree

- **Original task branch:** `codex/5-verifier-integration`
- **Dependency:** Tasks 2–5 and the existing issue #7 gate
- **Reason:** Integrate verifier checks into the existing strict audit gate from issue 7 by reusing the hook and auditor with `--tdd` and the configured test command.
- **Expected behavior:**
  - Reuse the existing hook and auditor alongside `--tdd` and the configured test command.
  - Measure both Git `HEAD` and the edited working tree.
  - Keep snippets and facts read-only.
- **Acceptance checks:**
  - After lint and tests pass, a strict `FAIL` leaves the edit on disk and HEAD unchanged. A strict `PASS` permits committing only the exact audited patch.
  - Test `UNKNOWN`/`ERROR` and patch race cases.
  - Test advisory behavior and flags disabled.
  - Verify snippets and facts remain read-only.
- **Oracle fetch passage ID:** `0284bd306f4d5bc7d5237220` (Think Python, p. 81)

## 7. Measure retrieval and edit outcomes

- **Original task branch:** `codex/5-benchmark-report`
- **Dependency:** Tasks 1–6
- **Reason:** Baseline stands at 14/26, passive at 22/26, with seven false approvals across audit arms; controlled benchmarking is required prior to tuning.
- **Expected behavior:** Freeze paired real replays, evaluate the five original arms, then evaluate one additional factor at a time on pinned local quantized Qwen. Separate correct edits, audit verdicts, and commits; report intervals, p50/p95 token cost, and cold/warm/incremental retrieval metrics; justify control margin and power prior to tuning.
- **Acceptance checks:**
  - Confirm paired real replays are frozen before evaluating the five original arms and one additional factor at a time on pinned local quantized Qwen.
  - Confirm correct edits, audit verdicts, and commits are reported separately.
  - Confirm intervals, p50/p95 token cost, and cold/warm/incremental retrieval metrics are included.
  - Confirm control margin and power are justified prior to tuning.
- **Oracle fetch passage ID:** IR pp195,198 (`bdc9b70e51a5d943a9ab07a9` and `5ecfb939602f76b7331773f5`)

### Benchmark and release gates

Use the baseline, passive, contract, audit and contract+audit arms. Pin model/revision, sampling, prompts, repository-map budget and fixture versions. Randomize paired order, keep hidden checks outside the model-visible worktree, and predeclare the control tolerance, sample-size rationale and critical regressions. Report cold build, warm query and incremental update separately on small and large repositories, with location accuracy, recall, precision, wrong-target and ambiguity rates, CPU/RAM, index size and files reparsed. For edits, publish paired wins/losses with intervals, false approvals, UNKNOWNs, prompt/completion tokens, calls and p50/p95 latency. Run caller, audit, Oracle and TDD suites and project lint before closure. Record failures and retain opt-in defaults when findings are inconclusive.
