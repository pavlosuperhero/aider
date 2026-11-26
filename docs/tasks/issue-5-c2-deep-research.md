# Issue #5 Section C2 — Forensic Deep Research Report (`lm_studio/qwen/qwen3.6-35b-a3b`)

- **Scope:** Complete forensic post-mortem of all **240 benchmark attempts** (`150` Stage 1 + `90` Stage 2) on the frozen preregistered corpus `issue5-c1-polyglot-v1` (`30` tasks: `10` `direct_caller`, `10` `indirect_caller`, `10` `control`; `7` languages: `python=6, javascript=4, typescript=4, cpp=4, bash=4, c=4, rust=4`).
- **Artifacts Verified:**
  - Frozen manifest: `/tmp/issue5-replay-corpus/manifest.json` (`sha256=c00e46d1fb3b00895c02846625e69446ce40b3274de19091f9017c9779d0a429`, `PASS`).
  - Reports: `docs/tasks/issue-5-benchmark-report.md`, `/tmp/issue5_c2_report.json`, `docs/tasks/issue-5-retrieval-benchmark.md`.
  - Raw JSONL & all **240/240** corresponding `aider.log`, `patch.diff`, and `verification.txt` files across `issue5-c2-replay-run/` and `/var/folders/0l/z219pp_1317b852dzhx8mjjm0000gn/T/aider-heldout-{e2lji1e0,0vwhb9jo,3t2l_7do,nse1xodw}`.
  - Read-only source inspection: `aider/caller_audit.py`, `aider/cymbal_callers.py`, `aider/local_callers.py`, `aider/coders/base_coder.py`, `aider/models.py`, `scripts/local_context_trial/run.py`, and `git diff ac5fcd6..b2e6ca3`.

---

## Executive Summary

A naive reading of the aggregate Section C2 tables suggests that `passive` works well (`+4/-1`, `20/30`), `cymbal` catastrophically regressed (`+0/-8`, `p = 0.0078`), `read_range` hurt (`+3/-6`), `contract` failed because the model struggled with contract prompting (`3/30`), and `audit` mostly failed to parse verdicts (`83.3%` `UNKNOWN`). **Forensic inspection of all 240 raw logs overturns almost every one of those surface interpretations:**

| Surface Finding | Forensic Reality (Verified from Raw Logs & Source) | Primary Root Cause |
| --- | --- | --- |
| **`cymbal` (`+0/-8`, `p=0.0078`, `ev=6/30` vs `9/30`)** | **5/8 (62.5%)** losses had **identical `NO_EVIDENCE`** on both arms; **1/8** (`js_dotenv_quiet_from_env`) had the **exact same `config.js:1` excerpt and `ptok=9837`**; only **2/8** (`py_packaging_invalid_interpreter_tag`, `py_click_show_envvar_sequence`) had real Cymbal noise/distraction. Meanwhile, Cymbal had **0/4 recall on C, C++, and Rust** (missing 5 `builtin`-retrieved cases) due to real code bugs, though those 5 cases were `+0/-0` (`2/5` vs `2/5`). Commit drift `ac5fcd6 -> b2e6ca3` had **zero** runtime effect. | **Compound:** (1) Unseeded LM Studio variance + winner's curse against Stage 1's highest draw (`passive=20/30`); (2) real Cymbal target-extraction (`aider/cymbal_callers.py:1945`), per-symbol error abort (`L2112`), and noisy Python class/transitive-caller bugs. |
| **`contract` (`3/30`, `+1/-15`, `p=0.0005`, `0/10` control)** | **100% (21/21)** of `evidence=False` cases (including `9/10` controls) were **deterministically blocked to `no_change`** after the LLM produced valid edit blocks (`3–15` `SEARCH/REPLACE` blocks each). On the `9` `evidence=True` cases, `contract` applied `7/9` and passed `3/9` (`33.3%`, vs `passive` `4/9` and `baseline` `3/9`). | **Deterministic code bug:** `CallerContractHook.__call__` (`aider/caller_audit.py:401-407`) returns `ResponseHookResult()` (`continuation=None`, `blocked=True`) whenever `not self._excerpts` and `not advisory`, silently discarding valid edits. |
| **`audit` (`3/24` false approvals, `20/24 = 83.3%` `UNKNOWN`)** | **20/20 (100%)** of `UNKNOWN` verdicts were deterministic pre-LLM short-circuits (`17` `NO_EVIDENCE`, `3` `INCOMPLETE`; **0 LLM calls**). Across `audit` + `contract+audit`, **8/8 (100%)** actual LLM auditor invocations emitted `VERDICT: PASS` (`0` `FAIL`, `0` `UNKNOWN`), and **7/8 (87.5%) were false approvals** (`3/4 = 75%` in `audit`, `4/4 = 100%` in `contract+audit`). | **Compound:** (1) `TreeSitterCallerFinder` matched **C header prototypes** (`yajl_parser.h:75`, `hiredis.h:338`) as `COMPLETE_WITHIN_SCOPE`; (2) severe LLM overconfidence/miscalibration (`ECE ≈ 0.875` on LLM-judged items) rubber-stamping failing patches on uninformative snippets. |
| **`facts=on` (`+3/-4`, `4` new timeouts, `facts_attached=0/30`)** | Attached **0/30** facts, serving as a **natural null control**. Yet it produced **`7/30` (23.3%) discordant flips** and **4 new timeouts** (`4/4` `0`-call first-turn runaways). | **Stochastic noise floor:** Unseeded LM Studio inference + `RepoMap` `PYTHONHASHSEED` tie-break drift (`±20–290` prompt tokens) + unbounded `max_tokens=32768` thinking chains. |
| **`read_range=on` (`+3/-6`, `3` new timeouts)** | **Complete stratified reversal:** On the **9** `read_range_attached=True` cases, ReadRange went **`+2/-1` (`6/9` vs `5/9`, `0` new timeouts)** at mean `+304` prompt tokens. All net losses (`+1/-5`) and **all 3 new timeouts** occurred on the **21 `read_range_attached=False` cases**. | **Code confounder + variance:** `aider/coders/base_coder.py:472-477` bumps `max_reflections` from `3` to `4` whenever `local_callers_read_range=True`, even with 0 ranges attached—pushing `cpp_inih_multiline_no_value` (`pass=1`) into a 4th false-positive C++ lint reflection and 360s timeout. |
| **`passive` (`+4/-1` vs `baseline`, `20/30` vs `17/30`)** | **2/4 wins** had `evidence=False` (`NO_EVIDENCE`); **1/4 win** (`rust_semver_display_formatter_flags`) beat a `0`-call baseline timeout; the **sole non-timeout `evidence=True` win** (`js_dotenv_quiet_from_env`) had a 3-line `dotenv.config()` snippet and failed on all 5 other arms (`1/6`). The `1` loss (`c_yajl_parse_integer_non_digit`) had `evidence=True`. | **Indistinguishable from noise:** Effective non-timeout `evidence=True` delta is `+1/-1` (`0` net gain). Additionally, Aider auto-added prompt-mentioned caller files to chat on `11/30` cases in `baseline` too, compressing retrieval headroom. |
| **All `27/240` Timeouts (360s)** | **0/27** were caused by slow compilation/tests. All 27 decompose cleanly into: **`10/27`** `0`-call first-turn thinking-token runaways, **`9/27`** false-positive C++/Rust tree-sitter `--auto-lint` reflection loops (`5/9` **already passed hidden tests!**), and **`8/27`** smoke-test-failure reflection loops. | **Harness & model config:** Unbounded `max_tokens=32768` thinking (`deepseek-r1-reasoning` p. 64) + default `--auto-lint` flagging C++ macros (`FMT_END_NAMESPACE`) on `35/240` runs. |

---

## 1. Master Per-Arm Telemetry & Cross-Arm Stochasticity Baseline

Across all 240 rows, every arm ran `lm_studio/qwen/qwen3.6-35b-a3b` (`edit_format="diff"`, `use_temperature=True` -> `temperature=0` at `aider/models.py:1007-1008`, `extra_params={"max_tokens": 32768}`, `reasoning_tag="think"`, `thinking_tokens=None`, no `seed` argument passed to `litellm.completion`).

