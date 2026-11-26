# ReasoningBank genesis: Aider fork, 18–26 November 2026

> **Historical, evidence-governed bootstrap — not a trained memory or a claim of 75 successful repairs.** Scope: the 75 fork-specific commits reachable from feat/issue-5-b1-d1 at 5d1bf64, beginning with 7fbc92a9 on 18 Nov 2026, after upstream baseline 5dc9490. Commits inherited from upstream are excluded. Includes merged main commits and the still-unmerged Issue 5 research branch. Source is Git ancestry and commit metadata, selected code/test diffs, issues #5/#7/#13, the committed B1/C2 reports, and four user-uploaded pilot JSONL datasets. This is **retrospective reconstruction**, not a fabricated contemporaneous developer diary.

## Why this exists

The user asked for a commit-aware ReasoningBank from product genesis. A Git commit proves that a change was recorded, **not** that it fixed a reported failure, passed tests, or that the commit author held a specific rationale. Each episode separates **observed diff**, **measured input/output**, **inferred mechanism**, **verification**, **unresolved risk** and **reuse eligibility**. Never train on an arbitrary successful commit, on green smoke alone, or on a model-written explanation without independent evidence.

The objective is **fewer editor iterations until an independently accepted repair**, not higher output volume, more prompts, or more commits.

### Evidence and status vocabulary

- **OBSERVED_CHANGE**: code/docs changed and exact commit exists; intent and success are unproven.
- **TEST_BACKED**: executable assertions are committed, but execution on the cited SHA is not independently established.
- **MEASURED_PILOT**: a small replay with outcomes and model/environment metadata; not generalizable.
- **DIAGNOSED**: deterministic code path or forensic artifact strongly supports a cause; fix may not exist.
- **FIXED_UNRETESTED**: source fix exists, but paired post-fix independent replay is missing.
- **UNRESOLVED**: failure observed; no verified accepted fix.
- **HYPOTHESIS**: plausible causal account or strategy needing a discriminating test.
- **VERIFIED_REPAIR** (reserved): red/green or equivalent independent reproduction, relevant regressions, accepted commit and immutable evidence. **No entry below is automatically promoted to VERIFIED_REPAIR.**

### Proposed minimal reusable record

~~~yaml
episode_id: RB-008
scope: {repo: pavlosuperhero/aider, worktree: branch_ref, base_sha: pinned_sha}
problem: bounded read-only caller file re-entered full editable context
observed: {input: model edit reply mentioning consumer.py, output: "2 editor calls; 98181 prompt tokens"}
attempts: [{patch_hash: sha256_of_uncommitted_diff, outcome: FAIL_OR_UNKNOWN}]
change: {commit_sha: 7d6af8de, mechanism: preserve_edit_marker_and_exclude_read_only_sources}
checks:
  - {before: large_file.jsonl, after: large_file_after_fix.jsonl, status: MEASURED_PILOT}
why_it_worked: {claim: "Prevented silent /add of large referenced caller", status: DIAGNOSED}
reuse: {trigger: "large-file context spike after an edit response", gate: "check marker/excerpt provenance and noninteractive /add semantics"}
validity: {head_sha: 5d1bf64, recheck_on: [file_mention_logic_change, harness_change]}
~~~

## Commit-derived episodes (chronological causal clusters)

### RB-000 — Fork boundary and deliberate CI narrowing (18 Nov)

