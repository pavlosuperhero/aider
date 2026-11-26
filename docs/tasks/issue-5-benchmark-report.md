# Issue #5 Section C2 — Stratified Replay Evaluation Report

> **Methodological Grounding:** Strict separation of development tuning from the frozen unseen test corpus (`ir-textbook`, p. 320, `069dc983a616649ad3adc765`), and mandatory stratified reporting by task category and language rather than collapsing heterogeneous strata into one opaque average (`beir-guide`, lines 414-438, `90905b05c622807ee5f8a78d`).

## 1. Frozen Manifest Verification

| Field | Value |
| --- | --- |
| **Manifest Path** | `/private/tmp/issue5-replay-corpus/manifest.json` |
| **Preregistration ID** | `issue5-c1-polyglot-v1` |
| **Protocol Version** | `1.0` |
| **Registered At** | `2026-11-25T16:40:00+02:00` |
| **Frozen** | `true` |
| **Declared `manifest_sha256`** | `c00e46d1fb3b00895c02846625e69446ce40b3274de19091f9017c9779d0a429` |
| **Computed `manifest_sha256`** | `c00e46d1fb3b00895c02846625e69446ce40b3274de19091f9017c9779d0a429` |
| **Digest Verified** | `PASS` |
| **Total Tasks** | `30` |
| **Category Balance** | `direct_caller=10, indirect_caller=10, control=10` |
| **Language Balance** | `python=6, javascript=4, typescript=4, cpp=4, bash=4, c=4, rust=4` |

## 2. Run Metadata & Provenance

| Run Label | Model | Git Commit | Python | Seed | `map_tokens` | Cymbal | Rows |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| `qwen/qwen3.6-35b-a3b` | `lm_studio/qwen/qwen3.6-35b-a3b` | `ac5fcd651073, b2e6ca399ad5 (dirty)` | `3.12.13` | `20261123` | `1024` | `cymbal v0.18.0` | 240 |

## 3.1. Model Evaluation: `qwen/qwen3.6-35b-a3b`

### Stage 1 — Five Context/Audit Arms (`builtin`, `read-range=off`, `facts=off`)

_Aggregate 30-task metrics are descriptive context only; interpret jointly with the stratified Category and Language tables below._

| Arm | Hidden Pass | Wilson 95% CI | Paired vs Ref (+w/-l) | Diff (Newcombe 95% CI) | McNemar p | False Approvals | UNKNOWN Verdicts | Mean 1st-Prompt | Mean Cum-Prompt | Mean Compl | Mean Calls / Refl | Latency p50 / p95 (s) | Timeouts (New) |
| --- | --- | --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 17/30 (56.7%) | [39.2%, 72.6%] | ref | ref | ref | 0/0 (n/a) | 0/0 (n/a) | 11381 (29/30) | 16821 (29/30) | 6170 (29/30) | 1.30 / 0.33 | 109.8 / 361.7 | 3 |
| `passive` | 20/30 (66.7%) | [48.8%, 80.8%] | +4/-1 (n=30) | +10.0pp [-5.3pp, +24.5pp] | 0.3750 | 0/0 (n/a) | 0/0 (n/a) | 11271 | 16659 | 6566 | 1.30 / 0.30 | 117.1 / 361.4 | 3 (new=1) |
| `contract` | 3/30 (10.0%) | [3.5%, 25.6%] | +1/-15 (n=30) | -46.7pp [-63.9pp, -23.2pp] | 0.0005 | 0/0 (n/a) | 0/0 (n/a) | 11482 (29/30) | 12299 (29/30) | 6120 (29/30) | 1.03 / 0.07 | 98.0 / 329.3 | 2 (new=0) |
| `audit` | 19/30 (63.3%) | [45.5%, 78.1%] | +3/-1 (n=30) | +6.7pp [-7.3pp, +20.2pp] | 0.6250 | 3/24 (12.5%) | 20/24 (83.3%) | 11281 | 16698 | 7521 | 1.37 / 0.37 | 124.8 / 361.8 | 3 (new=2) |
| `contract+audit` | 3/30 (10.0%) | [3.5%, 25.6%] | +1/-15 (n=30) | -46.7pp [-63.9pp, -23.2pp] | 0.0005 | 4/7 (57.1%) | 3/7 (42.9%) | 11576 (28/30) | 13761 (28/30) | 6494 (28/30) | 1.07 / 0.13 | 124.5 / 348.3 | 2 (new=1) |