| Stage | Arm | Hidden Pass | Evidence Attached | Read-Range Attached | Facts Attached | `contract_rejected` | `no_change` | Timeouts (360s) | Mean Compl Tok (Med / Max) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Stage 1 | `baseline` | 17/30 (56.7%) | 0/30 | 0/30 | 0/30 | 0 | 0 | 3 | 6,170 (4,574 / 16,422) |
| Stage 1 | `passive` | 20/30 (66.7%) | 9/30 | 0/30 | 0/30 | 0 | 0 | 3 | 6,566 (5,419 / 16,275) |
| Stage 1 | `contract` | 3/30 (10.0%) | 9/30 | 0/30 | 0/30 | 22 | 21 | 2 | 6,120 (5,129 / 15,930) |
| Stage 1 | `audit` | 19/30 (63.3%) | 9/30 | 0/30 | 0/30 | 0 | 0 | 3 | 7,521 (5,960 / 17,716) |
| Stage 1 | `contract+audit` | 3/30 (10.0%) | 9/30 | 0/30 | 0/30 | 21 | 20 | 2 | 6,494 (4,941 / 16,933) |
| Stage 2 | `cymbal` | 12/30 (40.0%) | 6/30 | 0/30 | 0/30 | 0 | 0 | 3 | 6,384 (4,162 / 19,977) |
| Stage 2 | `read_range` | 17/30 (56.7%) | 9/30 | 9/30 | 0/30 | 0 | 0 | 5 | 6,145 (4,595 / 16,097) |
| Stage 2 | `facts` | 19/30 (63.3%) | 9/30 | 0/30 | 0/30 | 0 | 0 | 6 | 5,319 (4,250 / 16,097) |

**Cross-Arm Flip Rate Across the 6 Non-Contract Arms (`baseline`, `passive`, `audit`, `cymbal`, `read_range`, `facts`):**
- `10/30` cases passed `6/6` arms consistently; `6/30` cases failed `0/6` arms consistently.
- **`14/30` (46.7%)** cases flipped outcomes across the 6 non-contract arms: **`6/9`** of `passive` `evidence=True` cases and **`8/21` (38.1%)** of `passive` `evidence=False` cases!
- Two cases passed **exclusively** in Stage 1 `passive` (`1/6`): `js_dotenv_quiet_from_env` (`[0, 1, 0, 0, 0, 0]`) and `py_click_show_envvar_sequence` (`[0, 1, 0, 0, 0, 0]`).

---

## 2. Q1 — Cymbal Regression (`+0/-8` vs `passive`, `p = 0.0078`, `evidence = 6/30` vs `9/30`)

### 2.1. Complete Forensic Classification of All 8 Discordant Losses

Comparing `aider-heldout-0vwhb9jo` (`cymbal`) against `aider-heldout-e2lji1e0` (`passive`) across all 30 tasks reveals that **Cymbal's `-8` paired loss is a mixture of stochastic variance / winner's curse (6/8 losses) and genuine Cymbal noise injection (2/8 losses)**—while Cymbal's real recall failures on C/C++/Rust (5 cases) caused **0/8** of the discordant losses.

| # | Case ID | Category | Lang | Ev (`P`/`C`) | 1st-Prompt Tok (`P`/`C`, `Δ`) | 6-Arm Pass Vector `[B,P,A,C,RR,F]` | Root Cause Classification |
| --- | --- | --- | --- | :---: | ---: | :---: | --- |
| 1 | `bash_rbenv_version_file_traversal` | `indirect_caller` | `bash` | `0` / `0` | 5,713 / 5,713 (`+0`) | `[1,1,1,0,0,1]` | **Pure LLM variance** (identical `NO_EVIDENCE`, identical prompt; Cymbal rejected `..` with `exit 1` instead of writing `'invalid version'` to stderr). |
| 2 | `cpp_fmt_printf_unsigned_sign` | `control` | `cpp` | `0` / `0` | 12,035 / 12,202 (`+167`) | `[1,1,1,0,1,1]` | **Pure LLM variance + RepoMap tie-break** (both `NO_EVIDENCE`; Cymbal patch failed smoke compilation). |
| 3 | `cpp_semver_prerelease_zero` | `control` | `cpp` | `0` / `0` | 13,900 / 13,900 (`+0`) | `[1,1,1,0,1,1]` | **Pure variance + C++ lint-loop timeout** (identical `NO_EVIDENCE`, identical prompt; hit 362.5s timeout on 2nd false-positive lint reflection with incomplete patch). |
| 4 | `py_pathspec_globstar_newline` | `indirect_caller` | `python` | `0` / `0` | 12,164 / 12,164 (`+0`) | `[1,1,1,0,0,1]` | **Pure LLM variance** (identical `NO_EVIDENCE`, identical prompt; Cymbal emitted `[\s\S]` instead of `(?s:.)`, matching identically to `read_range` loss). |
| 5 | `ts_pathe_resolve_unc_prefix` | `direct_caller` | `typescript` | `0` / `0` | 9,337 / 9,337 (`+0`) | `[0,1,1,0,0,1]` | **Pure LLM variance** (identical `NO_EVIDENCE`, identical prompt; flipped 3/6 across arms). |
| 6 | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `1` / `1` | 9,837 / 9,837 (`+0`) | `[0,1,0,0,0,0]` | **Pure LLM variance / winner's curse** (BOTH attached **identical** `config.js:1`, identical `ptok=9837`; `passive` was the ONLY arm of 6 to pass). |
| 7 | `py_packaging_invalid_interpreter_tag` | `direct_caller` | `python` | `1` / `1` | 18,677 / 18,941 (`+264`) | `[1,1,1,0,1,1]` | **Real Cymbal noise/distraction:** Builtin attached `src/packaging/pylock.py:610`; Cymbal added **spurious transitive caller** `src/packaging/direct_url.py:71` (`_get_object`, unrelated) + `pylock.py:609` with 4 ambiguity flags. Failed **ONLY** in Cymbal (`5/6` elsewhere). |
| 8 | `py_click_show_envvar_sequence` | `control` | `python` | `0` / `1` | 40,135 / 40,420 (`+285`) | `[0,1,0,0,0,0]` | **Cymbal noise on control + winner's curse:** Builtin had `NO_EVIDENCE`; Cymbal injected noisy ambiguous method caller `src/click/exceptions.py:147` (`duplicate symbol definition, ambiguous method receiver`). Also a `1/6` `passive`-only outlier. |

### 2.2. Stratified Breakdown Across All 30 Tasks (`passive` vs `cymbal`)

| Evidence Stratum (`Passive` vs `Cymbal`) | `n` | Passive Pass | Cymbal Pass | Paired (`+w/-l`) | Exact McNemar `p` | Interpretation |
| --- | ---: | ---: | ---: | :---: | ---: | --- |
| Both `evidence=False` (identical empty caller context) | 19 | 14/19 (73.7%) | 9/19 (47.4%) | `+0/-5` | `0.0625` | Pure stochasticity + winner's curse (no Cymbal snippet ever entered prompt!) |
| Both `evidence=True` | 4 | 2/4 (50.0%) | 0/4 (0.0%) | `+0/-2` | `0.5000` | 1 identical (`js_dotenv`), 1 real Cymbal transitive-caller noise (`py_packaging`) |
| `Passive` `ev=True`, `Cymbal` `ev=False` (Cymbal missed) | 5 | 2/5 (40.0%) | 2/5 (40.0%) | `+0/-0` | `1.0000` | Real Cymbal recall deficit on C/C++/Rust, zero pass-rate delta here |
| `Passive` `ev=False`, `Cymbal` `ev=True` (Cymbal extra on controls) | 2 | 2/2 (100.0%) | 1/2 (50.0%) | `+0/-1` | `1.0000` | Spurious ambiguous method-receiver excerpts on control tasks (`py_click`, `py_itsdangerous`) |
| **Total** | **30** | **20/30 (66.7%)** | **12/30 (40.0%)** | **`+0/-8`** | **`0.0078`** | **6/8 losses had zero caller-content difference; 2/8 had noisy Cymbal excerpts** |

