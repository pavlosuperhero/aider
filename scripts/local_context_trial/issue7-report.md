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

The pre-commit control is covered by deterministic Git tests. The small trial supplies two observed false approvals and no measured improvement in patch correctness. The full model trial remains for a separate run.