> **Matched-Pair Timeout Regressions:** `passive` (new_timeouts=1), `audit` (new_timeouts=2), `contract+audit` (new_timeouts=1)

#### Stage 1 — Stratified by Category (Never Collapsing Heterogeneous Strata)

| Stratum | n | `baseline` (ref) | `passive` (pass; +w/-l, p) | `contract` (pass; +w/-l, p) | `audit` (pass; +w/-l, p) | `contract+audit` (pass; +w/-l, p) |
| --- | ---: | --- | --- | --- | --- | --- |
| `direct_caller` | 10 | 5/10 (50.0%) | 6/10 (60.0%); +2/-1 (+10.0pp, p=1.0000) | 2/10 (20.0%); +0/-3 (-30.0pp, p=0.2500) | 5/10 (50.0%); +1/-1 (+0.0pp, p=1.0000) | 2/10 (20.0%); +0/-3 (-30.0pp, p=0.2500) |
| `indirect_caller` | 10 | 5/10 (50.0%) | 5/10 (50.0%); +0/-0 (+0.0pp, p=1.0000) | 1/10 (10.0%); +1/-5 (-40.0pp, p=0.2188) | 6/10 (60.0%); +1/-0 (+10.0pp, p=1.0000) | 1/10 (10.0%); +1/-5 (-40.0pp, p=0.2188) |
| `caller_dependent` | 20 | 10/20 (50.0%) | 11/20 (55.0%); +2/-1 (+5.0pp, p=1.0000) | 3/20 (15.0%); +1/-8 (-35.0pp, p=0.0391) | 11/20 (55.0%); +2/-1 (+5.0pp, p=1.0000) | 3/20 (15.0%); +1/-8 (-35.0pp, p=0.0391) |
| `control` | 10 | 7/10 (70.0%) | 9/10 (90.0%); +2/-0 (+20.0pp, p=0.5000) | 0/10 (0.0%); +0/-7 (-70.0pp, p=0.0156) | 8/10 (80.0%); +1/-0 (+10.0pp, p=1.0000) | 0/10 (0.0%); +0/-7 (-70.0pp, p=0.0156) |

#### Stage 1 — Stratified by Language (All Covered Languages)

| Stratum | n | `baseline` (ref) | `passive` (pass; +w/-l, p) | `contract` (pass; +w/-l, p) | `audit` (pass; +w/-l, p) | `contract+audit` (pass; +w/-l, p) |
| --- | ---: | --- | --- | --- | --- | --- |
| `python` | 6 | 4/6 (66.7%) | 5/6 (83.3%); +1/-0 (+16.7pp, p=1.0000) | 1/6 (16.7%); +0/-3 (-50.0pp, p=0.2500) | 4/6 (66.7%); +0/-0 (+0.0pp, p=1.0000) | 1/6 (16.7%); +0/-3 (-50.0pp, p=0.2500) |
| `javascript` | 4 | 2/4 (50.0%) | 3/4 (75.0%); +1/-0 (+25.0pp, p=1.0000) | 0/4 (0.0%); +0/-2 (-50.0pp, p=0.5000) | 2/4 (50.0%); +0/-0 (+0.0pp, p=1.0000) | 0/4 (0.0%); +0/-2 (-50.0pp, p=0.5000) |
| `typescript` | 4 | 1/4 (25.0%) | 2/4 (50.0%); +1/-0 (+25.0pp, p=1.0000) | 0/4 (0.0%); +0/-1 (-25.0pp, p=1.0000) | 2/4 (50.0%); +1/-0 (+25.0pp, p=1.0000) | 0/4 (0.0%); +0/-1 (-25.0pp, p=1.0000) |
| `cpp` | 4 | 3/4 (75.0%) | 3/4 (75.0%); +0/-0 (+0.0pp, p=1.0000) | 1/4 (25.0%); +1/-3 (-50.0pp, p=0.6250) | 4/4 (100.0%); +1/-0 (+25.0pp, p=1.0000) | 1/4 (25.0%); +1/-3 (-50.0pp, p=0.6250) |
| `bash` | 4 | 3/4 (75.0%) | 3/4 (75.0%); +0/-0 (+0.0pp, p=1.0000) | 0/4 (0.0%); +0/-3 (-75.0pp, p=0.2500) | 3/4 (75.0%); +0/-0 (+0.0pp, p=1.0000) | 0/4 (0.0%); +0/-3 (-75.0pp, p=0.2500) |
| `c` | 4 | 3/4 (75.0%) | 2/4 (50.0%); +0/-1 (-25.0pp, p=1.0000) | 0/4 (0.0%); +0/-3 (-75.0pp, p=0.2500) | 2/4 (50.0%); +0/-1 (-25.0pp, p=1.0000) | 0/4 (0.0%); +0/-3 (-75.0pp, p=0.2500) |
| `rust` | 4 | 1/4 (25.0%) | 2/4 (50.0%); +1/-0 (+25.0pp, p=1.0000) | 1/4 (25.0%); +0/-0 (+0.0pp, p=1.0000) | 2/4 (50.0%); +1/-0 (+25.0pp, p=1.0000) | 1/4 (25.0%); +0/-0 (+0.0pp, p=1.0000) |