*(Cross-check against `baseline` `17/30`: `cymbal` vs `baseline` is `+1/-6`, exact McNemar `p = 0.1250`; on the 19 `NO_EVIDENCE` cases, `cymbal` vs `baseline` is `+0/-4`, `p = 0.1250`.)*

### 2.3. Three Confirmed Cymbal Code Defects Exposed by the Logs (`ev = 6/30` vs `builtin` `9/30`)

Even though stochastic variance inflated the apparent `-8` headline, raw logs reveal **four concrete, deterministic engineering defects** in `aider/cymbal_callers.py` and Cymbal v0.18.0:

1. **Zero C/C++ function target extraction (`aider/cymbal_callers.py:1945-1955`):**
   `_extract_generic_targets()` relies exclusively on `_DEF_REGEXES` (`aider/local_callers.py:98-118`), which only matches JS/TS (`function`, arrow), Go (`func`), Rust (`fn`), Python/Ruby (`def`), and `class`. Unlike `TreeSitterCallerFinder._extract_definitions()` (`aider/local_callers.py:1191-1202`), it never invokes tree-sitter queries! Consequently, `_extract_editable_targets()` returned **`{}`** on both C cases (`c_hiredis_map_elements_overflow`, `c_yajl_parse_integer_non_digit`)—Cymbal was never even called.
2. **Fatal per-symbol abort on unindexed C++ templates (`aider/cymbal_callers.py:2112`):**
   On `cpp_fmt_dynamic_args_size`, `_extract_generic_targets()` matched C++ template classes `dynamic_format_arg_store` and `basic_format_args` via the `class` regex. Calling `self.client.refs(resolved_root, "dynamic_format_arg_store")` raised:
   ```text
   Local callers (cymbal) failed: Cymbal command failed (1): Error: symbol not found: dynamic_format_arg_store
   Local caller evidence: NO_EVIDENCE (cymbal error: Cymbal command failed (1): Error: symbol not found: dynamic_format_arg_store)
   ```
   Because `CymbalCallerFinder.find_callers` does not catch per-symbol `CymbalExecutionError`, a single unindexed symbol aborted the entire lookup for all remaining symbols (`basic_format_args`).
3. **Zero Rust caller recall (`rust_humantime_duration_nsec_carry_overflow`, `rust_semver_display_formatter_flags`):**
   `_extract_editable_targets()` correctly extracted `parse_duration` and `fmt`, yet Cymbal v0.18.0 returned `Sources: none` on both (vs `builtin` finding `src/date.rs:193` and `src/error.rs:83`).
4. **Noisy Python class/method & transitive-caller leakage (`aider/cymbal_callers.py:1920-1925`, `2146-2147`):**
   Unlike `PythonAstCallerFinder` (`aider/local_callers.py:689-787`), which ignores bare class names and verifies candidate imports before matching method calls, `_extract_python_targets` extracts bare `class` names (`Option`, `TimestampSigner`) and matches every same-name method globally via Cymbal `refs`/`impact`. On `py_click_show_envvar_sequence` (control) and `py_itsdangerous_future_timestamp` (control), Cymbal injected `INCOMPLETE: duplicate symbol definition, ambiguous method receiver` excerpts; on `py_packaging_invalid_interpreter_tag`, `impact` injected unrelated transitive caller `src/packaging/direct_url.py:71` (`_get_object`). This mirrors the exact `same_name` wrong-target failure (`0.500` precision / `0.500` wrong target) documented in Section B (`docs/tasks/issue-5-retrieval-benchmark.md`, lines 22, 86), and illustrates the **retrieval granularity / noise vs. completeness tradeoff** (`rag-survey`, p. 8, `bc4f3e5ab6f2de7a68c06704`) and the hazard of **accumulating irrelevant context** when retrieval is not gated (`rag-survey`, p. 11, `cda0c6c2c05eca1c55e40d1b`). It also confirms why Section B's Python/TS-only synthetic fixtures could not predict C/C++/Rust recall collapse (`beir-guide`, lines 414–438, `90905b05c622807ee5f8a78d`; `ir-textbook`, p. 198, `5ecfb939602f76b7331773f5`).

### 2.4. Commit Drift Check (`ac5fcd651073` Stage 1 vs `b2e6ca399ad5` Stage 2)

Inspecting `git diff ac5fcd6..b2e6ca3` confirms **zero causal impact** of commit drift on `cymbal` (or `facts`):
- `aider/main.py` (+28/-8): Modified only `LocalReadRangeReader.read_call_site` (never invoked when `--no-local-callers-read-range` is passed in `cymbal` and `facts`).
- `aider/project_facts.py` (+50/-15): Added `FileLock` serialization on JSONL writes (never invoked when `--no-facts` is passed in `cymbal`, and zero facts written/read in unseeded `facts`).
- `scripts/local_context_trial/heldout.py` & `run.py`: Wrapped trial exceptions in `_build_error_row`, excluded `.lock` files from git, and changed `commit_passed` boolean (`commits_added > 0`) without altering `hidden_passed` or Aider's runtime flags.
- `docs/local-context.md`, `scripts/local_context_trial/c2_report.py`, and `tests/`: Non-runtime reporting/test files only.

---

## 3. Q2 — Contract Collapse (`3/30 = 10.0%`, `+1/-15`, `p = 0.0005`, `0/10` Control)

### 3.1. Exact Deterministic Root Cause in Source Code & Raw Logs

The collapse of both `contract` (`3/30`) and `contract+audit` (`3/30`) was **NOT** caused by `qwen3.6-35b-a3b` failing to write `CALLER_CONTRACT:` / `TRACE:` headers. It was caused by a **hard fail-closed bug in `CallerContractHook.__call__`** (`aider/caller_audit.py:400-407`), backed by a second fail-closed check in `Coder.apply_updates` (`aider/coders/base_coder.py:3339-3345`):

```python
# aider/caller_audit.py:400-407
        # Supplementary evidence (facts/read-ranges) NEVER satisfies missing caller evidence.
        if not self._excerpts:
            if io:
                io.tool_output("Local caller contract check: NO_EVIDENCE")
            if self.advisory:
                return None
            self.blocked = True
            return ResponseHookResult()  # <-- continuation=None: SILENTLY ABORTS EDIT!
```

```python
# aider/coders/base_coder.py:3339-3345
            contract = self.local_caller_contract_hook
            if edits and contract.enabled and not contract.format_present and not contract.advisory:
                contract.blocked = True
                self.io.tool_warning(
                    "Local caller contract check: UNKNOWN (unverified edit format)"
                )
                return edited  # <-- returns empty set(); zero edits applied!
```

Because `local_callers_policy` defaults to `"strict"` (`advisory=False`, `aider/coders/base_coder.py:426-428, 468`), **whenever caller retrieval returned `NO_EVIDENCE` (`21/30` cases, including `9/10` controls), the model generated valid `SEARCH/REPLACE` blocks (between `3` and `15` blocks per case), and `CallerContractHook` unconditionally discarded the entire edit with zero retry (`commit_outcome="no_change"`).**

### 3.2. Exact Stratified Outcomes (`evidence=False` vs `evidence=True`)

| Arm | Stratum | `n` | Hidden Pass | Applied | `no_change` (Blocked) | Timeouts | Hook Log Evidence |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `contract` | `evidence=False` | 21 | **0/21 (0.0%)** | 0 | **21** | 0 | `21x` `Local caller contract check: NO_EVIDENCE` (every run had 3–15 `<<<<<<< SEARCH` blocks!) |
| `contract` | `evidence=True` | 9 | **3/9 (33.3%)** | 7 | 0 | 2 | `7x` format present on turn 1; `1x` `missing contract/trace` retried & passed (`cpp_fmt_dynamic_args_size`); `1x` 0-call timeout |
| `contract+audit` | `evidence=False` | 21 | **0/21 (0.0%)** | 0 | **20** | 1 | `20x` `Local caller contract check: NO_EVIDENCE`; `1x` 0-call timeout (`rust_json_print_dec_neg_exp`) |
| `contract+audit` | `evidence=True` | 9 | **3/9 (33.3%)** | 8 | 0 | 1 | `7x` format present on turn 1; `1x` `missing contract/trace` retried & passed (`cpp_fmt_dynamic_args_size`); `1x` 0-call timeout |
| `passive` (ref) | `evidence=True` (same 9) | 9 | **4/9 (44.4%)** | 8 | 0 | 1 | — |
| `baseline` (ref) | same 9 cases | 9 | **3/9 (33.3%)** | 7 | 0 | 2 | — |

