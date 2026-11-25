# Issue #5 Section C1 — Preregistration & Frozen 30-Task Polyglot Replay Corpus

This document freezes the experimental protocol, model/tool pins, statistical
analysis plan, non-inferiority bounds, and complete 30-task manifest for the
Issue #5 Section C1 unseen real-repository replay evaluation **prior to
running any LLM trial on this corpus**.

## 1. Frozen Freeze Record

| Field | Value |
| --- | --- |
| **Protocol Version** | `1.0` |
| **Preregistration ID** | `issue5-c1-polyglot-v1` |
| **Registered At** | `2026-11-25T16:40:00+02:00` |
| **Frozen** | `true` |
| **Canonical `manifest_sha256`** | `c00e46d1fb3b00895c02846625e69446ce40b3274de19091f9017c9779d0a429` |
| **External Manifest Path** | `/tmp/issue5-replay-corpus/manifest.json` |
| **Deterministic Builder** | [build_replay_manifest.py](../../scripts/local_context_trial/build_replay_manifest.py) |
| **Validator & Fixture Self-Check** | `replay_corpus.py validate --require-frozen --run-checks` (`0` errors, `30/30` cases) and `heldout.py --suite replay --check-fixtures` (`30/30` passed) |

### Methodological Grounding (Oracle)

- **Strict train/dev vs. unseen test separation (`ir-textbook`, p. 320, passage `069dc983a616649ad3adc765`; corroborated by p. 190, `2ddec6ce0616848e88839a07`):**
  During development, all prompt wording, caller heuristics, Cymbal adapter
  logic, bounded `read_range` thresholds, and verifier rules were tuned solely
  on development suites (`synthetic`, `requests`, `issue5`, and Section B
  deterministic fixtures). None of the 30 commits in this replay corpus were
  used during tuning. Once `manifest_sha256` is frozen, no parameter tuning,
  prompt editing, or case filtering on this corpus is permitted; the frozen
  protocol is executed and reported as-is.