### Stage 2 — One-Factor-at-a-Time Ablations vs. Stage 1 `passive`

> **Scope Note (`facts=on`):** Unseeded replay repositories have no pre-seeded facts store (`seed_facts=()`), so `facts=on` here measures zero-regression and empty-store token/latency overhead only. Positive fact-retrieval lift is evaluated separately on seeded convention suites.

| Factor | Hidden Pass | Wilson 95% CI | Paired vs `passive` (+w/-l) | Diff (Newcombe 95% CI) | McNemar p | Paired 1st-Prompt Delta | Paired Total-Tok Delta | Paired Latency Delta | Latency p50 / p95 (s) | Timeouts (New) |
| --- | --- | --- | --- | --- | ---: | --- | --- | --- | ---: | ---: |
| `passive (Stage 1 ref)` | 20/30 (66.7%) | [48.8%, 80.8%] | ref | ref | ref | ref | ref | ref | 117.1 / 361.4 | 3 |
| `cymbal (backend=cymbal)` | 12/30 (40.0%) | [24.6%, 57.7%] | +0/-8 (n=30) | -26.7pp [-41.4pp, -9.0pp] | 0.0078 | +0.1% (29/30, to=4) | -0.7% (29/30, to=4) | -3.1% (30/30, to=4) | 98.9 / 361.5 | 3 (new=1) |
| `read-range=on` | 17/30 (56.7%) | [39.2%, 72.6%] | +3/-6 (n=30) | -10.0pp [-28.8pp, +10.0pp] | 0.5078 | +0.8% (29/30, to=6) | -4.2% (29/30, to=6) | +2.9% (30/30, to=6) | 97.5 / 362.5 | 5 (new=3) |
| `facts=on` | 19/30 (63.3%) | [45.5%, 78.1%] | +3/-4 (n=30) | -3.3pp [-20.9pp, +14.5pp] | 1.0000 | +0.1% (26/30, to=7) | -12.3% (26/30, to=7) | +7.4% (30/30, to=7) | 102.7 / 362.6 | 6 (new=4) |

> **Matched-Pair Timeout Regressions:** `cymbal (backend=cymbal)` (new_timeouts=1), `read-range=on` (new_timeouts=3), `facts=on` (new_timeouts=4)

#### Stage 2 — Stratified by Category

| Stratum | n | `passive` (ref) | `cymbal (backend=cymbal)` (pass; +w/-l, p) | `read-range=on` (pass; +w/-l, p) | `facts=on` (pass; +w/-l, p) |
| --- | ---: | --- | --- | --- | --- |
| `direct_caller` | 10 | 6/10 (60.0%) | 3/10 (30.0%); +0/-3 (-30.0pp, p=0.2500) | 5/10 (50.0%); +1/-2 (-10.0pp, p=1.0000) | 6/10 (60.0%); +1/-1 (+0.0pp, p=1.0000) |
| `indirect_caller` | 10 | 5/10 (50.0%) | 3/10 (30.0%); +0/-2 (-20.0pp, p=0.5000) | 3/10 (30.0%); +1/-3 (-20.0pp, p=0.6250) | 5/10 (50.0%); +1/-1 (+0.0pp, p=1.0000) |
| `caller_dependent` | 20 | 11/20 (55.0%) | 6/20 (30.0%); +0/-5 (-25.0pp, p=0.0625) | 8/20 (40.0%); +2/-5 (-15.0pp, p=0.4531) | 11/20 (55.0%); +2/-2 (+0.0pp, p=1.0000) |
| `control` | 10 | 9/10 (90.0%) | 6/10 (60.0%); +0/-3 (-30.0pp, p=0.2500) | 9/10 (90.0%); +1/-1 (+0.0pp, p=1.0000) | 8/10 (80.0%); +1/-2 (-10.0pp, p=1.0000) |

#### Stage 2 — Stratified by Language