### 3.3. What Happened on the 9 `evidence=True` Cases (Even Without the `NO_EVIDENCE` Bug)

On the 9 cases where evidence WAS attached, `contract` scored **`3/9` (`33.3%`)** vs `passive` **`4/9` (`44.4%`)** (`+1/-2`, `p = 1.0000`):
- **Passes in `contract` (`3/9`):** `cpp_fmt_dynamic_args_size` (won vs passive timeout, though `contract` itself later timed out in a lint loop *after* applying the passing patch), `py_packaging_invalid_interpreter_tag`, `rust_humantime_duration_nsec_carry_overflow`.
- **Losses vs `passive` (`2`):** `js_dotenv_quiet_from_env` (applied with format present, failed hidden test) and `rust_semver_display_formatter_flags` (control case; `0`-call first-turn thinking timeout).
- **Format compliance was high (`7/8` on turn 1)**, meaning `CALLER_CONTRACT:` + `TRACE:` satisfied the syntactic regex check (`aider/caller_audit.py:410-416`, which explicitly logs `"Local caller contract format present; semantics unverified"`) without verifying semantic correctness.

**Fundamental-Works Grounding:**
- **Positive instructions vs. rigid negative constraints (`prompt-engineering`, pp. 56–57, `bac1d6367a4987d87c1f7f18`, `a85c0612f27a0d4af6804b6d`):** Enforcing a rigid blocking gate ("refuse to apply any edit unless exact `CALLER_CONTRACT:`/`TRACE:` format AND non-empty caller excerpts exist") acts as a brittle negative constraint that clashes with valid edits when caller evidence is absent (such as 9/10 control tasks), rather than positively instructing the model how to use attached evidence when available.
- **Reasoning-model token search space (`deepseek-r1-reasoning`, p. 64, `49684bee0b74b3588404c616`):** Forcing a reasoning model (`qwen3.6-35b-a3b`, already spending 5k–15k `<think>` tokens) to emit redundant explicit `CALLER_CONTRACT:` + `TRACE:` prose before `SEARCH/REPLACE` expands token generation and compounds the risk of length/time limits without adding a reliable semantic verifier.

---

## 4. Q3 — Audit Calibration (`3/24 = 12.5%` False Approvals, `20/24 = 83.3%` `UNKNOWN`)

### 4.1. Why 83.3% of `audit` Verdicts Were `UNKNOWN` (Zero LLM Abstentions!)

In the `audit` arm (`n = 30`), `audit_ran = 24` and `audit = not_run` on `6/30`:
- **Why `6/30` were `not_run`:** `3` timed out before reaching audit (`c_hiredis_map_elements_overflow`, `cpp_fmt_printf_unsigned_sign`, `py_pathspec_gitignore_cr_trim`), and **`3` bypassed audit via a subtle `base_coder.py:2571` edge case** (`cpp_fmt_dynamic_args_size`, `cpp_inih_multiline_no_value`, `cpp_semver_prerelease_zero`): after earlier reflection turns applied edits and triggered false-positive C++ lint reflections, the final reflection turn replied *"already applied, no further changes required"* (`edited = set()`), causing `if auditing and edited and not test_errors` (`aider/coders/base_coder.py:2571`) to skip `_finish_local_caller_audit()` completely!
- **Exact breakdown of all `20/24` (`83.3%`) `UNKNOWN` verdicts:**
  - **`17/24` (70.8%):** `Local caller audit: UNKNOWN - NO_EVIDENCE` — deterministic pre-LLM short-circuit at `aider/caller_audit.py:555-556` (**0 LLM audit calls, 0 audit tokens**).
  - **`3/24` (12.5%):** `Local caller audit: UNKNOWN - INCOMPLETE: caller evidence` — deterministic pre-LLM short-circuit at `aider/caller_audit.py:559-560` (**0 LLM audit calls, 0 audit tokens**) on `js_semver_inc_identifier_validation`, `py_packaging_invalid_interpreter_tag`, and `rust_humantime_duration_nsec_carry_overflow`.
  - **`0/24` (0.0%):** **ZERO `UNKNOWN` verdicts came from LLM abstention or `parse_audit_verdict()` formatting failures.**

### 4.2. Catastrophic Overconfidence on Every Actual LLM Audit Call (`8/8 = 100%` `PASS`, `7/8 = 87.5%` False Approvals!)

| Arm | `audit_ran` | Pre-LLM `UNKNOWN` (`0` LLM calls) | Actual LLM Audit Calls (`audit_calls=1`) | LLM Emitted `PASS` | LLM Emitted `FAIL` | LLM Emitted `UNKNOWN` | True Approvals (`hidden=pass`) | **False Approvals (`hidden=fail`)** | Conditional False-Approval Rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `audit` | 24 | 20 (`17` NO_EV + `3` INCOMPLETE) | **4** | **4/4 (100%)** | 0 | 0 | 1 | **3** | **75.0% (3/4)** |
| `contract+audit` | 7 | 3 (`3` INCOMPLETE) | **4** | **4/4 (100%)** | 0 | 0 | 0 | **4** | **100.0% (4/4)** |
| **Combined** | **31** | **23** | **8** | **8/8 (100%)** | **0/8 (0%)** | **0/8 (0%)** | **1/8 (12.5%)** | **7/8 (87.5%)** | **87.5% (7/8)** |

### 4.3. Forensic Deep-Dive Into All 3 `audit` False Approvals (+ 4th in `contract+audit`)

| Case ID (Arm) | Audit Tokens | Attached Caller Excerpt(s) Fed to Auditor | Proposed Patch Defect | Hidden Test Failure (`verification.txt`) | Why the Auditor Falsely Emitted `PASS` |
| --- | ---: | --- | --- | --- | --- |
| `c_yajl_parse_integer_non_digit` (`audit` & `contract+audit`) | 3,909 / 4,575 | `src/yajl_parser.h:72` & `src/api/yajl_tree.h:118` — **C header prototypes only!** (`long long yajl_parse_integer(...);`) | Set `errno = EINVAL; return 0;` on non-digit instead of `errno = ERANGE; return LLONG_MAX/MIN;` | `Assertion failed: (errno == ERANGE), file c_yajl_parse_integer_non_digit_test.c, line 70` | **Two compounding bugs:** (1) `src/yajl_tree.c` (real caller checking `errno`) was auto-added to chat, excluding it from `candidates`; `TreeSitterCallerFinder._find_regex_call_lines` (`aider/local_callers.py:1386-1412`) matched **C header prototypes** because `_DECL_LINE_RE` (`L137-141`) does not skip C declarations, labeling them `COMPLETE_WITHIN_SCOPE (SELECTED)`. (2) Despite seeing only `.h` prototypes with zero `errno` usage, the LLM auditor emitted `VERDICT: PASS` instead of `VERDICT: UNKNOWN`. |
| `js_dotenv_quiet_from_env` (`audit` & `contract+audit`) | 4,288 / 4,704 | `config.js:1`: `const dotenv = require('./lib/main'); dotenv.config()` (3 lines, zero arguments) | `audit`: checked `parsedAll.QUIET` instead of `DOTENV_QUIET` in custom `processEnv` and inverted precedence; `contract+audit`: failed custom `processEnv` `DOTENV_QUIET=true` | `AssertionError: custom processEnv should honor DOTENV_QUIET=true from .env` (`+ '◇ injected env (2) from .env\n'`, `- ''`) | `config.js:1` shows only a 0-arg `dotenv.config()` call with no custom `processEnv` or quiet-precedence checks. The auditor lacked evidence to verify `processEnv`/precedence semantics, yet rubber-stamped `PASS` instead of `UNKNOWN` or catching the `parsedAll.QUIET` key bug from the task description. |
| `ts_ohash_serialize_map_keys` (`audit` & `contract+audit`) | 4,155 / 4,452 | `src/hash.ts:10` (`digest(serialize(input))`), `src/utils/diff.ts:44` (`serialize(obj)`), `src/utils/is-equal.ts:9` (`serialize(object1) === serialize(object2)`) | Changed `${key}:${this.serialize(value)}` to `${this.serialize(key)}:${this.serialize(value)}` — **omitted `true` second arg (`this.serialize(key, true)`)** | `AssertionError: Serialized Map key must include serialized object key: Map{{'id':1}:'val'}` | Attached callers invoke top-level `serialize(...)` without constructing a `Map` with object keys or showing `serialize`'s second parameter (`isKey`/unquoted string behavior). The auditor failed to trace Map object keys vs. plain-object string keys through the diff and emitted `PASS`. |
| `c_hiredis_map_elements_overflow` (`contract+audit` only; timed out in `audit`) | 3,438 | `hiredis.h:335` — **C header prototype only!** (`int redisGetReplyFromReader(redisContext *c, void **reply);`) | Checked `elements > SIZE_MAX / 2` instead of `LLONG_MAX / 2` (on 64-bit, `SIZE_MAX / 2 == LLONG_MAX`, so `elements = LLONG_MAX / 2 + 1` overflows signed `long long`!) | `Assertion failed: (ctx.err == REDIS_ERR_PROTOCOL), file c_hiredis_map_elements_overflow_test.c, line 23` | Exact same C header prototype bug (`hiredis.c` auto-added to chat -> regex matched `hiredis.h:338`), plus auditor missed the signed `LLONG_MAX / 2` vs unsigned `SIZE_MAX / 2` bug and emitted `PASS`. |

