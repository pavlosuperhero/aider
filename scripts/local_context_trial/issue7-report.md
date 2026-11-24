# Caller verification before commit

With `--local-callers --local-callers-audit`, Aider writes the proposal to the working tree so the configured linter and tests can run. It audits the cumulative patch against selected caller excerpts, then checks that the patch still matches the audited version before auto-committing. A corrective reflection can edit a second file. Both files remain pending until acceptance.

Strict mode is the default for this opt-in feature. FAIL can request a correction within the existing reflection budget. UNKNOWN covers missing evidence, omitted hunks, ambiguous caller bindings, and missing or malformed final verdicts. A backend exception produces ERROR. A rejected edit remains available for inspection with `git status` and `git diff`. In a later task, Aider protects unresolved edits to the same path from automatic dirty-file commits. With `--no-auto-commits`, the user controls Git. Explicit `--local-callers-policy advisory` allows acceptance after a non-passing audit and retains the actual verdict in the log.

Contract mode checks the placement of the editor’s explanation. The log labels the trace as semantically unverified. In strict mode, exhausting the format retries stops the edit before writing. PASS from the independent auditor applies only to the selected direct callers.

## Design evidence retrieved from Oracle

| Source | Application |
|---|---|
| *Think Python*, second edition, §4.10 and §6.9, indexed PDF pages 58 and 81 | Test the values returned by the changed function through a real caller. The hidden check can disagree with a model’s trace. |
| *Architecture Patterns with Python*, chapter 4, “Our First Use Case”, Oracle passage `354853d1e08d13856ad0eb16` | Keep the decision to persist at the workflow boundary. Integration tests observe the actual Git commit while a fake verifier returns deterministic verdicts. |
| PEP 20, Oracle lines 1–63 | Represent missing evidence and backend failures explicitly. The user chooses advisory behavior. |

## Reproduction

Run the commands from the repository root with the project environment active, the Oracle service available and the model served by LM Studio. Use a fresh output filename for each experiment. The runner appends one record per attempt.

```sh
python scripts/local_context_trial/heldout.py --check-fixtures
python scripts/local_context_trial/heldout.py --suite requests --check-fixtures

python scripts/local_context_trial/heldout.py --rounds 1 \
    --cases py_noisy,js_slug --specs vague,explicit \
    --arms baseline,passive,contract,audit,contract+audit \
    --model lm_studio/qwen/qwen3.6-35b-a3b --out small.jsonl

python scripts/local_context_trial/heldout.py --rounds 3 \
    --specs vague,explicit --arms baseline,passive,contract,audit,contract+audit \
    --model lm_studio/qwen/qwen3.6-35b-a3b --out full.jsonl
python scripts/local_context_trial/heldout.py --summarize --out full.jsonl
```

The optional `--suite requests` uses the installed source of `requests==2.34.2`. It seeds two independent multi-file tasks. One exercises `PreparedRequest.copy()` through header and cookie classes with same-name methods and an indirect copy helper. The other exercises the aliased public JSON exception and its pickle contract. Each starting fixture passes smoke and fails its hidden check. Restoring the two upstream files makes the hidden check pass in both cases. To measure model performance on these tasks, add `--suite requests` to the full-run command and choose a separate output file.

## Small trial

The Python fixture contains misleading same-name code and an unrelated import. The JavaScript control checks slug formatting. Each arm receives the same four combinations of case and spec. A pass requires both the hidden check and the allowed-file check on the final working tree. A rejected edit can still pass the hidden check because strict mode preserves it for inspection.

Extra calls count recorded editor and audit completions beyond the first editor response in each attempt. Token totals include both kinds of completion. Latency includes running Aider and checking the fixture. A false approval is a final audit PASS on a patch that fails independent verification. UNKNOWN counts verdicts across attempts. Four paired observations per arm support a smoke comparison only.

### Observed results on Qwen 3.6 35B

| Arm | Hidden checks passed | Paired wins / losses vs baseline | Extra calls | Total tokens | Mean seconds | UNKNOWN |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | 3/4 | — | 0 | 29,553 | 41.3 | — |
| Passive | 3/4 | 0/0 | 0 | 29,364 | 41.2 | — |
| Contract | 3/4 | 0/0 | 0 | 32,996 | 50.8 | — |
| Audit | 3/4 | 0/0 | 5 | 47,327 | 78.4 | 4 |
| Contract + audit | 3/4 | 0/0 | 4 | 44,265 | 84.2 | 4 |

Every arm missed the vague Python case. The eight initial audits produced zero approvals and zero false approvals. Captured diagnostics showed that the model sometimes numbered its verdict as the fourth step. The final parser accepts that prefix and continues to reject competing verdicts and qualified passes.