| Stratum | n | `passive` (ref) | `cymbal (backend=cymbal)` (pass; +w/-l, p) | `read-range=on` (pass; +w/-l, p) | `facts=on` (pass; +w/-l, p) |
| --- | ---: | --- | --- | --- | --- |
| `python` | 6 | 5/6 (83.3%) | 2/6 (33.3%); +0/-3 (-50.0pp, p=0.2500) | 3/6 (50.0%); +0/-2 (-33.3pp, p=0.5000) | 4/6 (66.7%); +0/-1 (-16.7pp, p=1.0000) |
| `javascript` | 4 | 3/4 (75.0%) | 2/4 (50.0%); +0/-1 (-25.0pp, p=1.0000) | 2/4 (50.0%); +0/-1 (-25.0pp, p=1.0000) | 2/4 (50.0%); +0/-1 (-25.0pp, p=1.0000) |
| `typescript` | 4 | 2/4 (50.0%) | 1/4 (25.0%); +0/-1 (-25.0pp, p=1.0000) | 2/4 (50.0%); +1/-1 (+0.0pp, p=1.0000) | 3/4 (75.0%); +1/-0 (+25.0pp, p=1.0000) |
| `cpp` | 4 | 3/4 (75.0%) | 1/4 (25.0%); +0/-2 (-50.0pp, p=0.5000) | 4/4 (100.0%); +1/-0 (+25.0pp, p=1.0000) | 4/4 (100.0%); +1/-0 (+25.0pp, p=1.0000) |
| `bash` | 4 | 3/4 (75.0%) | 2/4 (50.0%); +0/-1 (-25.0pp, p=1.0000) | 2/4 (50.0%); +0/-1 (-25.0pp, p=1.0000) | 3/4 (75.0%); +0/-0 (+0.0pp, p=1.0000) |
| `c` | 4 | 2/4 (50.0%) | 2/4 (50.0%); +0/-0 (+0.0pp, p=1.0000) | 2/4 (50.0%); +1/-1 (+0.0pp, p=1.0000) | 2/4 (50.0%); +1/-1 (+0.0pp, p=1.0000) |
| `rust` | 4 | 2/4 (50.0%) | 2/4 (50.0%); +0/-0 (+0.0pp, p=1.0000) | 2/4 (50.0%); +0/-0 (+0.0pp, p=1.0000) | 1/4 (25.0%); +0/-1 (-25.0pp, p=1.0000) |

### Control Non-Inferiority Check (`qwen/qwen3.6-35b-a3b`)

| Stage | Arm / Factor | n | +w/-l (net reg) | Paired Diff (95% CI) | 1-Sided 95% UB (Reg Rate) | Clopper-Pearson 95% CI | Catastrophic | Operational Gate | Strict CI Gate |
| --- | --- | ---: | --- | --- | ---: | --- | ---: | --- | --- |
| Stage 1 | `passive` | 10 | +2/-0 (-2) | +20.0pp [-13.5pp, +50.2pp] | 25.9% | [0.0%, 30.8%] | 0 | PASS | UNDERPOWERED/FAIL |
| Stage 1 | `contract` | 10 | +0/-7 (7) | -70.0pp [-89.2pp, -28.9pp] | 91.3% | [34.8%, 93.3%] | 0 | FAIL | UNDERPOWERED/FAIL |
| Stage 1 | `audit` | 10 | +1/-0 (-1) | +10.0pp [-17.3pp, +36.4pp] | 25.9% | [0.0%, 30.8%] | 1 | FAIL | UNDERPOWERED/FAIL |
| Stage 1 | `contract+audit` | 10 | +0/-7 (7) | -70.0pp [-89.2pp, -28.9pp] | 91.3% | [34.8%, 93.3%] | 0 | FAIL | UNDERPOWERED/FAIL |
| Stage 2 | `cymbal (backend=cymbal)` | 10 | +0/-3 (3) | -30.0pp [-59.3pp, +7.0pp] | 60.7% | [6.7%, 65.2%] | 1 | FAIL | UNDERPOWERED/FAIL |
| Stage 2 | `read-range=on` | 10 | +1/-1 (0) | +0.0pp [-32.4pp, +32.4pp] | 39.4% | [0.3%, 44.5%] | 1 | FAIL | UNDERPOWERED/FAIL |
| Stage 2 | `facts=on` | 10 | +1/-2 (1) | -10.0pp [-43.4pp, +25.7pp] | 50.7% | [2.5%, 55.6%] | 2 | FAIL | UNDERPOWERED/FAIL |