- **Evidence:** [7fbc92a9](https://github.com/pavlosuperhero/aider/commit/7fbc92a93d8529419f291d6c528e4e141ce854df) proposed upstream sync by PR; [ca073b80](https://github.com/pavlosuperhero/aider/commit/ca073b803e0c27f3ae249c519d0363fc2459ef9e) required owner review; [03a72a34](https://github.com/pavlosuperhero/aider/commit/03a72a342c11dfc19c9bcf4d50c2b872908c0d8b) and the following workflow-deletion commits removed inherited workflows, leaving sync as the intended automation.
- **Observed decision:** prioritize controlled upstream integration and owner approval; this is a product governance decision, not a repair episode.
- **Risk/inference:** deleting inherited test/release workflows also reduced automatic verification coverage. Later optional Oracle/Cymbal jobs do not establish that all normal code changes get regression testing. Check actual required checks before inferring CI quality.
- **Status:** OBSERVED_CHANGE; rationale inferred from code/messages; no Accepted@1 claim.
- **Reuse:** every new feature PR needs an explicit mandatory deterministic test gate; upstream sync stays a separate workflow.

### RB-001 — LiteLLM version drift and dependency hygiene (18–25 Nov)

- **Evidence:** [8f78b181](https://github.com/pavlosuperhero/aider/commit/8f78b181385895876d50937ae4ef71cc495cefa4) changed exception compatibility; [c574ade8](https://github.com/pavlosuperhero/aider/commit/c574ade83e979a147e92bbaa75f424bb5afbb3d4) plus [dcf91d3e](https://github.com/pavlosuperhero/aider/commit/dcf91d3e75682cda2ace3111bdce88f90bc7028d)/[2443aa32](https://github.com/pavlosuperhero/aider/commit/2443aa3255d345d534097d54f0bf5ee5cc6dc7c2) changed dependency floors; PR #2 merged at [853f64b8](https://github.com/pavlosuperhero/aider/commit/853f64b82e42e8c86f5ba088ed7d4fda8c8e7659); [1203d6a5](https://github.com/pavlosuperhero/aider/commit/1203d6a5893cd32b5597e9c7cbf8ec7b299d9da7) later handled LiteLLM 1.111.0 and strict=False.
- **Observed problem:** API/exception classes and pinned dependency constraints evolved independently; this caused repeated integration changes.
- **Why:** the compatibility boundary between model-serving library behavior and Aider's exception classifier was unstable. This is a source-grounded explanation, **not** verified failure reproduction for all environments.
- **Status:** TEST_BACKED/OBSERVED_CHANGE, causal certainty HYPOTHESIS.
- **Reuse:** adapter compatibility matrices + version-locked tests; never infer support solely from an updated pin.

### RB-002 — Oracle evidence must remain optional and source-grounded (18 Nov)

- **Evidence:** [2f9f73ec](https://github.com/pavlosuperhero/aider/commit/2f9f73ec2850d897d6eabc56c6df534f570a421a) introduced provider, CLI, docs and tests; [ea112de9](https://github.com/pavlosuperhero/aider/commit/ea112de9f6694496a87c8e7b7aff17f97e68a14e) tightened evidence grounding; PR #3 merged at [8448ce9f](https://github.com/pavlosuperhero/aider/commit/8448ce9f8af3e5bd2c5e9a7cf9767c36b42b7488).
- **Observed decision:** Oracle context is opt-in/read-only and citeable, with policy outside core editing.
- **Unproven claim:** general documentation injection does not by itself yield higher Accepted@1 or compliance with Cosmic Python/Think Python. No measured incremental benefit is established by this commit chain.
- **Status:** OBSERVED_CHANGE + TEST_BACKED; quality lift HYPOTHESIS.
- **Reuse:** retrieve only task-relevant, pinned guidance; compile enforceable constraints into independent checks.

### RB-003 — TDD/check hooks need actual red/green and completion gates (19 Nov)

- **Evidence:** [ea9c3283](https://github.com/pavlosuperhero/aider/commit/ea9c32832b0d7257dc516c6c2db80580e65b7091) response/test hooks → [f8a2a9af](https://github.com/pavlosuperhero/aider/commit/f8a2a9af84e29a759cdae06c373ca962c9c77b6a) TDD controller/edit hooks → [28a9255c](https://github.com/pavlosuperhero/aider/commit/28a9255c1083ed9d1d49d210bae71dbd8fe822cd) and [6961d04d](https://github.com/pavlosuperhero/aider/commit/6961d04dce22a31902220b5be72eeb27947b4189) grounding hardening → [f4968be1](https://github.com/pavlosuperhero/aider/commit/f4968be1464846d18f4cca970defedcd1782b5a2) verification/completion fixes; PR #4 merged at [d40c5c5c](https://github.com/pavlosuperhero/aider/commit/d40c5c5c1c4bda65dda242093df74b003f71b856).
- **Observed failure class:** a procedural instruction and a passing smoke run are insufficient proof that the requested behavior was repaired; checks must detect the original bug on the base revision.
- **Status:** TEST_BACKED, independent task-level lift UNKNOWN.
- **Reuse:** record red on base, green on candidate and relevant hidden checks; preserve test artifacts with exact revision IDs.

### RB-004 — Passive local caller context vs format/audit gates (23–24 Nov)

- **Evidence:** [09949d4e](https://github.com/pavlosuperhero/aider/commit/09949d4e6a0676ce84ff4abf7d51645a2e83ecab) first Python caller trial; [f57b6604](https://github.com/pavlosuperhero/aider/commit/f57b6604ffcf276e3ebd02b00613aed2423796ab) 30 paired results; [7bf46168](https://github.com/pavlosuperhero/aider/commit/7bf46168f89321c16cfac7e9aeac14c91080a1f2) cross-language contract/audit; [bf623d3f](https://github.com/pavlosuperhero/aider/commit/bf623d3fab1f1ddae3c1a6452af9756adf67328f) strict pre-commit gate; [a02fc4b5](https://github.com/pavlosuperhero/aider/commit/a02fc4b520a6c51b7bf03742956e43a054acaa7a) 130-run Qwen report; PR #6 merged at [d2f5965e](https://github.com/pavlosuperhero/aider/commit/d2f5965e1de69827d913214890548f79f8fd7a02).
- **Measured early result:** on a 26-case synthetic/Qwen3.6 subset, baseline 14/26 vs passive 22/26, contract 22/26, audit 22/26 and contract+audit 23/26 (as tracked in #13); this is not a frozen real-repo causal estimate.
- **Later contrary evidence:** larger 240-attempt C2 on 30 tasks showed passive 20/30 vs baseline 17/30 (non-significant); contract 3/30, audit 19/30. Earlier gains cannot override later diagnostic failures.
- **Status:** MEASURED_PILOT + DIAGNOSED, feature-shipping lift UNKNOWN.
- **Reuse:** pair by task/seed/commit and preserve unaffected controls; distinguish evidence-attachment strata.

### RB-005 — Auditor false confidence and contract as formatting ritual (24–26 Nov)

- **Evidence:** [bf623d3f](https://github.com/pavlosuperhero/aider/commit/bf623d3fab1f1ddae3c1a6452af9756adf67328f) introduced strict audit gate; [5cbcb7ef](https://github.com/pavlosuperhero/aider/commit/5cbcb7efbd39a6839d95c13d8646cd5a304855f4) C2 forensic: 8 actual audit-model calls all PASS, 7 wrong; 20 UNKNOWN were pre-LLM. [5d1bf640](https://github.com/pavlosuperhero/aider/commit/5d1bf640d6b36605595e105e77141727b3825c61) tightened auditor instruction text only.
- **Mechanism:** the contract hook requires headings/references, not semantic correctness; model PASS is a generated claim, not independent proof. Header prototypes and incomplete caller coverage worsen evidence sufficiency.
- **Status:** DIAGNOSED; calibration fix UNRESOLVED pending separately labeled dev-set results.
- **Reuse:** force UNKNOWN for insufficient direct caller use; test FAIL sensitivity and false approvals, not format compliance.

### RB-006 — Cymbal CLI/schema and caller semantics are versioned dependencies (24–26 Nov)

- **Evidence:** [44ad1c51](https://github.com/pavlosuperhero/aider/commit/44ad1c5178db9b0ea125d2a88b779c11008537aa) integration discovery, [ee31f97b](https://github.com/pavlosuperhero/aider/commit/ee31f97bc7764f9d9bf81a43ce60be7c91565fe7) adapter, [e4dc7957](https://github.com/pavlosuperhero/aider/commit/e4dc7957c851c7bb28753f26eb8f83a88246464e) live Cymbal v0.18.0 schema fix, [2b46e666](https://github.com/pavlosuperhero/aider/commit/2b46e66605b3fdcb74f27e107c1b5bb1bcc3f1ea) B1 deterministic retrieval, [5cbcb7ef](https://github.com/pavlosuperhero/aider/commit/5cbcb7efbd39a6839d95c13d8646cd5a304855f4) C2 forensic.
- **Observed split:** B1 found large-file discovery advantage; C2 Cymbal arm 12/30 vs passive 20/30, but 6/8 paired losses had no substantive caller-content difference, with 2/8 noisy snippets. C/C++ target extraction/isolated symbol errors and same-name/alias precision gaps remain.
- **Status:** DIAGNOSED, real-repo lift UNRESOLVED.
- **Reuse:** use explicit backend version, structural recall/precision by language, scope and API; do not turn a backend error into NO_CALLERS.

### RB-007 — Repo Facts storage is distinct from proven coding benefit (24–26 Nov)

- **Evidence:** [9e6a272f](https://github.com/pavlosuperhero/aider/commit/9e6a272f3dd01ec7e4b5a95751b5decba8e697ae) project facts and bounded ranges; [973f4257](https://github.com/pavlosuperhero/aider/commit/973f42573670068b4387015c4649ccc943df6201) default JSONL + optional SQLite; [730696c8](https://github.com/pavlosuperhero/aider/commit/730696c8e1f649b88e48e3cd408ec5a6888e6a44) file lock and Cymbal slice guard; [5cbcb7ef](https://github.com/pavlosuperhero/aider/commit/5cbcb7efbd39a6839d95c13d8646cd5a304855f4) unseeded facts arm.
- **Measured result:** unseeded facts on 30 C2 tasks attached 0/30 facts and scored 19/30 vs 20/30 passive, a **null-control** result; it cannot estimate Fact DB benefit. Focused seeded fact pilots show functionality, not general Accepted@1 lift.
- **Status:** OBSERVED_CHANGE + DIAGNOSED; benefit UNKNOWN.
- **Reuse:** verified typed facts + stale/supersession controls + separate accuracy and coding ablations. Never train a rationale from a model's own fact.

### RB-008 — Read-only file mention caused >93k avoidable prompt tokens (25 Nov)

- **Evidence:** [d8bedb14](https://github.com/pavlosuperhero/aider/commit/d8bedb1498cb98ac9dfa615be02a598bf1bfc7b2) token instrumentation, [7d6af8de](https://github.com/pavlosuperhero/aider/commit/7d6af8deac1ed662d6686726874e4fd5421768f7) fixed edit-marker preservation and read-only source exclusion; integration regression fixture added in same commit.
- **Concrete I/O:** large_file pilot before: Cymbal 3/3 hidden-pass, **2 editor calls / 98,181 prompt tokens / 143.7 s mean**; after: 3/3, **1 call / 5,183 tokens / 25.1 s**. Initial prompt in both was 5,183; the second call caused extra ~92,998 prompt tokens. Builtin was 0/3 both.
- **Mechanism:** code stripped SEARCH/REPLACE text before checking whether a response was an edit, so read-only consumer mention reached /add; DEVNULL auto-accepted the large file. Fixed marker and ignore rules prevent this path.
- **Status:** MEASURED_PILOT + DIAGNOSED; only this fixture verified, not whole-repo accepted improvement.
- **Reuse:** test no unintended full-file promotion after edits, before any model ablation.

### RB-009 — Evaluation framework is part of product behavior (25–26 Nov)

- **Evidence:** [f9ec75c5](https://github.com/pavlosuperhero/aider/commit/f9ec75c5908888f895916d1cc2a71900eb95e871) frozen 30-task/7-language C1 preregistration; [4436ff44](https://github.com/pavlosuperhero/aider/commit/4436ff4475eac946454cdceea660956224f7fbe9) C2 stats; [d0961912](https://github.com/pavlosuperhero/aider/commit/d09619123e47ae27d7a18535c5a62bdb84925ff4) replay coverage/commit classification; [4943f5db](https://github.com/pavlosuperhero/aider/commit/4943f5db188a958ba4753b37f0763095ce217dcd) 240-attempt report; [5cbcb7ef](https://github.com/pavlosuperhero/aider/commit/5cbcb7efbd39a6839d95c13d8646cd5a304855f4) forensic.
- **Observed:** 27/240 timeouts: 10 zero-call thinking runaways, 9 C++/Rust auto-lint reflections, 8 smoke reflections; 0 slow compile/test root causes. No-op facts arm flipped 7/30 outcomes (+3/-4), indicating substantial run-to-run discordance on this setup, **not** a universal noise floor.
- **Status:** DIAGNOSED; reproducible post-fix baseline UNRESOLVED.
- **Reuse:** pin code and environment, disable harness-only auto-lint, prevent implicit /add, control hash seed/backend sampling/output budget and repeat A/A comparisons.

### RB-010 — NO_EVIDENCE contract mode vetoes valid SEARCH/REPLACE (24–26 Nov)

- **Evidence:** [bf623d3f](https://github.com/pavlosuperhero/aider/commit/bf623d3fab1f1ddae3c1a6452af9756adf67328f) strict hook source, [5cbcb7ef](https://github.com/pavlosuperhero/aider/commit/5cbcb7efbd39a6839d95c13d8646cd5a304855f4) 21/21 no-evidence contract cases rejected edits, Stage1 contract 3/30; [5d1bf640](https://github.com/pavlosuperhero/aider/commit/5d1bf640d6b36605595e105e77141727b3825c61) hook and apply_updates pass-through fix.
- **Input/output:** valid edit + zero excerpts → old CallerContractHook returned blocking ResponseHookResult, BaseCoder.apply_updates discarded patch. New hook returns None; apply_updates proceeds.
- **Open regression risk:** contract-only strict + auto_commits may commit *before* lint/test without caller verification; Cymbal SEARCH_ERROR also becomes empty excerpts and pass-through. Three existing tests still assert old behavior; 5d1bf64 changed no tests.
- **Status:** root cause DIAGNOSED, implementation FIXED_UNRETESTED; accepted release repair NOT VERIFIED.
- **Reuse:** separate no-callers, incomplete, backend error, strict/advisory and audit on/off policies with explicit edit/commit expectations.

### RB-011 — A green smoke test can be unrelated to the requested behavior (25 Nov)

- **Evidence:** [Requests fixture](https://github.com/pavlosuperhero/aider/blob/5d1bf640d6b36605595e105e77141727b3825c61/scripts/local_context_trial/requests_cases.py) and uploaded requests_cymbal.jsonl: requests_json 0/3 builtin + 0/3 Cymbal; all six smoke PASS but hidden FAIL.
- **Concrete mismatch:** smoke asserts only that a GET Request can be prepared; hidden checks malformed JSON via Response.json() and pickle preservation of JSONDecodeError.msg/doc/pos. A patch used *e.args for constructor reconstruction even though args carries one formatted message, not (msg, doc, pos).
- **Status:** UNRESOLVED; log every attempted diff as a **failed hypothesis**, not a verified repair.
- **Reuse:** task-specific red/green check + independent holdout before counting Accepted@1 or collecting LoRA examples.

### RB-012 — Post-fix signal schema and acceptance semantics remain inconsistent (26 Nov)

- **Evidence:** [5d1bf640](https://github.com/pavlosuperhero/aider/commit/5d1bf640d6b36605595e105e77141727b3825c61) pass-through diagnostic and audit deduplication; [run.py](https://github.com/pavlosuperhero/aider/blob/5d1bf640d6b36605595e105e77141727b3825c61/scripts/local_context_trial/run.py) computes contract_rejected using substring matching, so **NO_EVIDENCE (pass-through)** is now falsely counted as rejected. Runner passed requires hidden+smoke+scope, **not** a strict auditor PASS or accepted commit.
- **Additional static code-path bug:** any fenced code explanation can trigger the contract hook's generic triple-backtick edit marker even without a patch, adding a needless reflection.
- **Status:** DIAGNOSED by static code path; new HEAD not yet independently run.
- **Reuse:** typed events and separate patch_hidden_pass@1, strict_accept@1 and committed@1. Test format-only and audit transitions under state changes.

## Repository-wide synthesis

### Observed sequence

1. **Governance first:** upstream sync + owner review; broad inherited CI removed.
2. **Context and process scaffolding:** optional Oracle, hooks, TDD controls, then caller evidence/contract/audit.
3. **More memory and retrieval:** Cymbal, ReadRange, Facts.
4. **Measurement catches product-contract bugs:** 130 pilot, 72 focused attempts, B1, C1 and 240 C2.
5. **Forensic feedback drives small fixes:** excess file-add, strict NO_EVIDENCE, audit/reflection logic.

### Three central lessons (HYPOTHESES to verify, not dogma)

- **A pipeline's control-flow is part of model accuracy.** Missing edits, silent /add and lint/reflection loops create losses that better prompting cannot repair.
- **Independent feedback quality dominates retry count.** Unrelated smoke tests allow ten wrong retries; six Requests JSON failures demonstrate why model-visible tests must be task-relevant.
- **Data provenance matters more than memory volume.** Seventy-five source commits are enough to bootstrap an event ledger, not a 75-example verified SFT set. Keep failed, ambiguous and inconclusive cases; reserve training for repeated independently accepted repairs.

## Reuse and training gate

A **retrieval-ready** episode may have a DIAGNOSED cause but must carry a warning and current-code applicability check. A **training-ready** positive repair requires: original task + base SHA + failed candidate patch hash(es) + immutable test observations + accepted patch/commit + independent red/green or heldout evidence + a causal claim whose source can be inspected. Negative episodes may train error detection only with honest labels and leakage controls. Recheck or invalidate when Aider caller discovery, edit hooks, benchmark harness or serving backend changes. Do not promote an episode solely because a commit merged or a local smoke turned green.

### External pilot artifact provenance (not committed in this PR)

Uploaded JSONL fingerprints, 34 rows total: large_file.jsonl (6, sha256 477380a8727875ba8eb1d72cbe04dedfe75e192be9fd6208b5548fc045836dc3), large_file_after_fix.jsonl (6, sha256 c89f9d154f2d8a21ee4e91585858e991f0b9a398469403155b9b668aefd9f37c), fast_post_fix.jsonl (10, sha256 c47dfd5f8a72737cf152f73551bfdf9f2a0abe3c516ce490bc878903b7d38c3b), requests_cymbal.jsonl (12, sha256 9a4ae3fa17cf0dc6fc815628fb10989333f4d2a1bfe4720e4a936cf172edfc02). Corresponding metadata declares git_dirty=true and seeds; that does **not** prove the inference endpoint honored a seed. Archive raw evidence separately with permissions before describing this as fully replayable.

## Complete fork-specific commit ledger (75 ancestry entries, oldest to newest)

This is an **event ledger**, not 75 verified repair episodes. Class is inferred from message prefix and may be imperfect. Merge commits and workflow deletions intentionally remain visible.

| # | Date (UTC) | Commit | Class | Recorded subject |
| ---: | --- | --- | --- | --- |
| 1 | 2026-11-18 | [7fbc92a9](https://github.com/pavlosuperhero/aider/commit/7fbc92a93d8529419f291d6c528e4e141ce854df) | CI/GOV | ci: propose weekly upstream sync via pull request |
| 2 | 2026-11-18 | [ca073b80](https://github.com/pavlosuperhero/aider/commit/ca073b803e0c27f3ae249c519d0363fc2459ef9e) | CI/GOV | chore: require owner review for all paths |
| 3 | 2026-11-18 | [03a72a34](https://github.com/pavlosuperhero/aider/commit/03a72a342c11dfc19c9bcf4d50c2b872908c0d8b) | CI/GOV | ci: keep fork limited to upstream sync workflow |
| 4 | 2026-11-18 | [c910c0e4](https://github.com/pavlosuperhero/aider/commit/c910c0e4af99beceaa95f210063d939c735bd5ea) | CI/GOV | ci: remove inherited check_pypi_version.yml workflow from fork |
| 5 | 2026-11-18 | [fffd67d0](https://github.com/pavlosuperhero/aider/commit/fffd67d0667cb72873fbc429571357e28a62fa87) | CI/GOV | ci: remove inherited pages.yml workflow from fork |
| 6 | 2026-11-18 | [a89a3bf6](https://github.com/pavlosuperhero/aider/commit/a89a3bf62d32b5b33bc1c38500035d99a543ba7e) | CI/GOV | ci: remove inherited docker-build-test.yml workflow from fork |
| 7 | 2026-11-18 | [b73f6832](https://github.com/pavlosuperhero/aider/commit/b73f683216aa8d0e21e6c2bdc10f73abfb19ae51) | CI/GOV | ci: remove inherited docker-release.yml workflow from fork |
| 8 | 2026-11-18 | [06ba44cd](https://github.com/pavlosuperhero/aider/commit/06ba44cd5ac4f2bf2648216a2e0227869dbff90e) | CI/GOV | ci: remove inherited pre-commit.yml workflow from fork |
| 9 | 2026-11-18 | [bac6f5b8](https://github.com/pavlosuperhero/aider/commit/bac6f5b8d7447387784d1bda2d0221a69d663d96) | CI/GOV | ci: remove inherited issues.yml workflow from fork |
| 10 | 2026-11-18 | [809cdf29](https://github.com/pavlosuperhero/aider/commit/809cdf2942c72bae728d19f41acfb99b83808812) | CI/GOV | ci: remove inherited release.yml workflow from fork |
| 11 | 2026-11-18 | [495b14e8](https://github.com/pavlosuperhero/aider/commit/495b14e8ee8d634e434273b88d5dcb0996a4d228) | CI/GOV | ci: remove inherited ubuntu-tests.yml workflow from fork |
| 12 | 2026-11-18 | [561fbb46](https://github.com/pavlosuperhero/aider/commit/561fbb46f9a01fa5a9ca0e21099732478fd96e1f) | CI/GOV | ci: remove inherited windows_check_pypi_version.yml workflow from fork |
| 13 | 2026-11-18 | [c016fad3](https://github.com/pavlosuperhero/aider/commit/c016fad37de450228ee032d667b207ee8c0b9e5c) | CI/GOV | ci: remove inherited windows-tests.yml workflow from fork |
| 14 | 2026-11-18 | [8f78b181](https://github.com/pavlosuperhero/aider/commit/8f78b181385895876d50937ae4ef71cc495cefa4) | FIX | fix: support newer LiteLLM exception classes |
| 15 | 2026-11-18 | [c574ade8](https://github.com/pavlosuperhero/aider/commit/c574ade83e979a147e92bbaa75f424bb5afbb3d4) | BUILD | build: relax compiled dependency pins and require LiteLLM 1.83.14 |
| 16 | 2026-11-18 | [dcf91d3e](https://github.com/pavlosuperhero/aider/commit/dcf91d3e75682cda2ace3111bdce88f90bc7028d) | BUILD | build: align requirements/requirements.in with LiteLLM 1.83.14 floor |
| 17 | 2026-11-18 | [2443aa32](https://github.com/pavlosuperhero/aider/commit/2443aa3255d345d534097d54f0bf5ee5cc6dc7c2) | BUILD | build: align requirements/common-constraints.txt with LiteLLM 1.83.14 floor |
| 18 | 2026-11-18 | [853f64b8](https://github.com/pavlosuperhero/aider/commit/853f64b82e42e8c86f5ba088ed7d4fda8c8e7659) | MERGE | Merge pull request #2 from pavlosuperhero/fix/litellm-183-exceptions-deps |
| 19 | 2026-11-18 | [2f9f73ec](https://github.com/pavlosuperhero/aider/commit/2f9f73ec2850d897d6eabc56c6df534f570a421a) | FEATURE | feat: add opt-in Oracle documentation context provider |
| 20 | 2026-11-18 | [ea112de9](https://github.com/pavlosuperhero/aider/commit/ea112de9f6694496a87c8e7b7aff17f97e68a14e) | FIX | fix: tighten Oracle evidence grounding and document trial |
| 21 | 2026-11-18 | [9472907d](https://github.com/pavlosuperhero/aider/commit/9472907d5f11216bea4174a55bff3cb400e5a686) | DOCS | docs: clarify Oracle operating instructions |
| 22 | 2026-11-18 | [eaef02d2](https://github.com/pavlosuperhero/aider/commit/eaef02d235c81d001b690a40dc2684b70d8ab326) | FIX | fix: keep automation limited to upstream sync |
| 23 | 2026-11-18 | [a3704a0a](https://github.com/pavlosuperhero/aider/commit/a3704a0ab1b3d5ff727b98de16e88cbad8dd8293) | CI/GOV | ci: separate Oracle tests from upstream synchronization |
| 24 | 2026-11-18 | [8448ce9f](https://github.com/pavlosuperhero/aider/commit/8448ce9f8af3e5bd2c5e9a7cf9767c36b42b7488) | MERGE | Merge pull request #3 from pavlosuperhero/codex/oracle-context-provider |
| 25 | 2026-11-18 | [5d4ae3f7](https://github.com/pavlosuperhero/aider/commit/5d4ae3f7d8f4c9f1b9aed9a6a8bb3208c74a3905) | TEST | test: prepare Oracle quality task and evaluation guide |
| 26 | 2026-11-19 | [ea9c3283](https://github.com/pavlosuperhero/aider/commit/ea9c32832b0d7257dc516c6c2db80580e65b7091) | FEATURE | Add optional response and test-result hooks for Oracle workflow |
| 27 | 2026-11-19 | [f8a2a9af](https://github.com/pavlosuperhero/aider/commit/f8a2a9af84e29a759cdae06c373ca962c9c77b6a) | FEATURE | Add optional TDD workflow controller, edit hooks, and citation grounding |
| 28 | 2026-11-19 | [28a9255c](https://github.com/pavlosuperhero/aider/commit/28a9255c1083ed9d1d49d210bae71dbd8fe822cd) | OTHER | Harden TDD workflow and Oracle search parsing for small-model trials |
| 29 | 2026-11-19 | [6961d04d](https://github.com/pavlosuperhero/aider/commit/6961d04dce22a31902220b5be72eeb27947b4189) | FEATURE | Add TddConfig.require_citation and harden TDD/Oracle code-block handling |
| 30 | 2026-11-19 | [0c9894b9](https://github.com/pavlosuperhero/aider/commit/0c9894b9712a5469b848d2d229b375edf5f029a2) | OTHER | Pin tree-sitter-language-pack==0.13.0 and skip empty RepoMap parser loads |
| 31 | 2026-11-19 | [8f78a1dc](https://github.com/pavlosuperhero/aider/commit/8f78a1dc09aac76a9abb86a27a64917ca0fef849) | FEATURE | Add default TDD globs, test-cmd resolution, flag overrides, and setup guide |
| 32 | 2026-11-19 | [5e5cd6b6](https://github.com/pavlosuperhero/aider/commit/5e5cd6b6d95a8b24c24fb8438f88fedf3f212251) | OTHER | Resolve default oracle.md and tdd.py paths from Aider repo with flag overrides |
| 33 | 2026-11-19 | [f4968be1](https://github.com/pavlosuperhero/aider/commit/f4968be1464846d18f4cca970defedcd1782b5a2) | FIX | Fix TDD verification and completion gates |
| 34 | 2026-11-19 | [d40c5c5c](https://github.com/pavlosuperhero/aider/commit/d40c5c5c1c4bda65dda242093df74b003f71b856) | MERGE | Merge pull request #4 from pavlosuperhero/test/aider-oracle-quality |
| 35 | 2026-11-20 | [5e7db481](https://github.com/pavlosuperhero/aider/commit/5e7db481d7485958a07d853dee1773704874e80f) | FEATURE | Added sonnet 5.5 and haiku to litllm |
| 36 | 2026-11-20 | [5f460de8](https://github.com/pavlosuperhero/aider/commit/5f460de83e4179e6a71de53e80ccd31c59f53c8f) | FIX | Fixing caching problem |
| 37 | 2026-11-23 | [09949d4e](https://github.com/pavlosuperhero/aider/commit/09949d4e6a0676ce84ff4abf7d51645a2e83ecab) | FEATURE | Trial read-only local Python caller evidence with paired Qwen tasks |
| 38 | 2026-11-23 | [f57b6604](https://github.com/pavlosuperhero/aider/commit/f57b6604ffcf276e3ebd02b00613aed2423796ab) | FEATURE | Attach local caller evidence to the task and record 30 paired trial runs |
| 39 | 2026-11-23 | [0992ae50](https://github.com/pavlosuperhero/aider/commit/0992ae50d50f7cc862985185e92c61dcea740fc2) | FEATURE | Added local context trial |
| 40 | 2026-11-23 | [7bf46168](https://github.com/pavlosuperhero/aider/commit/7bf46168f89321c16cfac7e9aeac14c91080a1f2) | FEATURE | feat: add language-agnostic local callers, contract hook, and CoVe audit |
| 41 | 2026-11-23 | [aecdcf11](https://github.com/pavlosuperhero/aider/commit/aecdcf119e51836b91ddc0a8de59547ff3e208a9) | BENCH | bench: add multi-language held-out caller trial and Qwen 30B results |
| 42 | 2026-11-24 | [bf623d3f](https://github.com/pavlosuperhero/aider/commit/bf623d3fab1f1ddae3c1a6452af9756adf67328f) | FEATURE | feat: add pre-commit caller audit gate, strict policy, and qwen3.6-35b-a3b model config |
| 43 | 2026-11-24 | [11bb2466](https://github.com/pavlosuperhero/aider/commit/11bb246661483106ea4896eeda6c2f18e50cbbd8) | FIX | fix: scope JS import ambiguity check, harden verdict/contract parsing, and add audit retries |
| 44 | 2026-11-24 | [a02fc4b5](https://github.com/pavlosuperhero/aider/commit/a02fc4b520a6c51b7bf03742956e43a054acaa7a) | BENCH | bench: record 130-run Qwen 3.6 35B trial results and update issue #7 report |
| 45 | 2026-11-24 | [d2f5965e](https://github.com/pavlosuperhero/aider/commit/d2f5965e1de69827d913214890548f79f8fd7a02) | MERGE | Merge pull request #6 from pavlosuperhero/feat/local-context-trial |
| 46 | 2026-11-24 | [8bb0f008](https://github.com/pavlosuperhero/aider/commit/8bb0f008e68ac4cc5194156d35604f6a04adb786) | DOCS | docs: track issue 5 iterations and verified caller limitations |
| 47 | 2026-11-24 | [1153e7da](https://github.com/pavlosuperhero/aider/commit/1153e7da358d2b3bbb665669ab3b4da3923d3ada) | DOCS | docs: consolidate issue 5 plan and specify local facts storage |
| 48 | 2026-11-24 | [9e6a272f](https://github.com/pavlosuperhero/aider/commit/9e6a272f3dd01ec7e4b5a95751b5decba8e697ae) | FEATURE | feat: add bounded source ranges and local project facts |
| 49 | 2026-11-24 | [44ad1c51](https://github.com/pavlosuperhero/aider/commit/44ad1c5178db9b0ea125d2a88b779c11008537aa) | FEATURE | feat: add Cymbal discovery and extend the local context trial |
| 50 | 2026-11-24 | [ecd4a99b](https://github.com/pavlosuperhero/aider/commit/ecd4a99be9db1ff789e6517acd5f6296a283886b) | FEATURE | feat: expose scoped caller evidence and injectable discovery |
| 51 | 2026-11-24 | [ee31f97b](https://github.com/pavlosuperhero/aider/commit/ee31f97bc7764f9d9bf81a43ce60be7c91565fe7) | FEATURE | feat: integrate Cymbal callers, read-range, and project facts |
| 52 | 2026-11-24 | [7eba03c9](https://github.com/pavlosuperhero/aider/commit/7eba03c914a476760c8675b6f657f8f2ea2207b3) | FEATURE | feat: wire replay suite CLI, fix overlay cleanup, and update issue 5 tracker |
| 53 | 2026-11-25 | [973f4257](https://github.com/pavlosuperhero/aider/commit/973f42573670068b4387015c4649ccc943df6201) | FEATURE | feat: store project facts in JSONL with chat hash ID by default and SQLite via --facts-db |
| 54 | 2026-11-25 | [e4dc7957](https://github.com/pavlosuperhero/aider/commit/e4dc7957c851c7bb28753f26eb8f83a88246464e) | FIX | fix: align Cymbal callers and read-range validator with live v0.18.0 JSON |
| 55 | 2026-11-25 | [1d2f228c](https://github.com/pavlosuperhero/aider/commit/1d2f228c4de29e55996e47f1744a9a726dc055b9) | FEATURE | feat: add issue5 trial suite, facts seeding, and economy/efficiency preset |
| 56 | 2026-11-25 | [a6275692](https://github.com/pavlosuperhero/aider/commit/a62756928e368f787f9f06838f84fa423844e773) | FIX | fix: hand off architect turn evidence, exempt non-editing coders, drop ranges, and alias /read |
| 57 | 2026-11-25 | [ca4144e3](https://github.com/pavlosuperhero/aider/commit/ca4144e3650873e9c837eae9e3f36caf0917ec25) | FIX | fix: accurately report architect dry-run and contract-vetoed outcomes |
| 58 | 2026-11-25 | [d8bedb14](https://github.com/pavlosuperhero/aider/commit/d8bedb1498cb98ac9dfa615be02a598bf1bfc7b2) | FEATURE | feat: log editor/audit prompt tokens and add 72-attempt issue 5 input trial |
| 59 | 2026-11-25 | [7d6af8de](https://github.com/pavlosuperhero/aider/commit/7d6af8deac1ed662d6686726874e4fd5421768f7) | FIX | fix: preserve edit marker before stripping in check_for_file_mentions and center auto read-ranges |
| 60 | 2026-11-25 | [a4732bbc](https://github.com/pavlosuperhero/aider/commit/a4732bbcb373ee25a02147c1f3f25fa993182868) | DOCS | docs: record final Issue 5 verification and Qwen trial results in tracker |
| 61 | 2026-11-25 | [1203d6a5](https://github.com/pavlosuperhero/aider/commit/1203d6a5893cd32b5597e9c7cbf8ec7b299d9da7) | FIX | fix: support LiteLLM 1.111.0 exceptions and honor strict=False in LiteLLMExceptions._load |
| 62 | 2026-11-25 | [f90135b1](https://github.com/pavlosuperhero/aider/commit/f90135b1c78d228ffa117adfe9acff7951427191) | MERGE | Merge pull request #12 from pavlosuperhero/feat/caller-context-cymbal-readrange-facts |
| 63 | 2026-11-25 | [2b46e666](https://github.com/pavlosuperhero/aider/commit/2b46e66605b3fdcb74f27e107c1b5bb1bcc3f1ea) | BENCH | bench(issue-5): add Section B deterministic no-LLM retrieval benchmark and report |
| 64 | 2026-11-25 | [64d2d2ca](https://github.com/pavlosuperhero/aider/commit/64d2d2caf78c0206572ff1a66fa69220a285a771) | DOCS | docs(issue-5): add local-context user guide and optional Cymbal v0.18.0 CI smoke workflow |
| 65 | 2026-11-25 | [f9ec75c5](https://github.com/pavlosuperhero/aider/commit/f9ec75c5908888f895916d1cc2a71900eb95e871) | BENCH | bench(issue-5): freeze 30-task 7-language replay corpus and preregistration (C1) |
| 66 | 2026-11-25 | [ac5fcd65](https://github.com/pavlosuperhero/aider/commit/ac5fcd65107338de9257bc3141b6f80047ae59ef) | DOCS | docs(issue-5): amend C1 preregistration primary model to lm_studio/qwen/qwen3.6-35b-a3b prior to C2 |
| 67 | 2026-11-25 | [4436ff44](https://github.com/pavlosuperhero/aider/commit/4436ff4475eac946454cdceea660956224f7fbe9) | BENCH | bench(issue-5): add C2 stratified statistical analyzer and report generator |
| 68 | 2026-11-25 | [90981e11](https://github.com/pavlosuperhero/aider/commit/90981e11d2d2c1caab2ac4be80a55a4f916c6b0e) | DOCS | docs(local-context): add monolith-to-libraries refactoring playbook grounded in Oracle Prompt Engineering & Clean Architecture |
| 69 | 2026-11-25 | [fca795b2](https://github.com/pavlosuperhero/aider/commit/fca795b21a6045665bbf8ade77e5a57d7ad910e2) | BENCH | bench(issue-5): checkpoint first 42 C2 replay attempts |
| 70 | 2026-11-25 | [730696c8](https://github.com/pavlosuperhero/aider/commit/730696c8e1f649b88e48e3cd408ec5a6888e6a44) | FIX | fix(issue-5): serialize JSONL fact writes with FileLock and reject mismatched Cymbal slices (P1 #1, P2 #3) |
| 71 | 2026-11-25 | [d0961912](https://github.com/pavlosuperhero/aider/commit/d09619123e47ae27d7a18535c5a62bdb84925ff4) | FIX | fix(issue-5): enforce frozen manifest coverage, separate commit outcomes, harden replay loop, and match paired timeouts (P1 #2, P2 #4-6) |
| 72 | 2026-11-25 | [b2e6ca39](https://github.com/pavlosuperhero/aider/commit/b2e6ca399ad57c1654d170421fa79a698b4d5acd) | REFACTOR | refactor(issue-5): apply Oracle-grounded cleanups (_locked_write chaining, _build_error_row, commit_passed reuse) |
| 73 | 2026-11-26 | [4943f5db](https://github.com/pavlosuperhero/aider/commit/4943f5db188a958ba4753b37f0763095ce217dcd) | BENCH | bench(issue-5): publish complete 240-attempt Qwen 3.6 35B-A3B C2 replay benchmark report |
| 74 | 2026-11-26 | [5cbcb7ef](https://github.com/pavlosuperhero/aider/commit/5cbcb7efbd39a6839d95c13d8646cd5a304855f4) | DOCS | docs(issue-5): publish Oracle-grounded forensic research report on all 240 C2 replay trials |
| 75 | 2026-11-26 | [5d1bf640](https://github.com/pavlosuperhero/aider/commit/5d1bf640d6b36605595e105e77141727b3825c61) | OTHER | Pushing latest changes |

---

Maintained under [Issue #13](https://github.com/pavlosuperhero/aider/issues/13). This file is a historical bootstrap; future entries should be generated incrementally at high-signal failure→accepted-repair transitions with stable artifact references.