*(The lone true approval was `rust_semver_display_formatter_flags` in `audit`, `audit_tok=4314`, where `src/error.rs:83` called `Display::fmt(self, formatter)` and the patch happened to pass.)*

**Fundamental-Works Grounding:**
- **Miscalibration & Overconfidence (`neural-network-calibration`, Guo et al., pp. 2 & 4, `ea61327ba99a017fc6cf0259`, `bef588d8258cb9bd1dffce88`):** Guo et al. formalize miscalibration as the expectation gap between predicted confidence and empirical accuracy ($\text{ECE} = \mathbb{E}[|P(\hat{Y}=Y|\hat{P}=p)-p|]$) and emphasize that high-capacity neural networks exhibit severe overconfidence requiring post-hoc calibration on a **separate held-out validation set**. Here, whenever `IsolatedCallerAuditor` invoked the LLM (`n=8`), it asserted categorical approval (`PASS`, effective confidence `1.0`) while achieving **`12.5%` accuracy (`1/8`)**—an empirical calibration gap of **`0.875`** and **`0%` sensitivity (`0/7`)** to failing patches.
- **Difficulty of Fine-Grained LLM Verifiers (`deepseek-r1-reasoning`, p. 64, `49684bee0b74b3588404c616`):** Training or prompting an LLM to act as a fine-grained verifier over subtle token/semantic trajectories is inherently fragile—here `qwen3.6-35b-a3b` spent `3,438–4,704` tokens per audit call yet failed to catch `EINVAL` vs `ERANGE`, `parsedAll.QUIET` vs `DOTENV_QUIET`, missing `serialize(key, true)`, and `SIZE_MAX / 2` vs `LLONG_MAX / 2`.
- **Static Heuristic Review vs. Dynamic Executable Testing (`istqb-ctfl`, p. 34 `2fc92971c52b3ea07044cd66` & p. 39 `3901aea857d960c248cfafd1`):** Static review and black-box/white-box dynamic test techniques (equivalence partitioning, boundary value analysis) are complementary—LLM static review over incomplete 7-line caller snippets cannot replace executable dynamic tests for boundary/protocol invariants.

---

## 5. Q4 — Read-Range (`+3/-6`), Unseeded Facts (`+3/-4`, Natural Null Control), and Complete Timeout Anatomy

### 5.1. Unseeded `facts=on` as a Natural Null-Control Measurement of Stochastic Variance

Because the frozen replay repositories contain zero pre-seeded facts (`seed_facts=()`), `facts=on` attached **`0/30` facts (`facts_attached=False` on all 30 tasks)** and added **zero characters of fact context to any prompt**. It is therefore a **natural null-control replicate** of `passive`.

Yet comparing `facts=on` vs `passive` reveals:
- **Pass-rate delta:** `19/30` vs `20/30` (**`+3/-4`**, **`7/30 = 23.3%` discordant flips**, exact McNemar `p = 1.0000`):
  - Wins (`+3`): `c_hiredis_map_elements_overflow` (`ev=True`), `cpp_fmt_dynamic_args_size` (`ev=True`, `passive` timed out), `ts_scule_is_uppercase` (`ev=False`).
  - Losses (`-4`): `c_yajl_decode_null_byte` (`ev=False`), `py_click_show_envvar_sequence` (`ev=False`), `js_dotenv_quiet_from_env` (`ev=True`, **`0`-call timeout**), `rust_semver_display_formatter_flags` (`ev=True`, **`0`-call timeout**).
- **Timeout delta:** **`4` new timeouts** (`cpp_semver_prerelease_zero` lint-loop timeout with `pass=True`; plus **3 `0`-call first-turn runaways** on `js_dotenv_quiet_from_env`, `rust_semver_display_formatter_flags`, `rust_semver_eval_less_prerelease`).
- **Prompt-token drift on identical inputs (`17/30` exact match, `9/26` non-timeout cases differed by `±20–291` tokens):**
  Forensic inspection of `aider.log` and repo trees proves this drift is **not** from `facts`: the exact same `±20–291` token drift appears across `baseline`, `passive`, `audit`, `cymbal`, `read_range`, and `facts` on identical `NO_EVIDENCE` tasks (e.g., `py_click_show_envvar_sequence`: `40239 / 40135 / 40286 / 40169 / 40156`; `cpp_fmt_printf_unsigned_sign`: `12220 / 12035 / 12129 / 12202 / 12326`; `py_itsdangerous_future_timestamp`: `7330 / 7536 / 7530 / 7358 / 7284`). Root cause:
  1. `scripts/local_context_trial/run.py:740` does not set `PYTHONHASHSEED`, so Aider's `RepoMap` (`map_tokens=1024`) tie-breaks differently across fresh Python processes, shifting the 1,024-token repo map slightly.
  2. `aider/models.py:993-1045` sends `temperature=0` (`L1008`) but passes **no `seed`** to LM Studio; combined with MoE GPU non-associativity and small RepoMap prefix perturbations, `qwen3.6-35b-a3b`'s multi-thousand-token `<think>` chains diverge completely across runs.

**Bias-Variance Tradeoff (`ir-textbook`, pp. 348–349, `1efb108765827aa01461cf8e`, `858cff187182773816348b76`):**
As Manning et al. emphasize, *"Variance measures how inconsistent the decisions are, not whether they are correct or incorrect."* With a **single-round (`rounds=1`) null-control flip rate of `7/30` (`23.3%`) and `4` spurious timeouts on a literal no-op**, any single-round delta of `±1` to `±4` tasks across `n = 30` lies squarely inside the **measurement noise floor**. Furthermore, evaluating every Stage 2 arm against Stage 1's single highest-scoring arm (`passive = 20/30`, which won two `1/6` lucky outliers) creates a systematic **winner's curse / regression-to-the-mean penalty** against every Stage 2 ablation (`facts = 19`, `read_range = 17`, `cymbal = 12`).

### 5.2. Read-Range (`read-range=on`) — Complete Stratified Reversal + `max_reflections` Confounder

Stratifying `read_range` (`+3/-6`, `3` new timeouts) by whether `read_range_attached` was actually `True` (`9` cases) vs `False` (`21` cases) reverses the aggregate conclusion:

| Read-Range Stratum | `n` | Passive Pass | Read-Range Pass | Paired (`+w/-l`) | Exact `p` | New Timeouts | Mean 1st-Prompt Token Delta |
| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: |
| **`read_range_attached = True`** | **9** | 5/9 (55.6%) | **6/9 (66.7%)** | **`+2/-1`** | `1.0000` | **`0`** | `+304` tok (`+65` to `+582`) |
| **`read_range_attached = False`** | **21** | 15/21 (71.4%) | 11/21 (52.4%) | **`+1/-5`** | `0.2188` | **`3`** | ~`0` (RepoMap drift only) |
| **Aggregate** | **30** | **20/30 (66.7%)** | **17/30 (56.7%)** | **`+3/-6`** | `0.5078` | **`3`** | — |

**Case-by-Case Inspection of All 9 `read_range_attached=True` Runs (`aider-heldout-3t2l_7do` vs `passive`):**

| Case ID | Category | Ranges Attached | `Δ` 1st-Prompt Tok | Latency (`P` -> `RR`, s) | Outcome (`P` -> `RR`) | Verdict |
| --- | --- | ---: | ---: | ---: | :---: | --- |
| `c_yajl_parse_integer_non_digit` | `direct_caller` | 2 | `+582` | `86.6 -> 91.6` | `fail -> PASS` | **WIN** |
| `cpp_fmt_dynamic_args_size` | `indirect_caller` | 1 | `+312` | `362.2 (TO) -> 118.9` | `fail -> PASS` | **WIN** (resolved timeout!) |
| `js_dotenv_quiet_from_env` | `direct_caller` | 1 | `+65` | `143.7 -> 201.8` | `PASS -> fail` | **LOSS** (`1/6` `passive`-only outlier) |
| `py_packaging_invalid_interpreter_tag` | `direct_caller` | 1 | `+401` | `118.7 -> 103.5` | `PASS -> PASS` | Neutral (faster) |
| `rust_humantime_duration_nsec_carry_overflow` | `direct_caller` | 1 | `+208` | `106.3 -> 83.8` | `PASS -> PASS` | Neutral (faster) |
| `rust_semver_display_formatter_flags` | `control` | 1 | `+152` | `160.8 -> 130.9` | `PASS -> PASS` | Neutral (faster) |
| `c_hiredis_map_elements_overflow` | `direct_caller` | 1 | `+190` | `132.8 -> 175.9` | `fail -> fail` | Neutral |
| `js_semver_inc_identifier_validation` | `indirect_caller` | 3 | `+395` | `69.2 -> 57.2` | `fail -> fail` | Neutral (faster) |
| `ts_ohash_serialize_map_keys` | `direct_caller` | 3 | `+433` | `48.9 -> 41.6` | `fail -> fail` | Neutral (faster) |

On the 9 cases where ReadRange actually ran, it **did not distract or cause a single timeout** (`0/9` timeouts, `6/9` faster than `passive`, `+2/-1` net wins). Why then did `read_range` suffer `3` new timeouts on `read_range_attached=False` cases (`cpp_inih_multiline_no_value`, `cpp_semver_prerelease_zero`, `rust_semver_eval_less_prerelease`)?
In addition to unseeded variance, we identified a **real code confounder at `aider/coders/base_coder.py:472-477`**:

```python
# aider/coders/base_coder.py:472-477 (default Coder.max_reflections = 3 at line 112)
        if self.local_callers and (
            self.local_callers_audit
            or self.local_callers_mode == "contract"
            or self.local_callers_read_range
        ):
            self.max_reflections = max(self.max_reflections, 4)
```

Enabling `--local-callers-read-range` silently bumped `max_reflections` from `3` to `4` **even on `NO_EVIDENCE` tasks**! On `cpp_inih_multiline_no_value` (`read_range_attached=False`), the model solved the bug (`hidden_passed=True`, `smoke_passed=True`) yet hit a false-positive C++ tree-sitter lint error and looped for **`4` reflections (`calls=4`, `16,097` completion tokens)** until hitting the 360s wall-clock cap—whereas `passive` (`max_reflections=3`) stopped at 3 reflections and exited cleanly in `115.5s`!

### 5.3. Complete Timeout Anatomy Across All 27 Timeouts (and All 14 Stage 2 Timeouts)

**Zero of the 27 timeouts (`0/27`) were caused by slow compilation or test execution.** Every single timeout hit 360s inside LLM generation via exactly three mechanisms:

| Timeout Mechanism | All 240 Runs | Stage 1 (150) | Stage 2 `cymbal` (30) | Stage 2 `read_range` (30) | Stage 2 `facts` (30) | Diagnostic Signature in `aider.log` |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| **1. First-turn thinking-token runaway (`calls=0`)** | **10/27 (37.0%)** | 4 | 1 (`rust_json`) | 1 (`rust_json`) | **4** (`rust_semver_eval`, `rust_json`, `js_dotenv`, `rust_semver_display`) | `log_bytes = 314–603`, `editor_calls = 0`, `ptok = None`, `ctok = None`; LM Studio never returned turn 1 within 360s (`max_tokens=32768`, no thinking cap). |
| **2. False-positive C++/Rust auto-lint reflection loop** | **9/27 (33.3%)** | 5 | 1 (`cpp_semver_prerelease_zero`) | **2** (`cpp_inih_multiline_no_value` [PASS!], `cpp_semver_prerelease_zero` [PASS!]) | 1 (`cpp_semver_prerelease_zero` [PASS!]) | `Attempt to fix lint errors? (Y)es/(N)o [Yes]:` auto-accepted by `stdin=DEVNULL`; `2–4` calls (`10k–16k` completion tok). **5/9 already passed hidden tests!** |
| **3. Smoke-test failure reflection loop** | **8/27 (29.6%)** | 4 | 1 (`py_pathspec_gitignore_cr_trim`) | **2** (`py_pathspec_gitignore_cr_trim`, `rust_semver_eval_less_prerelease`) | 1 (`py_pathspec_gitignore_cr_trim`) | `Attempt to fix test errors? (Y)es/(N)o [Yes]:`; `1–2` calls (`4.6k–20.0k` completion tok) retrying broken patches (`py_pathspec_gitignore_cr_trim` accounted for `6/8`!). |
| **Total** | **27/240 (11.3%)** | **13** | **3** | **5** | **6** | **0/27 caused by compile/test runtime** |

**Hidden Harness Defect — False-Positive Tree-Sitter Auto-Lint (`35/240` runs!):**
`scripts/local_context_trial/run.py:744-773` does **not** pass `--no-auto-lint`, so Aider runs its tree-sitter syntax linter on every edited file. Across all 240 attempts, **`35/240` (14.6%)** triggered `Attempt to fix lint errors?`—**`26` in C++**, `6` in Python, `3` in Rust—flagging valid C++ macros (e.g., `FMT_END_NAMESPACE` in `include/fmt/args.h` and `include/fmt/printf.h`, C++20 `detail::range_parser` in `include/semver.hpp`). **`9/35` timed out** (`8` C++, `1` Rust), and `5` of those `9` had **already passed all hidden tests** before Aider trapped them in infinite lint-fix reflections!

**Fundamental-Works Grounding (`deepseek-r1-reasoning`, p. 64, `49684bee0b74b3588404c616`):**
Reasoning models face an exponential token-generation search space; without a tight maximum extension/thinking-token limit (`qwen3.6-35b-a3b` runs with `max_tokens=32768` and `thinking_tokens=None`), a single difficult turn routinely consumes `>15,000` reasoning tokens or hangs past `360s`, and multi-turn reflection loops (`2–4` turns) multiply this latency linearly.

---

## 6. Q5 — Ruthlessly Honest Audit of `passive` (`+4/-1` vs `baseline`, `20/30` vs `17/30`)

Does `passive` (`+10.0pp`, `p = 0.3750`) demonstrate real caller-context lift on `qwen3.6-35b-a3b`? **No—forensic evidence shows the `+4/-1` delta is overwhelmingly noise and timeout luck, not proven caller-retrieval benefit.**

### 6.1. Forensic Audit of All 5 Discordant Pairs (`passive` vs `baseline`)