> **Sample-Size Power Limitation:** On `n = 10` control tasks, even `0/10` paired regressions has an exact one-sided 95% upper confidence bound of `25.9%` (two-sided Clopper-Pearson `[0.0%, 30.8%]`), and `1/10` regressions has an upper bound of `39.4%`. Passing the operational `<= 1/10` tolerance is **never** proof of zero degradation; bounding true regression rate below `5%` at 95% confidence with `0` losses requires `n >= 59` independent control tasks.

## 4. Failure, Timeout, False-Approval & UNKNOWN Verdict Catalog

**Observed anomaly counts:** `FALSE_APPROVAL=7`, `HIDDEN_FAIL=130`, `SMOKE_FAIL=10`, `TIMEOUT=27`, `UNKNOWN_VERDICT=23` (total flagged rows: 151)

| Model | Stage | Arm | Case ID | Category | Language | Flags | Hidden | Smoke | Audit | Commit | Exit | Seconds |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `bash_bats_load_stack_trace` | `indirect_caller` | `bash` | `HIDDEN_FAIL, UNKNOWN_VERDICT` | fail | pass | unknown | applied | 0 | 108.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `bash_bats_semaphore_exit_status` | `direct_caller` | `bash` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 34.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `bash_rbenv_init_shell_suffix` | `control` | `bash` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 56.4 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `bash_rbenv_version_file_traversal` | `indirect_caller` | `bash` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 39.0 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `c_hiredis_map_elements_overflow` | `direct_caller` | `c` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 363.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `c_tiny_regex_dot_newline` | `control` | `c` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 34.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `c_yajl_decode_null_byte` | `indirect_caller` | `c` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 186.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `c_yajl_parse_integer_non_digit` | `direct_caller` | `c` | `HIDDEN_FAIL, FALSE_APPROVAL` | fail | pass | pass | applied | 0 | 140.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `cpp_fmt_printf_unsigned_sign` | `control` | `cpp` | `TIMEOUT` | pass | pass | not_run | timeout | 124 | 363.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `js_dotenv_fast_bom` | `control` | `javascript` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 60.5 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `HIDDEN_FAIL, FALSE_APPROVAL` | fail | pass | pass | applied | 0 | 311.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `js_ms_reject_infinity` | `control` | `javascript` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 34.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `js_semver_inc_identifier_validation` | `indirect_caller` | `javascript` | `HIDDEN_FAIL, UNKNOWN_VERDICT` | fail | pass | unknown | applied | 0 | 61.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `py_click_show_envvar_sequence` | `control` | `python` | `HIDDEN_FAIL, UNKNOWN_VERDICT` | fail | pass | unknown | applied | 0 | 174.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `py_dotenv_parser_crlf_recovery` | `indirect_caller` | `python` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 59.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `py_itsdangerous_future_timestamp` | `control` | `python` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 51.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `py_packaging_invalid_interpreter_tag` | `direct_caller` | `python` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 101.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `py_pathspec_gitignore_cr_trim` | `indirect_caller` | `python` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 360.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `py_pathspec_globstar_newline` | `indirect_caller` | `python` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 186.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `rust_humantime_duration_nsec_carry_overflow` | `direct_caller` | `rust` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 99.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `rust_json_print_dec_neg_exp` | `indirect_caller` | `rust` | `HIDDEN_FAIL, UNKNOWN_VERDICT` | fail | pass | unknown | applied | 0 | 258.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `rust_semver_eval_less_prerelease` | `direct_caller` | `rust` | `HIDDEN_FAIL, UNKNOWN_VERDICT` | fail | pass | unknown | applied | 0 | 281.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `ts_ohash_serialize_map_keys` | `direct_caller` | `typescript` | `HIDDEN_FAIL, FALSE_APPROVAL` | fail | pass | pass | applied | 0 | 94.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `ts_pathe_normalize_windows_drive` | `indirect_caller` | `typescript` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 47.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `ts_pathe_resolve_unc_prefix` | `direct_caller` | `typescript` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 245.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `audit` | `ts_scule_is_uppercase` | `control` | `typescript` | `HIDDEN_FAIL, UNKNOWN_VERDICT` | fail | pass | unknown | applied | 0 | 44.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `bash_bats_load_stack_trace` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 115.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `c_hiredis_map_elements_overflow` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 119.0 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `cpp_fmt_dynamic_args_size` | `indirect_caller` | `cpp` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 164.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `js_semver_inc_identifier_validation` | `indirect_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 47.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `py_click_show_envvar_sequence` | `control` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 190.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `py_pathspec_gitignore_cr_trim` | `indirect_caller` | `python` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 360.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `rust_json_print_dec_neg_exp` | `indirect_caller` | `rust` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 241.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `rust_semver_display_formatter_flags` | `control` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `rust_semver_eval_less_prerelease` | `direct_caller` | `rust` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 287.5 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `ts_ohash_serialize_map_keys` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 52.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `ts_pathe_resolve_unc_prefix` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 173.0 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `baseline` | `ts_scule_is_uppercase` | `control` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 47.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `bash_bats_load_stack_trace` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 89.5 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `bash_bats_semaphore_exit_status` | `direct_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 31.0 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `bash_rbenv_init_shell_suffix` | `control` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 72.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `bash_rbenv_version_file_traversal` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 53.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `c_hiredis_map_elements_overflow` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 203.4 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `c_tiny_regex_dot_newline` | `control` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 33.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `c_yajl_decode_null_byte` | `indirect_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 216.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `c_yajl_parse_integer_non_digit` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 114.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `cpp_fmt_dynamic_args_size` | `indirect_caller` | `cpp` | `TIMEOUT` | pass | pass | not_run | timeout | 124 | 362.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `cpp_fmt_printf_unsigned_sign` | `control` | `cpp` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 137.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `cpp_inih_multiline_no_value` | `direct_caller` | `cpp` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 61.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `cpp_semver_prerelease_zero` | `control` | `cpp` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 151.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `js_dotenv_fast_bom` | `control` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 55.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 203.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `js_ms_reject_infinity` | `control` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 36.0 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `js_semver_inc_identifier_validation` | `indirect_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 92.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `py_click_show_envvar_sequence` | `control` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 233.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `py_dotenv_parser_crlf_recovery` | `indirect_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 50.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `py_itsdangerous_future_timestamp` | `control` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 46.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `py_pathspec_gitignore_cr_trim` | `indirect_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 103.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `py_pathspec_globstar_newline` | `indirect_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 69.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `rust_json_print_dec_neg_exp` | `indirect_caller` | `rust` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 288.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `rust_semver_display_formatter_flags` | `control` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 363.0 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `rust_semver_eval_less_prerelease` | `direct_caller` | `rust` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 171.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `ts_ohash_serialize_map_keys` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 88.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `ts_pathe_normalize_windows_drive` | `indirect_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 49.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `ts_pathe_resolve_unc_prefix` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 205.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract` | `ts_scule_is_uppercase` | `control` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 48.4 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `bash_bats_load_stack_trace` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 92.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `bash_bats_semaphore_exit_status` | `direct_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 45.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `bash_rbenv_init_shell_suffix` | `control` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 57.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `bash_rbenv_version_file_traversal` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 53.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `c_hiredis_map_elements_overflow` | `direct_caller` | `c` | `HIDDEN_FAIL, FALSE_APPROVAL` | fail | pass | pass | applied | 0 | 216.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `c_tiny_regex_dot_newline` | `control` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 41.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `c_yajl_decode_null_byte` | `indirect_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 194.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `c_yajl_parse_integer_non_digit` | `direct_caller` | `c` | `HIDDEN_FAIL, FALSE_APPROVAL` | fail | pass | pass | applied | 0 | 187.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `cpp_fmt_printf_unsigned_sign` | `control` | `cpp` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 136.5 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `cpp_inih_multiline_no_value` | `direct_caller` | `cpp` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 76.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `cpp_semver_prerelease_zero` | `control` | `cpp` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 154.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `js_dotenv_fast_bom` | `control` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 63.5 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `HIDDEN_FAIL, FALSE_APPROVAL` | fail | pass | pass | applied | 0 | 247.7 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `js_ms_reject_infinity` | `control` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 40.4 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `js_semver_inc_identifier_validation` | `indirect_caller` | `javascript` | `HIDDEN_FAIL, UNKNOWN_VERDICT` | fail | pass | unknown | applied | 0 | 86.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `py_click_show_envvar_sequence` | `control` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 134.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `py_dotenv_parser_crlf_recovery` | `indirect_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 60.5 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `py_itsdangerous_future_timestamp` | `control` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 58.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `py_packaging_invalid_interpreter_tag` | `direct_caller` | `python` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 115.0 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `py_pathspec_gitignore_cr_trim` | `indirect_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 178.3 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `py_pathspec_globstar_newline` | `indirect_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 78.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `rust_humantime_duration_nsec_carry_overflow` | `direct_caller` | `rust` | `UNKNOWN_VERDICT` | pass | pass | unknown | applied | 0 | 135.0 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `rust_json_print_dec_neg_exp` | `indirect_caller` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 363.4 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `rust_semver_display_formatter_flags` | `control` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `rust_semver_eval_less_prerelease` | `direct_caller` | `rust` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 266.4 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `ts_ohash_serialize_map_keys` | `direct_caller` | `typescript` | `HIDDEN_FAIL, FALSE_APPROVAL` | fail | pass | pass | applied | 0 | 134.1 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `ts_pathe_normalize_windows_drive` | `indirect_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 46.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `ts_pathe_resolve_unc_prefix` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 194.5 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `contract+audit` | `ts_scule_is_uppercase` | `control` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | no_change | 0 | 47.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `bash_bats_load_stack_trace` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 125.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `c_hiredis_map_elements_overflow` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 132.8 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `c_yajl_parse_integer_non_digit` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 86.6 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `cpp_fmt_dynamic_args_size` | `indirect_caller` | `cpp` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `js_semver_inc_identifier_validation` | `indirect_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 69.2 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `py_pathspec_gitignore_cr_trim` | `indirect_caller` | `python` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 360.4 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `rust_json_print_dec_neg_exp` | `indirect_caller` | `rust` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 362.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `rust_semver_eval_less_prerelease` | `direct_caller` | `rust` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 157.4 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `ts_ohash_serialize_map_keys` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 48.9 |
| `qwen/qwen3.6-35b-a3b` | stage1 | `passive` | `ts_scule_is_uppercase` | `control` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 46.2 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `bash_bats_load_stack_trace` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 79.5 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `bash_rbenv_version_file_traversal` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 44.9 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `c_hiredis_map_elements_overflow` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 95.8 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `c_yajl_parse_integer_non_digit` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 102.1 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `cpp_fmt_dynamic_args_size` | `indirect_caller` | `cpp` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 311.1 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `cpp_fmt_printf_unsigned_sign` | `control` | `cpp` | `HIDDEN_FAIL, SMOKE_FAIL` | fail | fail | not_run | applied | 0 | 300.3 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `cpp_semver_prerelease_zero` | `control` | `cpp` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.5 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 145.3 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `js_semver_inc_identifier_validation` | `indirect_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 61.6 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `py_click_show_envvar_sequence` | `control` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 149.1 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `py_packaging_invalid_interpreter_tag` | `direct_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 110.8 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `py_pathspec_gitignore_cr_trim` | `indirect_caller` | `python` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 360.3 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `py_pathspec_globstar_newline` | `indirect_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 127.4 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `rust_json_print_dec_neg_exp` | `indirect_caller` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.6 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `rust_semver_eval_less_prerelease` | `direct_caller` | `rust` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 223.1 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `ts_ohash_serialize_map_keys` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 67.5 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `ts_pathe_resolve_unc_prefix` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 184.5 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `cymbal` | `ts_scule_is_uppercase` | `control` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 48.6 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `bash_bats_load_stack_trace` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 89.5 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `c_yajl_decode_null_byte` | `indirect_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 122.8 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `c_yajl_parse_integer_non_digit` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 77.6 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `cpp_semver_prerelease_zero` | `control` | `cpp` | `TIMEOUT` | pass | pass | not_run | timeout | 124 | 362.2 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 360.3 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `js_semver_inc_identifier_validation` | `indirect_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 76.2 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `py_click_show_envvar_sequence` | `control` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 163.0 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `py_pathspec_gitignore_cr_trim` | `indirect_caller` | `python` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 360.7 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `rust_json_print_dec_neg_exp` | `indirect_caller` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.8 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `rust_semver_display_formatter_flags` | `control` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.9 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `rust_semver_eval_less_prerelease` | `direct_caller` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.4 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `facts` | `ts_ohash_serialize_map_keys` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 55.0 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `bash_bats_load_stack_trace` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 88.4 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `bash_rbenv_version_file_traversal` | `indirect_caller` | `bash` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 68.8 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `c_hiredis_map_elements_overflow` | `direct_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 175.9 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `c_yajl_decode_null_byte` | `indirect_caller` | `c` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 122.8 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `cpp_inih_multiline_no_value` | `direct_caller` | `cpp` | `TIMEOUT` | pass | pass | not_run | timeout | 124 | 363.6 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `cpp_semver_prerelease_zero` | `control` | `cpp` | `TIMEOUT` | pass | pass | not_run | timeout | 124 | 362.6 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 201.8 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `js_semver_inc_identifier_validation` | `indirect_caller` | `javascript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 57.2 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `py_click_show_envvar_sequence` | `control` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 187.7 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `py_pathspec_gitignore_cr_trim` | `indirect_caller` | `python` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 360.3 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `py_pathspec_globstar_newline` | `indirect_caller` | `python` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 180.7 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `rust_json_print_dec_neg_exp` | `indirect_caller` | `rust` | `HIDDEN_FAIL, TIMEOUT` | fail | pass | not_run | timeout | 124 | 362.3 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `rust_semver_eval_less_prerelease` | `direct_caller` | `rust` | `HIDDEN_FAIL, SMOKE_FAIL, TIMEOUT` | fail | fail | not_run | timeout | 124 | 360.9 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `ts_ohash_serialize_map_keys` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 41.6 |
| `qwen/qwen3.6-35b-a3b` | stage2 | `read_range` | `ts_pathe_resolve_unc_prefix` | `direct_caller` | `typescript` | `HIDDEN_FAIL` | fail | pass | not_run | applied | 0 | 121.3 |

## 5. Shipping Gate Verdict (`OPT_IN`, `DEFAULT_ON`, `BLOCKED`, or `INCOMPLETE`)

| Stage | Candidate | Overall Verdict | Per-Model Verdicts | Evidence Rationale |
| --- | --- | --- | --- | --- |
| stage1 | `passive` | **BLOCKED** | `qwen/qwen3.6-35b-a3b=BLOCKED` | qwen/qwen3.6-35b-a3b: Critical safety regression(s): false_approvals=0, errors=0, out_of_scope=0, new_timeouts=1 |
| stage1 | `contract` | **BLOCKED** | `qwen/qwen3.6-35b-a3b=BLOCKED` | qwen/qwen3.6-35b-a3b: Control operational non-inferiority gate failed (net_regressions=7/10, delta=-70.0pp, catastrophic=0); qwen/qwen3.6-35b-a3b: Statistically significant regression observed (caller_delta=-35.0pp, overall_delta=-46.7pp) |
| stage1 | `audit` | **BLOCKED** | `qwen/qwen3.6-35b-a3b=BLOCKED` | qwen/qwen3.6-35b-a3b: Critical safety regression(s): false_approvals=3, errors=0, out_of_scope=0, new_timeouts=2; qwen/qwen3.6-35b-a3b: Control operational non-inferiority gate failed (net_regressions=-1/10, delta=+10.0pp, catastrophic=1) |
| stage1 | `contract+audit` | **BLOCKED** | `qwen/qwen3.6-35b-a3b=BLOCKED` | qwen/qwen3.6-35b-a3b: Critical safety regression(s): false_approvals=4, errors=0, out_of_scope=0, new_timeouts=1; qwen/qwen3.6-35b-a3b: Control operational non-inferiority gate failed (net_regressions=7/10, delta=-70.0pp, catastrophic=0); qwen/qwen3.6-35b-a3b: Statistically significant regression observed (caller_delta=-35.0pp, overall_delta=-46.7pp) |
| stage2 | `cymbal` | **BLOCKED** | `qwen/qwen3.6-35b-a3b=BLOCKED` | qwen/qwen3.6-35b-a3b: Critical safety regression(s): false_approvals=0, errors=0, out_of_scope=0, new_timeouts=1; qwen/qwen3.6-35b-a3b: Control operational non-inferiority gate failed (net_regressions=3/10, delta=-30.0pp, catastrophic=1); qwen/qwen3.6-35b-a3b: Statistically significant regression observed (caller_delta=-25.0pp, overall_delta=-26.7pp) |
| stage2 | `read_range` | **BLOCKED** | `qwen/qwen3.6-35b-a3b=BLOCKED` | qwen/qwen3.6-35b-a3b: Critical safety regression(s): false_approvals=0, errors=0, out_of_scope=0, new_timeouts=3; qwen/qwen3.6-35b-a3b: Control operational non-inferiority gate failed (net_regressions=0/10, delta=+0.0pp, catastrophic=1) |
| stage2 | `facts` | **BLOCKED** | `qwen/qwen3.6-35b-a3b=BLOCKED` | qwen/qwen3.6-35b-a3b: Critical safety regression(s): false_approvals=0, errors=0, out_of_scope=0, new_timeouts=4; qwen/qwen3.6-35b-a3b: Control operational non-inferiority gate failed (net_regressions=1/10, delta=-10.0pp, catastrophic=2) |