After that change, both audit arms reran the vague Python case. Both produced PASS and both failed the hidden check. For input `42 @ ops`, the task’s hidden expectation is group `ops` and code `42`. Both implementations returned the fields in input order. The caller shows how the fields are consumed, while the vague spec leaves the input convention implicit. On separate price-format probes, the final auditor returned PASS for a plain decimal and FAIL for a currency-prefixed string that violated an explicit caller assertion.

The pre-commit control is covered by deterministic Git tests. The small trial was too small to separate arms (four pairs per arm).

## Full 130-run trial on Qwen 3.6 35B

After the review fixes (`11bb246`: scoping JS import checks to JS/TS, truncating long `FAIL` reasons instead of turning them into `UNKNOWN`, prioritizing explicit patch markers in `CallerContractHook`, adding exponential-backoff retries to `_complete_caller_audit`, setting `reasoning_tag: think`, and handling subprocess timeouts in `run.py`), we ran a 10-run smoke check (`qwen36_35b_short_v2.jsonl`, 0 unknowns, 0 errors) followed by the full 130-run synthetic suite (`13 cases × 2 specs × 5 arms × 1 round`, `qwen36_35b_full_130.jsonl`):

```sh
python scripts/local_context_trial/heldout.py --rounds 1 \
    --specs vague,explicit --arms baseline,passive,contract,audit,contract+audit \
    --model lm_studio/qwen/qwen3.6-35b-a3b \
    --out scripts/local_context_trial/qwen36_35b_full_130.jsonl
```

### Observed 130-run results (`n = 26` paired `(case, spec)` tasks per arm)

| Arm | Overall pass | Caller `vague` | Caller `explicit` | Paired vs `baseline` (sign test) | Paired vs `passive` | Mean s | Mean tokens | Extra calls | False approvals | `UNKNOWN` | `FAIL` | Timeouts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `baseline` | 14/26 (54%) | 2/9 | 6/9 | — | — | 49.5 | 7,916 | 3 | — | — | — | 1 |
| `passive` | 22/26 (85%) | 8/9 | 9/9 | **+10/−2 (`p=0.0386`)** | — | **41.7** | 9,349 | 10 | — | — | — | 0 |
| `contract` | 22/26 (85%) | 6/9 | 9/9 | **+8/−0 (`p=0.0078`)** | +2/−2 (`p=1.0`) | 66.2 | 7,597* | 0 | — | — | — | 2 |
| `audit` | 22/26 (85%) | 7/9 | 9/9 | **+9/−1 (`p=0.0215`)** | +1/−1 (`p=1.0`) | 76.8 | 12,320 | 36 | 4 | 2 | 0 | 0 |
| `contract+audit` | 23/26 (88%) | 7/9 | 9/9 | **+9/−0 (`p=0.0039`)** | +3/−2 (`p=1.0`) | 80.5 | 10,743 | 26 | 3 | 3 | 0 | 0 |

*\*`contract` mean tokens computed over the 24 non-timed-out runs; 2 runs (`js_price(vague)` and `names(vague)`) hit the 360s timeout with `tokens=None`. Zero `ERROR` verdicts and zero contract rejections occurred across all 130 runs.*

### Conclusions

1. **Attaching local caller evidence (`--local-callers`, i.e. `passive`) produces a large, statistically significant gain over `baseline`:** pass rate rose from **54% (14/26)** to **85–88% (22–23/26)** across every local-caller arm (`p < 0.05` for all four), including **36/36 (100%)** on `caller/explicit` vs. **6/9 (67%)** for `baseline` and **8/9 (89%)** for `passive` on `caller/vague` vs. **2/9 (22%)** for `baseline`.
2. **Adding `contract` or `audit` on top of `passive` produced no measurable accuracy gain on this thinking model (`p = 1.0` vs. `passive`):** `passive` was the fastest arm overall (**41.7s** mean, faster than `baseline` at 49.5s) with zero timeouts.
3. **Self-auditing with the same thinking model caught 0 bugs across 52 audited runs:** the auditor issued **0 `FAIL`** verdicts, falsely approved all **7/7** incorrect patches that reached it (`audit`: 4/4, `contract+audit`: 3/3), and returned **5 `UNKNOWN`** verdicts (all 5 on patches that passed hidden checks), at ~1.8–1.9× the latency of `passive`.
4. **Real-repository `requests` suite note:** both starting fixtures pass self-checks (`2/2`), but probing `find_related_callers()` shows `requests_copy` produces `INCOMPLETE (ambiguous method receiver)` and `requests_json` produces `NO_EVIDENCE`, so under strict mode they exercise fail-closed gating rather than LLM audit accuracy (deeper receiver/indirect-caller resolution is tracked in #5).