| Outcome | Case ID | Category | Lang | `passive` `ev` | Attached Excerpt (`passive`) | `baseline` vs `passive` Forensic Cause | Attributable to Caller Context? |
| :---: | --- | --- | --- | :---: | --- | --- | :---: |
| **WIN** | `py_click_show_envvar_sequence` | `control` | `python` | **`False`** | `NO_EVIDENCE` | Both saw zero caller context (`ptok` `40239` vs `40135`). Passed `1/6` across all non-contract arms. | **NO (100% pure variance)** |
| **WIN** | `ts_pathe_resolve_unc_prefix` | `direct_caller` | `typescript` | **`False`** | `NO_EVIDENCE` | Both saw zero caller context, **identical `ptok=9337`**. Flipped `3/6` across arms. | **NO (100% pure variance)** |
| **WIN** | `rust_semver_display_formatter_flags` | `control` | `rust` | `True` | `src/error.rs:83` (`Display::fmt(self, formatter)`) | `baseline` failed **solely because of a `0`-call first-turn timeout (`362.8s`)**, not wrong logic! `cymbal` passed this task with `ev=False` in `137.6s`. | **NO (baseline timeout artifact)** |
| **WIN** | `js_dotenv_quiet_from_env` | `direct_caller` | `javascript` | `True` | `config.js:1` (`dotenv.config()`, `+40` tok) | `config.js:1` contains **no** `processEnv`, `DOTENV_QUIET`, or precedence logic. Every other arm with the exact same `config.js:1` snippet (`audit`, `cymbal`, `read_range`, `facts`, `contract`, `contract+audit`) **failed** (`1/6`). | **UNLIKELY (weak/lucky draw; `1/6` across arms)** |
| **LOSS** | `c_yajl_parse_integer_non_digit` | `direct_caller` | `c` | `True` | `src/yajl_parser.h:72`, `src/api/yajl_tree.h:118` (C header prototypes, `+154` tok) | `baseline` set `errno = ERANGE` (PASS); `passive` set `errno = EINVAL` (FAIL). Note `src/yajl_tree.c` was auto-added to chat in both arms. | **Variance / noisy header prototype** |

### 6.2. Stratified Summary & Auto-Add Confounder

