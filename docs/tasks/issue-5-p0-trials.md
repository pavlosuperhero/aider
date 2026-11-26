# Issue #5 P0 trial runbook

[Issue #5](https://github.com/pavlosuperhero/aider/issues/5) uses these checks to verify edit ordering, caller verification, and the trial harness before another model sweep. The original [preregistration](issue-5-preregistration.md) and [240-attempt report](issue-5-benchmark-report.md) record the earlier configuration and results.

## Fast and long profiles

Run from the repository root with a persistent results directory and a loaded local model. Prefix the profile with `--plan` to print the attempt counts without inference or output files.

```bash
trial=scripts/local_context_trial
model=lm_studio/qwen/qwen3.6-35b-a3b
manifest=/tmp/issue5-replay-corpus/manifest.json

bash "$trial/issue5_trials.sh" --plan fast "$model" trial-results/issue5-fast
bash "$trial/issue5_trials.sh" --plan long "$model" trial-results/issue5-long "$manifest"
```

| Profile | Schedule | Use |
| --- | --- | --- |
| Fast | 5 Issue #5 fixtures × passive and audit × 1 round = 10 attempts | Check model connectivity, candidate evaluation and telemetry |
| Long | 30 frozen cases × 5 baseline arms, then 30 per Cymbal, ReadRange and Facts ablation = 240 attempts | Reproduce the earlier sweep's scale. The archived attempts total 9 h 31 min. Runtime depends on the model, hardware and timeouts. |

Run the offline gate and check that LM Studio and Oracle are reachable before starting either profile. Start with one canary and check its artifacts before launching the fast profile. Keep the replay manifest, source snapshots and hidden-check assets available across a reboot. Put outputs on persistent storage rather than in `/tmp`.

```bash
.venv/bin/python "$trial/heldout.py" --checkpoint --suite issue5 \
  --cases caller_multi_scope --arms passive --rounds 1 --specs vague \
  --seed 20261123 --map-tokens 1024 --model "$model" \
  --local-callers-backend builtin --no-local-callers-read-range --no-facts --no-auto-lint \
  --out trial-results/issue5-fast/canary.jsonl
```

Verify a completed editor call, input and output token usage, and the Oracle foundation message in `aider.log`. Check `verification.txt` and the independent outcome fields in the JSONL. The passive canary should show `contract_rejected=false` and `commits_added=0`. A recorded model miss with usable telemetry can still validate the pipeline.

After the canary gate, run and summarize the fast profile.

```bash
bash "$trial/issue5_trials.sh" fast "$model" trial-results/issue5-fast
.venv/bin/python "$trial/heldout.py" --summarize --out trial-results/issue5-fast/fast.jsonl
```

Review the fast summary and verify the provider settings before starting a separate long calibration.

```bash
bash "$trial/issue5_trials.sh" long "$model" trial-results/issue5-long "$manifest"
```

For `lm_studio/qwen/qwen3.6-35b-a3b`, `aider/resources/model-settings.yml` pins `use_temperature: 0.6` and `extra_params.max_tokens: 32768` so Aider does not override LM Studio with greedy `temperature=0`. Do **not** set `TRIAL_THINKING_TOKENS` / `--thinking-tokens` for `lm_studio/qwen/qwen3.6-35b-a3b`: neither LiteLLM's `LMStudioChatConfig` nor LM Studio's `/v1/chat/completions` honors Anthropic `thinking.budget_tokens` (and `heldout.py` fails fast when `--thinking-tokens` is requested for a model whose `accepts_settings` or LiteLLM provider drops or ignores it). To bound reasoning or disable thinking on LM Studio, use `max_tokens` or configure server-side LM Studio model/preset settings (for example, `reasoning_effort: "none"` to toggle thinking off). Reserve `TRIAL_THINKING_TOKENS` (for example, `TRIAL_THINKING_TOKENS=4096`) for models whose `accepts_settings` includes `thinking_tokens` and whose LiteLLM provider retains `thinking`/`reasoning` (such as `claude-3-7-sonnet-20250219` or `claude-sonnet-4-6`). Leave `TRIAL_THINKING_TOKENS` unset to omit `--thinking-tokens`.

### Resume after an interruption

Run the same profile command with the same model, manifest and results directory. Each completed attempt is appended to its stage JSONL and synced to disk before the next attempt starts. The atomic metadata sidecar stores the run ID and an input fingerprint. The runner skips completed attempts, discards an incomplete final JSONL line, and retries the interrupted attempt in a fresh repository. A changed configuration, fixture, or trial source requires a new results directory. Keep the local model settings and Oracle configuration fixed across sessions.

The long profile stores four JSONL files and adjacent metadata. Resume checks completed stages before advancing to the next stage. You can summarize a stage at any saved point with `heldout.py --summarize --out PATH` after the runner has stopped and recovered any incomplete final line. Each stage keeps diagnostic artifacts beside its JSONL. Completed timeout and error rows remain recorded outcomes.

After all four stages finish, generate the paired report.

```bash
long_results=trial-results/issue5-long
.venv/bin/python "$trial/c2_report.py" --manifest "$manifest" \
  --stage1-jsonl "$long_results/stage1.jsonl" \
  --stage2-jsonl "$long_results/stage2-cymbal.jsonl" \
  --stage2-jsonl "$long_results/stage2-readrange.jsonl" \
  --stage2-jsonl "$long_results/stage2-facts.jsonl" \
  --out-md "$long_results/report.md" --out-json "$long_results/report.json"
```

## Offline gate

Run the offline checks from the repository root. Resolve any failure before starting model calibration.

```bash
py=.venv/bin/python
trial=scripts/local_context_trial
manifest=/tmp/issue5-replay-corpus/manifest.json
CYMBAL_CACHE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/issue5-cymbal.XXXXXX")
export CYMBAL_CACHE_DIR

LITELLM_LOCAL_MODEL_COST_MAP=True "$py" -m pytest -q \
  tests/basic/test_{edit_file_mentions,coder,editblock,wholefile,udiff,edit_hooks,local_callers,caller_audit,local_context_integration,cymbal_callers,read_range,project_facts,local_context_trial,requests_trial,retrieval_benchmark,build_replay_manifest,c2_report,exceptions,oracle_integration,tdd,tdd_integration,test_result_hooks}.py \
  "$trial/test_replay_corpus.py"
for suite in synthetic issue5 requests; do
  "$py" "$trial/heldout.py" --suite "$suite" --check-fixtures
done
"$py" "$trial/replay_corpus.py" validate "$manifest" --require-frozen --run-checks
"$py" "$trial/heldout.py" --suite replay --replay-manifest "$manifest" --check-fixtures
```

Expect 13 synthetic, five Issue #5, two Requests and 30 historical replay fixture checks. The Requests JSON case expects smoke and hidden failure on the seeded code. Its focused test also checks partial repairs and a complete repair. The offline edit-flow tests exercise real commits and the observable file and tool state across hook vetoes. Run the pinned Cymbal smoke with `cymbal v0.18.0` on `PATH` and check that pytest executes it.

## Local model calibration

Use a fresh output directory for each session and preserve the JSONL, adjacent metadata, and per-case diagnostics. The ten-attempt pilot pairs passive with passive plus audit on the five Issue #5 fixtures. It measures harness behavior on known cases.

```sh
export LM_STUDIO_API_BASE=http://127.0.0.1:1234/v1
export LM_STUDIO_API_KEY=dummy-api-key
model=lm_studio/qwen/qwen3.6-35b-a3b
out_dir=$(mktemp -d "${TMPDIR:-/tmp}/issue5-p0.XXXXXX")
curl --fail --silent --show-error "$LM_STUDIO_API_BASE/models"

"$py" "$trial/heldout.py" --suite issue5 --arms passive,audit \
  --rounds 1 --specs vague --seed 20261123 --map-tokens 1024 --model "$model" \
  --local-callers-backend builtin --no-local-callers-read-range --no-facts \
  --no-auto-lint --out "$out_dir/fixture-pilot.jsonl"
"$py" "$trial/heldout.py" --summarize --out "$out_dir/fixture-pilot.jsonl"
```

Check that both arms return five rows, usage records include editor calls, and `contract_rejected` stays false in both arms. To exercise format-block telemetry, run `--arms passive,contract` on the historical corpus with `--suite replay --replay-manifest "$manifest"` and a fresh output filename. That calibration schedules 60 attempts. Run two separate `--arms passive` jobs with the same configuration and different output filenames to measure A/A disagreement. Match rows by case, spec and round.

Record the served model revision, quantization, inference settings and timeout. The harness pins `PYTHONHASHSEED=0`, disables automatic file-mention prompts, auto-commits and dirty commits, isolates configuration with `--config /dev/null --model-settings-file --no-default-model-settings`, runs a pre-attempt #1 `aider --exit` preflight gate, and uses `--seed` for job order. Verify model sampling seed and server-side thinking/reasoning settings at the provider before treating them as controlled. The harness validates `--thinking-tokens` against `accepts_settings` and LiteLLM provider parameter support before running, enforces a per-row `TrialParameterDriftError` gate, records `config_provenance`, LM Studio `/api/v0/models` `provider_provenance`, `requested_thinking_tokens`, and `effective_model_params` in run metadata, and parses `Trial model params:`, `Audit response tail:` (on `PASS`/`FAIL`/`UNKNOWN`), and any `ignored_settings` warnings from `aider.log` into each JSONL row. `heldout.py --summarize` and `c2_report.py` report a 4-axis auditor breakdown (`hidden_passed`, `audit_pass`, `false_approval`, `false_abstention`), uncensored latency alongside timeout censoring, and a provenance summary block.

## Fresh paired replay

Freeze a fresh external manifest on unseen tasks before model execution, using the [preregistration criteria](issue-5-preregistration.md). Check disjointness from the historical corpus by repository and fix identity, validate the frozen digest and executable oracles, and keep hidden checks outside candidate worktrees. Select a clean, fixed revision and preserve its SHA. Predeclare case counts, the control non-inferiority margin, and the stopping rule for both models.

```sh
: "${fresh_manifest:?Set fresh_manifest to the newly frozen external manifest}"
"$py" "$trial/replay_corpus.py" validate "$fresh_manifest" --require-frozen --run-checks
"$py" "$trial/heldout.py" --suite replay --replay-manifest "$fresh_manifest" --check-fixtures

"$py" "$trial/heldout.py" --suite replay --replay-manifest "$fresh_manifest" \
  --arms baseline,passive,contract,audit,contract+audit \
  --rounds 3 --specs vague --seed 20261123 --map-tokens 1024 --model "$model" \
  --local-callers-backend builtin --no-local-callers-read-range --no-facts \
  --no-auto-lint --checkpoint --out trial-results/issue5-fresh/qwen-stage1.jsonl
```

Repeat for the preregistered 8B model using a distinct output file. For each Stage 2 comparison, use `--arms passive`, keep the model and other parameters pinned, and change only the listed control flags from Stage 1.

| Comparison | Control flags to replace |
| --- | --- |
| Cymbal | `--local-callers-backend builtin,cymbal` |
| ReadRange | `--read-range-modes off,on` in place of `--no-local-callers-read-range` |
| Facts | `--facts-modes off,on` in place of `--no-facts` |

Empty-store Facts runs measure overhead and control regressions. Use seeded convention fixtures to measure the effect of attaching a relevant fact.

Report `patch_hidden_passed`, `strict_accepted`, and `committed` individually with false approvals, UNKNOWN/ERROR, timeouts, reflections, input/output tokens and p50/p95 latency. Pair by case, spec and round, and report wins/losses and confidence intervals by category and language. Rows describe the final candidate in a turn that can include reflections. First-proposal acceptance requires evidence about the first editor call. Exercise real auto-commit behavior in the offline tests. Keep the features opt-in pending the issue’s behavioral gates.

## Branch `feat/issue-5-b1-d1` status, archive & convergence gates

### Verified commit progression

- **`955617d`**: C/C++ prototype filtering, builtin `>128 KiB` skip → `INCOMPLETE`, Cymbal C/C++ targets, auditor prompt calibration, `TRIAL_THINKING_TOKENS`.
- **`f03e940`**: Qwen3.6 `use_temperature: 0.6`, fail-fast `--thinking-tokens` validation (LM Studio `/v1/chat/completions` ignores Anthropic `thinking.budget_tokens` and LiteLLM drops `thinking`/`reasoning_effort` for `lm_studio/*`), Cymbal scan-cap/unreadable/stale skips → `INCOMPLETE`, C/C++ non-header declaration alignment.
- **`66e0df0`**: Zero-excerpt search ambiguity → `INCOMPLETE` in builtin and Cymbal.
- **`a4b0a3a`**: Hermetic `--config /dev/null --model-settings-file`, pre-attempt #1 `aider --exit` preflight gate, `config_provenance`, and LM Studio `/api/v0/models` `provider_provenance`.
- **`e67a2a7`**: Fixed `only_target_modified` to non-empty subset of `targets`, pre-verification `git diff`/`git status` capture, per-row `TrialParameterDriftError` gate, offline `rescore.py`, 5-example class-mixed few-shot auditor calibration (`UNKNOWN`/`PASS`/`FAIL` interleaved), and `Audit response tail:` logging on `PASS`/`FAIL`/`UNKNOWN`.
- **`57d664e`**: Phase 1 root-cause TDD fixes — same-file caller discovery (cross-file ranked first), vague-prompt target seeding (`select_target_definitions`: exact → subtoken → bounded exported fallback), Bash function & command call extraction + shebang detection, Rust homonymous trait-method ambiguity guard (`ambiguous trait method receiver` → `INCOMPLETE`), zero private `_...` imports from `local_callers` in `cymbal_callers`, explicit `FallbackCallerFinder` `auto`-mode contract, and `--no-default-model-settings` true hermetic provenance.
- **Working-tree Phase 2A zero-LLM tooling**: held-out `scripts/local_context_trial/auditor_calibration.py` (`--plan`, `--dry-run`, `--model`, `0 false-PASS` safety gate), upgraded `c2_report.py` & `heldout.py` (4-axis auditor breakdown: `hidden_passed`, `audit_pass`, `false_approval`, `false_abstention`; timeout censoring / uncensored latency; provenance summary block), and Cymbal Bash target extraction parity.

### Immutable legacy diagnostic archive (`a4b0a3a`, 92/150 Stage 1 rows)

- Stored in `trial-results/issue5-long-a4b0a3a/` (`stage1.jsonl`, `stage1.rescored.jsonl`, `stage1.meta.json`); preserved as an immutable diagnostic archive (**NO-GO** to mix with post-`e67a2a7`/`57d664e` runs).
- Verified manifest SHA: `c00e46d1fb3b00895c02846625e69446ce40b3274de19091f9017c9779d0a429` (`Validation OK`).
- Offline rescore (`repo=92, patch_only=0, changed=2`, flipping jobs 11 & 39 on `js_dotenv_quiet_from_env`): `baseline 9/18`, `passive 11/20`, `contract 10/17`, `audit 9/19` (`2` false approvals, `strict 0/19`), `contract+audit 12/18` (`1` false approval, `strict 1/18`); `6/92` timeouts; non-baseline evidence `29/74 (39.2%)` (`25/28` caller-dependent misses were `Definitions: none` vague-prompt misses, `3/28` were Bash call syntax).

### Phase 2A deterministic retrieval & environment audit (`57d664e`, 0 LLM calls)

- **`retrieval_benchmark.py` (10 Section B cases, `cymbal v0.18.0`):** `b10_negative_control` scored `P=1.000 / R=1.000 / F1=1.000 / Wrong Target=0.000` on both `builtin` and `cymbal`; `cymbal` wins `large_file` (`+1.000` F1) and `transitive` (`+0.500` Recall); `builtin` wins `aliases` and `same_name`.
- **30-case frozen replay corpus (vague prompts, clean base repos):** `builtin` evidence jumped from `39.2%` (`a4b0a3a`) to **`26/30 (87%)`** (`10/10` on `direct_caller`, `7/10` on `indirect_caller`, `9/10` on `control`; `py_pathspec_gitignore_cr_trim` now `COMPLETE_WITHIN_SCOPE`; Job 23 `c_yajl` finds both `src/yajl_tree.c:313` and same-file `src/yajl_parser.c:287`; Job 58 `rust_semver` flags `src/error.rs:86` as `INCOMPLETE` `ambiguous trait method receiver`).
- **Runtime dependency audit:** Python `3.12.13`, `cymbal v0.18.0` (exact pin match), `tree-sitter-language-pack 0.13.0` (exact pin match), `litellm 1.110.0`. Note `.venv` drift vs `common-constraints.txt` for `tree-sitter` (`0.26.1` vs `0.25.2`), `pytest` (`9.2.0` vs `9.0.2`), and `pydantic` (`2.14.3` vs `2.12.5`).

### Phase 2B & Phase 3 convergence gates

1. **Phase 2B — Held-out auditor calibration:**
   ```bash
   .venv/bin/python scripts/local_context_trial/auditor_calibration.py --plan
   .venv/bin/python scripts/local_context_trial/auditor_calibration.py --dry-run
   .venv/bin/python scripts/local_context_trial/auditor_calibration.py --model lm_studio/qwen/qwen3.6-35b-a3b
   ```
   **Gate:** `0 false-PASS` and `deterministic_gate_violations == 0`.
2. **Phase 2C — Short diagnostic & A/A stochasticity pilot:** Run `bash scripts/local_context_trial/issue5_trials.sh fast "$model" <fresh_out_dir>` plus an A/A baseline stochasticity pilot before sizing Phase 3 rounds.
3. **Phase 3 — Fresh controlled Stage 1 + Stage 2 factorial:** Run on clean `HEAD` into a new `out_dir` (never append into `trial-results/issue5-long-a4b0a3a/`). Keep `--local-callers-audit` strictly opt-in until `0 false-PASS` holds on both held-out calibration and frozen replay.

## Priority status

| Priority | Verified or prepared | Still open |
| --- | --- | --- |
| P0 | Edit-before-mention regression, focused hook and strict-audit cases, evidence/error distinction (`>128 KiB`, zero-excerpt ambiguity, Cymbal inspection skips, Rust homonymous trait-method guard), fixture checks, harness classification, checkpoint recovery, hermetic `--config /dev/null --model-settings-file --no-default-model-settings`, preflight gate, per-row `TrialParameterDriftError` gate, fail-fast `--thinking-tokens` validation, pre-verification diff capture, `only_target_modified` subset fix, and offline `rescore.py`. | Remaining veto and commit policy cases, first-editor-call acceptance metrics, deduplication when evidence changes, cross-platform CI, C/C++ header lint behavior, `.venv` constraint drift reconciliation (`tree-sitter`, `pytest`, `pydantic`), and provider-side seed/thinking controls. |
| P1 | Phase 1 retrieval fixes (same-file callers, vague-prompt target seeding, Bash calls/shebang + Cymbal Bash parity, C/C++ declarations/prototypes), Phase 2A 0-LLM audit (`retrieval_benchmark.py`, `26/30 (87%)` frozen replay evidence), held-out `auditor_calibration.py`, 4-axis `c2_report.py`/`heldout.py` reporting, and immutable `a4b0a3a` archive. | Phase 2B live `auditor_calibration.py` run (`0 false-PASS` gate), targeted ReadRange and seeded Facts efficacy runs, versioned release and reproducible artifact archival. |
| P2 | Frozen 30-case historical corpus (`c00e46d1...`), resumable 240-attempt calibration profile, and immutable `a4b0a3a` diagnostic archive (`92/150` rows). | Phase 2C fast + A/A pilot, Phase 3 fresh controlled Stage 1 + Stage 2 factorial on clean `HEAD`, new unseen external corpus, 8B model sweep, independent acceptance/safety gates, and opt-in vs default-on release decision. |