- **Stratified heterogeneous evaluation & reproducibility (`beir-guide`, lines 414–438, passage `90905b05c622807ee5f8a78d`, Kamalloo et al., SIGIR '24):**
  Heterogeneous multi-domain/multi-language benchmarks suffer when outcomes are
  collapsed into a single opaque overall average or lack reproducible reference
  configurations. All results on this corpus **must** be reported stratified by
  task category (`direct_caller`, `indirect_caller`, `control`) and by language
  (7 languages) with paired effect sizes and confidence intervals, never solely
  as an undifferentiated 30-task aggregate.

## 2. Corpus Composition & Language Scope

- **Total frozen tasks:** `30` historical open-source bugfix commits (`base_commit = fix_commit~1`).
- **Category balance (exact 10 / 10 / 10):**
  - `direct_caller`: `10` tasks
  - `indirect_caller`: `10` tasks
  - `control`: `10` tasks (self-contained single-file bugs where caller context is not needed)
- **File scope:** `27` `single_file` tasks and `3` `multi_file` tasks (`py_packaging_invalid_interpreter_tag`, `py_pathspec_globstar_newline`, `js_dotenv_quiet_from_env`).
- **Difficulty tiers:** `14` `easy`, `14` `medium`, `2` `hard`.

| Language | Total | `direct_caller` | `indirect_caller` | `control` | `multi_file` | Host Verification Toolchain |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `python` | 6 | 1 | 3 | 2 | 2 | `.venv/bin/python` (CPython 3.12) |
| `javascript` | 4 | 1 | 1 | 2 | 1 | `node` (CommonJS / Node built-in `assert`) |
| `typescript` | 4 | 2 | 1 | 1 | 0 | `node` (native TypeScript type-stripping + module hooks) |
| `cpp` | 4 | 1 | 1 | 2 | 0 | `clang` / `clang++ -std=c++17` |
| `bash` | 4 | 1 | 2 | 1 | 0 | `bash` |
| `c` | 4 | 2 | 1 | 1 | 0 | `clang -std=c11` |
| `rust` | 4 | 2 | 1 | 1 | 0 | `rustc` (`~/.cargo/bin/rustc`) |
| **Total** | **30** | **10** | **10** | **10** | **3** | **7 executable languages** |

### Language Scope Exclusion Note

Seven executable languages are included (`python`, `javascript`, `typescript`,
`cpp`, `bash`, `c`, `rust`). Two languages considered during corpus planning
were explicitly excluded prior to freeze:

- **Java:** Excluded because OpenJDK (`javac`/`java`) was not installed on the
  evaluation host at freeze time, preventing offline fail-on-base / pass-on-fix
  execution verification.
- **Groovy:** Excluded because neither a local Groovy runtime nor `cymbal v0.18.0`
  indexing support for `.groovy` source files is available.

## 3. Pinned Environment, Models & Harness Parameters

| Parameter | Pinned Value |
| --- | --- |
| **Primary Model** | `lm_studio/qwen/qwen3.6-35b-a3b` (thinking MoE; amended prior to C2 data collection) |
| **Secondary 8B Model** | `lm_studio/meta-llama-3.1-8b-instruct` |
| **Inference Endpoint** | LM Studio OpenAI-compatible API at `http://127.0.0.1:1234/v1` |
| **Repo-Map Token Budget** | `--map-tokens 1024` (identical across all arms and stages) |
| **Pairing & Order Seed** | `--seed 20261123` (paired randomized arm/task execution order via `heldout.py`) |
| **Cymbal Version & Flags** | `cymbal v0.18.0` (`/opt/homebrew/bin/cymbal`, `--no-federate`) |
| **Default Specs** | Paired runs record both spec wording and exact arm metadata in JSONL + `.meta.json` |

> **Sampling Reproducibility Caveat:** Server-side sampling seeds **cannot be
> guaranteed pinned** if LM Studio omits or ignores the `seed` parameter on the
> served MLX/GGUF backend. Client-side job ordering (`--seed 20261123`), model
> identifiers, repo-map budgets (`--map-tokens 1024`), prompts, and fixture
> digests are strictly pinned; any residual decoding stochasticity from LM
> Studio is accounted for via paired randomized arm ordering and reported with
> exact discordant-pair counts and confidence intervals.

## 4. Experimental Design (Stage 1 & Stage 2)

Every pair shares `(case_id, spec, round_id)` with randomized arm execution
order seeded by `--seed 20261123` to neutralize server cache/thermal drift.

### Stage 1 — Five Baseline Context/Audit Arms (`builtin`, `read-range=off`, `facts=off`)

All five arms run with `--local-callers-backend builtin`,
`--no-local-callers-read-range`, and `--no-facts`:

1. `baseline`: `--no-local-callers`
2. `passive`: `--local-callers --local-callers-mode passive --no-local-callers-audit`
3. `contract`: `--local-callers --local-callers-mode contract --no-local-callers-audit`
4. `audit`: `--local-callers --local-callers-mode passive --local-callers-audit`
5. `contract+audit`: `--local-callers --local-callers-mode contract --local-callers-audit`

### Stage 2 — One-Factor-at-a-Time Ablations on `passive`

Starting from Stage 1 `passive` (`builtin`, `read-range=off`, `facts=off`),
each Issue #5 factor is toggled **individually** (never conflating multiple
factors in one jump):

1. **Cymbal Backend Factor:** `--local-callers-backend cymbal` (pinned `v0.18.0`), `read-range=off`, `facts=off` — isolates structural index retrieval impact across all 7 languages versus `builtin`.
2. **Read-Range Factor:** `--local-callers-read-range` (`read-range=on`), `builtin`, `facts=off` — isolates bounded call-site range extraction versus 700-character excerpts.
3. **Project Facts Factor:** `--facts` (`facts=on`), `builtin`, `read-range=off` — **Explicit Scope Note:** Because these 30 unseen historical OSS replay repositories have no pre-seeded `.aider.facts.jsonl` or SQLite facts store (`seed_facts=()`), `facts=on` on unseeded replay repos specifically measures **zero-regression and empty-store token/latency overhead**. Positive fact-retrieval lift is evaluated separately on the seeded `issue5` convention fixtures, not on unseeded replays.

## 5. Inclusion Criteria, Exclusion Criteria, Leakage Controls & Stopping Rule

### Inclusion Criteria

1. Genuine historical bugfix commit from an unseen compact open-source
   repository with `base_commit = fix_commit~1` and zero external network or
   package installation requirements at verification time.