- **On `evidence=False` (`n = 21`):** `passive` `16/21` vs `baseline` `14/21` (`+2/-0`, `p = 0.5000`) — **100% stochastic variance** (`2/4` of `passive`'s wins!).
- **On `evidence=True` (`n = 9`):** `passive` `4/9` vs `baseline` `3/9` (`+2/-1`, `p = 1.0000`) — and excluding the `baseline` 0-call timeout (`rust_semver_display_formatter_flags`), non-timeout evidence-bearing performance is **`+1/-1` (`0` net gain)**!
- **Critical Harness Confounder — Prompt-Mentioned Caller Auto-Add (`11/30` cases):**
  Because `run.py` runs Aider with `stdin=subprocess.DEVNULL`, Aider's `Add file to the chat? (Y)es/(N)o/(D)on't ask again [Yes]:` prompt auto-accepts `[Yes]` whenever the task prompt mentions a caller file (occurring on **`11/30` cases** in `passive`, e.g., `src/yajl_tree.c`, `hiredis.c`, `src/lib.rs`, `src/index.ts`). This has two consequences:
  1. Even **`baseline` already receives the real caller file in full chat context** on those 11 tasks, shrinking the incremental headroom where `--local-callers` can provide genuinely novel information.
  2. Once the real caller file is in `abs_fnames` (editable), `CompositeCallerFinder` excludes it from `candidates` (`aider/local_callers.py:1480`), pushing the finder to match secondary/noisy files (such as C header prototypes `yajl_parser.h` and `hiredis.h`).

---

## 7. Q6 — Evidence-Backed Actionable Next Steps (P0 / P1 / P2)

To avoid conflating software defects with benchmark noise, recommendations are partitioned into **Code Bugs** vs. **Experimental Methodology / Harness Fixes** and ranked strictly by verified empirical impact.

### P0 — Critical Blockers (Fix Immediately Before Any Re-Run)

| ID | Type | Target (`file:line`) | Concrete Fix | Expected Impact | Fundamental-Works Grounding |
| --- | --- | --- | --- | --- | --- |
| **P0.1** | Code Bug | `aider/caller_audit.py:401-407`, `aider/coders/base_coder.py:3339-3345` | **Pass through when `not self._excerpts` in `CallerContractHook`:** When no caller excerpts were retrieved (`NO_EVIDENCE`), return `None` (do not block edits) and gate `base_coder.py:3340` on `bool(contract._excerpts)` (or default `local_callers_policy="advisory"`). Convert contract prompting to positive guidance rather than silent blocking. | Eliminates **`21/30` (70%)** deterministic `no_change` failures in `contract` (`20/30` in `contract+audit`) and **`7/10`** control regressions immediately. | `prompt-engineering` pp. 56–57 (`bac1d6367a4987d87c1f7f18`, `a85c0612f27a0d4af6804b6d`): positive instructions over clashing negative constraints. |
| **P0.2** | Harness & Code Bug | `scripts/local_context_trial/run.py:744-773` (and Aider C++ linter) | **Pass `--no-auto-lint` in replay trials** (and fix tree-sitter C++ macro false positives in Aider's linter separately). | Eliminates **`35/240` wasted reflection cycles** and **`9/27` (33.3%) of all 360s timeouts** (including `5` runs whose patches already passed hidden tests!). | `istqb-ctfl` p. 34 (`2fc92971c52b3ea07044cd66`): static analysis false positives waste debugging effort when dynamic tests already execute. |
| **P0.3** | Harness & Model Config | `aider/models.py:993-1045`, `scripts/local_context_trial/run.py:740-773` | **Bound thinking/output tokens & stabilize determinism:** (a) Pass `PYTHONHASHSEED=0` in `run.py:740` to eliminate `RepoMap` `±20–291` token drift across runs; (b) expose and pass a fixed `seed` to `litellm.completion` for LM Studio; (c) cap `max_tokens` (e.g., `8192–12288`) or configure a reasoning-token budget for `qwen3.6-35b-a3b` instead of unbounded `32768`; (d) run **`rounds >= 3`** (or `>= 5` on `evidence=True` strata) to reduce estimator variance below the `±3.5`-task null-control noise floor. | Eliminates **`10/27` (37.0%)** `0`-call first-turn runaway timeouts and cuts stochastic flip noise (`7/30` on a no-op) so real deltas become statistically detectable. | `deepseek-r1-reasoning` p. 64 (`49684bee0b74b3588404c616`): max-extension bounds for reasoning search; `ir-textbook` pp. 348–349 (`1efb108765827aa01461cf8e`, `858cff187182773816348b76`): bias-variance decomposition. |
| **P0.4** | Code Bug | `aider/local_callers.py:137-141`, `1386-1412` | **Reject C/C++ header declarations/prototypes in `TreeSitterCallerFinder`:** Exclude `.h`/`.hpp` prototype lines (e.g., trailing `);` or tree-sitter `declaration` / `function_declarator` nodes) from `_find_regex_call_lines` and `_find_ts_call_lines`. | Prevents `yajl_parser.h:75`, `yajl_tree.h:121`, and `hiredis.h:338` from being falsely reported as `COMPLETE_WITHIN_SCOPE (SELECTED)` callers (directly eliminating `3/7` audit false approvals). | `rag-survey` p. 8 (`bc4f3e5ab6f2de7a68c06704`): metadata/AST filtering to exclude non-call noise. |

### P1 — High-Impact Subsystem Fixes (Cymbal, Auditor, Read-Range)

| ID | Type | Target (`file:line`) | Concrete Fix | Expected Impact | Fundamental-Works Grounding |
| --- | --- | --- | --- | --- | --- |
| **P1.1** | Code Bug | `aider/cymbal_callers.py:1903-1955`, `2112-2148` | **Fix Cymbal target extraction, error isolation, and precision filtering:** (1) Share `TreeSitterCallerFinder._extract_definitions()` in `_extract_editable_targets()` so C/C++ functions are discovered (`c_hiredis`, `c_yajl`); (2) catch per-symbol `CymbalExecutionError` ("symbol not found") in `find_callers` (`L2112`, `L2131`) so one unindexed C++ template (`dynamic_format_arg_store`) logs a diagnostic instead of aborting the whole turn; (3) align `_extract_python_targets` with `PythonAstCallerFinder` (stop matching bare class names and drop unverified `ambiguous method receiver` / unrelated `transitive_impact` hits unless `use_transitive=True` is explicitly opted in); (4) investigate Cymbal v0.18.0 Rust `refs`/`impact` miss on `parse_duration` and `fmt`. | Closes the `0/12` C/C++/Rust Cymbal recall gap and eliminates the exact 2 noisy-excerpt losses (`py_packaging_invalid_interpreter_tag`, `py_click_show_envvar_sequence`). | `rag-survey` pp. 8 & 11 (`bc4f3e5ab6f2de7a68c06704`, `cda0c6c2c05eca1c55e40d1b`): adaptive retrieval gating & noise filtering; `beir-guide` lines 414–438 (`90905b05c622807ee5f8a78d`). |
| **P1.2** | Code Bug & Calibration | `aider/caller_audit.py:28-60`, `555-573`, `aider/coders/base_coder.py:2571` | **Demote LLM `audit` to opt-in/advisory and recalibrate on a separate dev set:** (1) Strengthen `_AUDITOR_SYSTEM_PROMPT` to *require* `VERDICT: UNKNOWN` whenever caller snippets do not inspect the return value/error code or pass relevant arguments (e.g., `dotenv.config()` with no args); (2) fix `base_coder.py:2571` so auditing still runs if `self._audit_originals` is non-empty even when the final reflection turn made no new edits; (3) calibrate auditor precision/recall on a **non-C2 dev set** of known-bad patches (targeting `<5%` false approval and `>50%` `FAIL` sensitivity) before re-testing on the frozen C2 corpus; (4) prioritize deterministic smoke/unit test feedback (ReAct loop) over LLM self-judging. | Directly addresses the `87.5%` (`7/8`) conditional false-approval rate and `0/8` bug-detection sensitivity. | `neural-network-calibration` pp. 2 & 4 (`ea61327ba99a017fc6cf0259`, `bef588d8258cb9bd1dffce88`); `ir-textbook` p. 320 (`069dc983a616649ad3adc765`); `prompt-engineering` p. 37 (`a6d4e6e89737d00ed31c0eac`); `istqb-ctfl` p. 39 (`3901aea857d960c248cfafd1`). |
| **P1.3** | Code Bug | `aider/coders/base_coder.py:472-477` | **Remove `self.local_callers_read_range` from the `max_reflections = max(..., 4)` condition.** ReadRange is passive pre-turn context and needs zero extra reflection rounds. Re-test `read_range` cleanly on `evidence=True` cases where it showed `+2/-1` (`0` timeouts) at `+304` mean prompt tokens. | Eliminates the `cpp_inih_multiline_no_value` 4-reflection timeout confounder and isolates ReadRange's true positive/neutral effect. | `rag-survey` p. 8 (`bc4f3e5ab6f2de7a68c06704`): Small2Big bounded context completeness without truncation. |

### P2 — Benchmark Methodology & Corpus Hardening

| ID | Type | Target | Concrete Fix | Expected Impact | Fundamental-Works Grounding |
| --- | --- | --- | --- | --- | --- |
| **P2.1** | Methodology | `scripts/local_context_trial/run.py:744-773` | **Prevent silent auto-adding of prompt-mentioned caller files (`--no-suggest-shell-commands` counterpart / auto-decline `Add file to the chat?`)** OR explicitly stratify tasks into `caller_already_in_chat` (`11/30`) vs `caller_not_in_chat` (`19/30`). | Prevents `baseline` from accidentally receiving full caller files via `stdin=DEVNULL` `[Yes]`, restoring clean signal on whether `--local-callers` actually helps when callers are *not* already in chat. | `ir-textbook` p. 198 (`5ecfb939602f76b7331773f5`): isolate information needs where retrieval is actually required. |
| **P2.2** | Methodology | `scripts/local_context_trial/retrieval_benchmark.py` (Section B) | **Expand deterministic no-LLM Section B retrieval benchmark to cover C, C++, Rust, and Bash** (including `.h` header prototypes, C++ templates, and Rust trait impls) and run it as a fast CI gate *before* spending GPU hours on stochastic LLM replay. | Catches Cymbal's `0/12` C/C++/Rust gap and `TreeSitterCallerFinder`'s C-header-prototype bug in `<2` seconds with zero LLM variance. | `beir-guide` lines 414–438 (`90905b05c622807ee5f8a78d`): stratified multi-language generalization. |
| **P2.3** | Methodology | `scripts/local_context_trial/c2_report.py` & Stage 2 protocol | **Avoid winner's-curse comparisons against a single Stage 1 draw:** Compare Stage 2 ablations against a multi-round pooled `passive` baseline (and always report stratified `evidence=True` vs `evidence=False` sub-tables alongside category/language tables). Evaluate `facts=on` on a dedicated **seeded** convention suite to measure positive lift rather than only empty-store overhead. | Prevents single-round regression-to-the-mean artifacts from misattributing `+0/-5` `NO_EVIDENCE` noise to retrieval backends or ReadRange. | `ir-textbook` pp. 320, 348–349 (`069dc983a616649ad3adc765`, `1efb108765827aa01461cf8e`, `858cff187182773816348b76`). |

---

## 8. Oracle Fundamental Works Verification Log

All theoretical citations in this report were verified directly against the live Oracle catalog:

| Source ID | Locator | Passage ID | Application in This Report |
| --- | --- | --- | --- |
| `ir-textbook` | p. 198 | `5ecfb939602f76b7331773f5` | Per-query retrieval effectiveness variance across information needs; Section B vs C2 retrieval gap. |
| `ir-textbook` | p. 320 | `069dc983a616649ad3adc765` | Strict dev vs. frozen held-out test separation (calibrate auditor & fixes on dev set, never tune on C2). |
| `ir-textbook` | pp. 348–349 | `1efb108765827aa01461cf8e`, `858cff187182773816348b76` | Bias-variance tradeoff; interpreting `facts=on` (`+3/-4`, `7/30` flips) as variance floor independent of correctness. |
| `beir-guide` | lines 414–438 | `90905b05c622807ee5f8a78d` | Stratified domain/language evaluation; avoiding opaque single averages across C/C++/Rust/Python/JS/TS/Bash. |
| `rag-survey` | p. 8 | `bc4f3e5ab6f2de7a68c06704` | Retrieval granularity / chunking noise vs. completeness tradeoff; Small2Big (`read_range`) and metadata/AST filtering. |
| `rag-survey` | p. 11 | `cda0c6c2c05eca1c55e40d1b` | Iterative/adaptive retrieval — deciding *when* to retrieve and avoiding accumulation of irrelevant context (`py_packaging`, `py_click`). |
| `deepseek-r1-reasoning` | p. 64 | `49684bee0b74b3588404c616` | Exponential token-search space explosion (`10/27` 0-call runaways under `max_tokens=32768`) and fragility of fine-grained LLM verifiers (`7/8` audit false approvals). |
| `neural-network-calibration` | pp. 2, 4 | `ea61327ba99a017fc6cf0259`, `bef588d8258cb9bd1dffce88` | ECE miscalibration / overconfidence (`100%` `PASS` at `12.5%` accuracy -> `ECE ≈ 0.875`) and requirement of held-out validation for calibration. |
| `prompt-engineering` | pp. 56–57, 37 | `bac1d6367a4987d87c1f7f18`, `a85c0612f27a0d4af6804b6d`, `a6d4e6e89737d00ed31c0eac` | Positive, specific instructions over rigid clashing negative constraints (`CallerContractHook`); ReAct executable tool-feedback loop. |
| `istqb-ctfl` | pp. 34, 39 | `2fc92971c52b3ea07044cd66`, `3901aea857d960c248cfafd1` | Static review vs. black-box/white-box dynamic test techniques; why static LLM review over truncated snippets cannot replace executable dynamic tests. |
