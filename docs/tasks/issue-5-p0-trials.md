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

For thinking models such as `qwen/qwen3.6-35b-a3b`, set `TRIAL_THINKING_TOKENS` (for example, `TRIAL_THINKING_TOKENS=4096 bash "$trial/issue5_trials.sh" fast "$model" trial-results/issue5-fast`) to forward `--thinking-tokens` and bound multi-minute runaway reasoning turns. Leave `TRIAL_THINKING_TOKENS` unset to omit `--thinking-tokens`.

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

Record the served model revision, quantization, inference settings and timeout. The harness pins `PYTHONHASHSEED=0`, disables automatic file-mention prompts, auto-commits and dirty commits, and uses `--seed` for job order. Verify model sampling seed and thinking limits at the provider before treating them as controlled. The harness records the requested `--thinking-tokens` value when supplied.

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

## Priority status

| Priority | Verified or prepared | Still open |
| --- | --- | --- |
| P0 | Edit-before-mention regression, focused hook and strict-audit cases, evidence/error distinction, fixture checks, harness classification and checkpoint recovery. Ordinary auto-commit waits for configured automatic tests and a failed candidate stays in the working tree. Replay pins Python hash seed and disables automatic file additions, lint and both commit modes. | Complete the remaining veto and commit policy cases, first-editor-call acceptance metrics, deduplication when evidence changes, cross-platform CI, C/C++ header lint behavior, provider seed and thinking limits. |
| P1 | Historical retrieval benchmark and opt-in documentation exist. The long profile has separate ablations and a persistent artifact directory per stage. | Expanded language and caller-resolution coverage, independently labeled auditor evaluation, targeted ReadRange and seeded Facts efficacy runs, versioned release and reproducible artifact archival. |
| P2 | Frozen historical corpus and resumable 240-attempt calibration profile. | New unseen corpus, at least three paired rounds with A/A controls, independent acceptance and safety gates, and the release decision. |