2. Executable locally on host toolchains across the 7 covered languages.
3. `SmokeSpec` passes on both `base_commit` and `fix_commit`.
4. External `HiddenSpec` (`placement="hidden_dir"`, SHA-256 pinned) fails on
   `base_commit` and passes when only the declared `editable_files` from
   `fix_commit` are applied.
5. Editable source files fit comfortably inside local 8B and 30B context
   windows and were never inspected or used for feature/prompt tuning.

### Exclusion Criteria

1. Synthetic tasks or development fixtures previously used to tune prompts,
   caller heuristics, or verifier rules (`heldout.py` synthetic, `issue5`, and
   `requests` suites).
2. Languages lacking an executable host toolchain or Cymbal `v0.18.0` indexing
   at freeze time (Java, Groovy).
3. Commits requiring network access, heavy build systems, or external third-party
   test runners (`pytest` plugins, `mocha`, `tap`, etc.) at verification time.
4. Documentation-only, CI-only, formatting-only, or refactor commits without a
   clear fail-on-base / pass-on-fix behavioral regression.

### Leakage Controls

1. External git clones (`/tmp/issue5-replay-repos`) and hidden verification
   scripts (`/tmp/issue5-replay-corpus/hidden_*`) reside completely outside the
   `aider` repository and outside candidate workspaces.
2. Materialization excludes `tests/`, `docs/`, `benchmarks/`, and `examples/`
   via `SourcePin.include_globs` / `exclude_globs` so fix-commit unit tests and
   changelogs never enter candidate workspaces.
3. Every `HiddenFileSpec` uses `placement="hidden_dir"` with a pinned SHA-256
   digest and is staged inside an isolated temporary directory only after Aider
   exits.
4. Automated leakage guards in
   [build_replay_manifest.py](../../scripts/local_context_trial/build_replay_manifest.py)
   verify that `task_prompt` and `explicit_guidance` contain no unified diff
   markers (`diff --git`, `@@ `, `+++ b/`, `--- a/`), no commit SHAs, no
   external corpus/hidden file paths, and no test assertion snippets.

### Stopping Rule

Freeze the corpus at exactly `30` validated tasks (`10` `direct_caller`, `10`
`indirect_caller`, `10` `control` across 7 languages; `>= 3` `multi_file`)
before invoking any LLM in Stage 1 or Stage 2. No post-hoc task additions,
removals, category reassignments, or prompt rewrites are permitted after
`manifest_sha256` is frozen. Any environment/infrastructure failure during a
trial run is logged transparently as an error rather than silently dropped.

## 6. Metrics, Non-Inferiority Tolerance & Power Limits

### Primary Metric

- **`paired_hidden_pass_rate_delta`**: Paired change in external hidden-test
  pass rate between treatment and reference arms (`treatment - baseline` in
  Stage 1; `factor_on - passive` in Stage 2), analyzed with an **exact
  two-sided sign test on discordant pairs** and stratified separately for:
  - `direct_caller` (`n = 10`)
  - `indirect_caller` (`n = 10`)
  - combined caller-dependent (`n = 20`)
  - `control` (`n = 10`)
  - per-language strata (7 languages)

### Secondary Metrics (Recorded Separately — Never Conflated)

- **Edit vs. Audit vs. Commit separation:** `correct_edit_rate` (hidden check
  pass regardless of commit), `smoke_pass_rate`, `audit_verdict` breakdown
  (`PASS` / `FAIL` / `UNKNOWN` / `ERROR`), `false_approval_rate` (audit `PASS`
  on an edit that fails hidden checks), and `commit_rate`.
- **Token & Latency Economy:** `first_editor_prompt_tokens`,
  `cumulative_prompt_tokens`, `completion_tokens`, `total_tokens` (`p50` /
  `p95`), `tokens_per_solved_task`, `wall_clock_seconds` (`p50` / `p95`),
  `mean_editor_calls`, and `caller_evidence_attachment_rate`.

### Control Non-Inferiority Tolerance & Power / Sample-Size Limits (`n = 10`)

The `control` stratum has **`n = 10`** paired tasks per single-round sweep.
This sample size imposes strict statistical limits that must be respected in
all conclusions:

- **Explicit prohibition against claiming "zero degradation":** Observing `0/10`
  paired regressions on control tasks does **not** prove zero degradation
  (`p = 0`). Under an exact binomial model:
  - `0/10` regressions has an exact **one-sided 95% upper confidence bound of
    `25.89%`** (`1 - 0.05^(1/10) = 0.2589`) and an exact **two-sided 95%
    Clopper-Pearson interval of `[0.00%, 30.85%]`**.
  - `1/10` regressions has an exact **one-sided 95% upper confidence bound of
    `39.42%`**.
- **Detection power on `n = 10` control tasks:**

| True Control Regression Rate (`p`) | `P(>= 1 loss in 10)` | `P(>= 2 losses in 10)` |
| ---: | ---: | ---: |
| `5%` (`0.05`) | `40.1%` | `8.6%` |
| `10%` (`0.10`) | `65.1%` | `26.4%` |
| `15%` (`0.15`) | `80.3%` | `45.6%` |
| `20%` (`0.20`) | `89.3%` | `62.4%` |
| `25%` (`0.25`) | `94.4%` | `75.6%` |
| `30%` (`0.30`) | `97.2%` | `85.1%` |

- **Preregistered operational non-inferiority gate:**
  - **Tolerance margin:** An arm passes the `n = 10` control gate only if net
    paired control regressions are **`<= 1 / 10`** (`delta >= -10%` point
    estimate, i.e., `losses - wins <= 1`), with **zero** catastrophic failures
    (unhandled crashes, workspace corruption, or audit false approvals on
    control tasks). Observing `>= 2` net control regressions (`-20%` or worse)
    blocks default-on promotion.
  - **Reporting discipline:** Passing with `0/10` or `1/10` regressions must be
    reported strictly as *"within the preregistered `<= 1/10` operational
    control tolerance (one-sided 95% upper bound 25.9% / 39.4%)"*—never as
    proof of zero degradation. Tightening the upper bound below `5%` at 95%
    confidence with `0` regressions requires `n >= 59` independent control
    tasks.
- **Caller-dependent sign-test sensitivity (`n = 20`):** On the combined
  caller-dependent stratum (`10 direct_caller + 10 indirect_caller`), an exact
  two-sided sign test with `0` losses reaches `p < 0.05` starting at `6` paired
  wins (`p = 0.03125`) and `p < 0.01` starting at `8` paired wins
  (`p = 0.00781`). Per-stratum (`n = 10`) and per-language (`n = 4`–`6`)
  breakdowns are reported descriptively with exact discordant counts alongside
  the combined test.

## 7. Complete 30-Task Frozen Corpus Table

All 30 tasks below are frozen verbatim in `/tmp/issue5-replay-corpus/manifest.json`
under `manifest_sha256 = c00e46d1fb3b00895c02846625e69446ce40b3274de19091f9017c9779d0a429`:

| # | `case_id` | `repo` | `language` | `category` | `difficulty` | `file_scope` | `base_sha` | `fix_sha` |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `py_packaging_invalid_interpreter_tag` | `pypa/packaging` | `python` | `direct_caller` | `medium` | `multi_file` | `826426fc88773515fab149a5f6bde85761e57fbb` | `26fa1d42ccd98010d5fcf701dd10069e761b034c` |
| 2 | `py_pathspec_globstar_newline` | `cpburnz/python-pathspec` | `python` | `indirect_caller` | `hard` | `multi_file` | `5f14318a9e6fb91ee51d2b8b642b1a170fd49b3b` | `dacd8f9310627f9e4fcd71522d6d4a4bddc869bb` |
| 3 | `py_pathspec_gitignore_cr_trim` | `cpburnz/python-pathspec` | `python` | `indirect_caller` | `medium` | `single_file` | `47ea6a5b02bc968f8afe1f3b1a9cfa77a6493e4c` | `b84b01927e37624b19884f6c352214268f35aa65` |
| 4 | `py_dotenv_parser_crlf_recovery` | `theskumar/python-dotenv` | `python` | `indirect_caller` | `medium` | `single_file` | `0b2880591780a426b0551436f47a17e7a76d954f` | `bad5771ce54fd643538f177aab1465e4375e2039` |
| 5 | `py_click_show_envvar_sequence` | `pallets/click` | `python` | `control` | `medium` | `single_file` | `bb695aaecdef95832d167fc1d508921745aa824e` | `c53d3732b98c764932e146c007146ad402acb229` |
| 6 | `py_itsdangerous_future_timestamp` | `pallets/itsdangerous` | `python` | `control` | `easy` | `single_file` | `1a9b8d1f9968eb248bda69f39d373a6330b692ba` | `c30678d19e37011890e2374cca04f7789e101793` |
| 7 | `js_dotenv_quiet_from_env` | `motdotla/dotenv` | `javascript` | `direct_caller` | `medium` | `multi_file` | `d6b3a1dafaada5e2e2e28ef3c0f571d8e6877fa5` | `a654bc23257ad4ef35ed1787a035844c803ab722` |
| 8 | `js_semver_inc_identifier_validation` | `npm/node-semver` | `javascript` | `indirect_caller` | `hard` | `single_file` | `0864b3ce7932667013e0c7c5ec764777d4682883` | `8a34bdecc783407f4e1a8a1ee1f67906b84a4b78` |
| 9 | `js_dotenv_fast_bom` | `motdotla/dotenv` | `javascript` | `control` | `easy` | `single_file` | `c0e32b8267b69a438bc0cc31345f73b5e2f037db` | `bfb434360ffaf6cf53c7ca1f696165b0fbdf0d6c` |
| 10 | `js_ms_reject_infinity` | `vercel/ms` | `javascript` | `control` | `easy` | `single_file` | `2669f23e99be0bb6b65d365151884de52434301c` | `d95e17fd75a93708c8a3daf5bb07bf649cd5931c` |
| 11 | `ts_pathe_resolve_unc_prefix` | `unjs/pathe` | `typescript` | `direct_caller` | `medium` | `single_file` | `55a21be8a14b70ec67098d3f68f54a34f3796fd4` | `b52fcacc59717a1e4a8783d4bbfae7fa81b4db07` |
| 12 | `ts_ohash_serialize_map_keys` | `unjs/ohash` | `typescript` | `direct_caller` | `medium` | `single_file` | `6968b45c63d12f5c268e638a775aadb23a330279` | `3b225de3d991e74e3e34abff10431d96db72481a` |
| 13 | `ts_pathe_normalize_windows_drive` | `unjs/pathe` | `typescript` | `indirect_caller` | `easy` | `single_file` | `04a0aee4090e6cabfb15570398d6afff1184c215` | `cbd609509af084dac5cd5f22506dda75d51c5d13` |
| 14 | `ts_scule_is_uppercase` | `unjs/scule` | `typescript` | `control` | `easy` | `single_file` | `d2c281f10e12b8e33a2ed6ef3630eeb54de7de7c` | `57cfd1528a03720392604a2e582782fc481657dd` |
| 15 | `cpp_inih_multiline_no_value` | `benhoyt/inih` | `cpp` | `direct_caller` | `medium` | `single_file` | `96069a7039eac3dd0a6b2232b2e8ed18ab6c6ba7` | `2c6a6ac5f99b8cbea2dbfed67c00c15b1b03d3cd` |
| 16 | `cpp_fmt_dynamic_args_size` | `fmtlib/fmt` | `cpp` | `indirect_caller` | `medium` | `single_file` | `8dc5d3f69f15417d13c60a4a21f7298ed24db41c` | `854fc9567a2cbea01b0f413565705ccbaf5bde5c` |
| 17 | `cpp_fmt_printf_unsigned_sign` | `fmtlib/fmt` | `cpp` | `control` | `easy` | `single_file` | `8efd79f3f0f6648a35d185cf9b1f653f312c9eff` | `01bef95d345d4d3f730ee8c350c30ba52069ad13` |
| 18 | `cpp_semver_prerelease_zero` | `Neargye/semver` | `cpp` | `control` | `easy` | `single_file` | `8c72bf5cb4a13408eab768d554fdd5f66f8bc58d` | `14a86c62a31931ea2e7aa78b348dc9f7883b79c0` |
| 19 | `bash_bats_semaphore_exit_status` | `bats-core/bats-core` | `bash` | `direct_caller` | `easy` | `single_file` | `3781eabc113abcbe52421cb2f71659768b93fa62` | `87380dce0529ad586920837cbc996191c5587fbf` |
| 20 | `bash_bats_load_stack_trace` | `bats-core/bats-core` | `bash` | `indirect_caller` | `medium` | `single_file` | `19c9b04a8886a79c13710b54e2699dcc4a764bc8` | `7d98f6d8b668a59aa4494e897521a6d42e19429d` |
| 21 | `bash_rbenv_version_file_traversal` | `rbenv/rbenv` | `bash` | `indirect_caller` | `easy` | `single_file` | `a3fa9b73b8e6907845bdf47d2c2924c187580bdc` | `370c26a6c9ee0511972ea04904fcc89014a22987` |
| 22 | `bash_rbenv_init_shell_suffix` | `rbenv/rbenv` | `bash` | `control` | `easy` | `single_file` | `d604acb78aeba583be95f08d45eeae430372beb9` | `45951d2cbbdfb22fe069d5f935512335af27b8d2` |
| 23 | `c_yajl_parse_integer_non_digit` | `lloyd/yajl` | `c` | `direct_caller` | `medium` | `single_file` | `b0ea41643e411730b8411e282b229d98d5eb38b6` | `52d60b8f6a42e8b04ab2fa323ef338a9231d1b97` |
| 24 | `c_hiredis_map_elements_overflow` | `redis/hiredis` | `c` | `direct_caller` | `medium` | `single_file` | `e834b57f05871c05f7f13e461b78d20907aa3a02` | `af083b42bbfc7b72bd0292bb4ce6dacdc7125acb` |
| 25 | `c_yajl_decode_null_byte` | `lloyd/yajl` | `c` | `indirect_caller` | `medium` | `single_file` | `49116c941312b78ca6768b916fd0d247147f1f47` | `1df735562e13bca40f0960b1d9cdf5e7bc4a9dc5` |
| 26 | `c_tiny_regex_dot_newline` | `kokke/tiny-regex-c` | `c` | `control` | `easy` | `single_file` | `ef5deaeda453d314a454ca679678f0cfa4111910` | `e50673f7ced30f268e374ef2b974a3933966bab5` |
| 27 | `rust_semver_eval_less_prerelease` | `dtolnay/semver` | `rust` | `direct_caller` | `medium` | `single_file` | `35d918d334cdecc9725e217f460b90f478b5dabd` | `5742fc2f584dc14b46199d797de65305fe9b5144` |
| 28 | `rust_humantime_duration_nsec_carry_overflow` | `tailhook/humantime` | `rust` | `direct_caller` | `easy` | `single_file` | `fc092817fa8689298eaac28ff49bd8bede4ff605` | `ed7cc7dcd2566facfce2a1a6af8087993d1887c3` |
| 29 | `rust_json_print_dec_neg_exp` | `maciejhirsz/json-rust` | `rust` | `indirect_caller` | `medium` | `single_file` | `32ad6492c48eb2b4b186b8511a8ae72599c0b446` | `d38a183a4372f36e75ba8f5b8527a6a910b4dba7` |
| 30 | `rust_semver_display_formatter_flags` | `dtolnay/semver` | `rust` | `control` | `easy` | `single_file` | `9d801cf9da0cb4ecc77eb6c2c9a5769c4f76c055` | `e043c5e55d447b381ddfcaea7709edf18c46b855` |

## 8. Reproduction & Verification Commands

```bash
# 1. Deterministically assemble and freeze the 30-task manifest
.venv/bin/python scripts/local_context_trial/build_replay_manifest.py \
  --input-dir /tmp/issue5-replay-corpus \
  --out /tmp/issue5-replay-corpus/manifest.json

# 2. Verify schema, preregistration freeze digest, git pins, smoke & fail-to-pass hidden checks
.venv/bin/python scripts/local_context_trial/replay_corpus.py validate \
  /tmp/issue5-replay-corpus/manifest.json --require-frozen --run-checks

# 3. Verify heldout.py replay suite integration (smoke pass + hidden fail on base)
.venv/bin/python scripts/local_context_trial/heldout.py \
  --suite replay \
  --replay-manifest /tmp/issue5-replay-corpus/manifest.json \
  --check-fixtures
```
