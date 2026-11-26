# ReasoningBank trial evidence, 26 November 2026

The [ReasoningBank genesis](reasoningbank-genesis-2026-11-26.md) reconstructs changes from Git history. This companion records what the available trial runs measured and which findings can guide future repair checks.

The [JSONL inventory](reasoningbank-trial-inventory-2026-11-26.jsonl) covers **705 distinct attempts** from **22 source files**. Those files contain 747 rows. The 42-row interim C2 export matches the opening 42 rows of the final Stage 1 file after removing `patch` and `workdir`, which the export omitted. The inventory preserves both source hashes and counts those attempts once. Separate attempts on the same task remain separate records.

## What the trials show

### Caller context and verification

- The legacy two-arm fixtures favored passive context in both recorded series, from **3/15 to 8/15** and from **23/42 to 39/42**. The 130-attempt Qwen 3.6 sweep recorded **14/26** baseline, **22/26** passive, **22/26** contract, **22/26** audit and **23/26** contract plus audit. Seven audit false approvals were logged in that sweep. The two numbered-verdict probes both failed while logging false approval. A parsable verdict therefore still needs a task-relevant independent check. See the [Issue #7 trial report](../../scripts/local_context_trial/issue7-report.md).
- Two temporary Issue #7 pilots add four Qwen 3.6 attempts with **2/4** recorded passes, and eight Qwen3 Coder 30B attempts with no candidate patches. Each of those eight logs reports that LM Studio stopped model loading. Count them as attempted jobs and infrastructure failures. They provide no comparison of patch quality.
- The 240-attempt C2 replay recorded **17/30** baseline, **20/30** passive, **19/30** audit, **3/30** contract and **3/30** contract plus audit. Passive had four paired wins and one loss against baseline. The strong contract-mode regression and seven logged false approvals justify keeping these modes opt-in. The earlier synthetic improvement did not establish the effect on real-repository repairs. See the [C2 report](issue-5-benchmark-report.md) and [forensic analysis](issue-5-c2-deep-research.md).
- C2 report files contain **27 timeouts in 240 attempts**. The recorded attempt durations sum to **9 h 31 min**. Stage 1 records `ac5fcd651073` and Stage 2 records `b2e6ca399ad5`; both report a dirty checkout. Configuration, revision and timing differences constrain comparisons across stages. The C2 output describes candidate state after a turn that may include reflections. It cannot establish first-editor-call or commit acceptance.

### Cymbal, ReadRange and Facts

- In the large-file pilot, Cymbal solved **3/3** against built-in **0/3** both before and after the file-mention fix. For Cymbal, cumulative input fell from **98,181 to 5,183 tokens per attempt** and editor calls fell from **two to one**. First-editor input already measured 5,183 tokens before the fix. This supports the specific diagnosis that read-only context re-entered the chat as a full file, and gives future repairs a direct token and call-count regression check. The one-round follow-up across five fixtures was **5/5** Cymbal and **4/5** built-in.
- Across the 60-attempt seeded Issue #5 pilot, Facts improved each ReadRange setting from **6/15 to 12/15**, with wins concentrated in currency and sentinel conventions. The stale-fact case passed all six Facts-enabled attempts without a stale attachment. The real-repository Facts arm attached **0/30** facts and scored **19/30** against passive **20/30**. Its seven paired flips are an empty-store control observation. Evidence that attached facts help comes from the seeded fixtures.
- C2 Cymbal scored **12/30** and ReadRange **17/30** against passive **20/30**. Cymbal had zero paired wins and eight losses. ReadRange had three wins and six losses. These are signals to investigate resolution, attachment and reflection behavior with fixed controls. They do not establish the isolated effect of a snippet or range. See the [deterministic retrieval benchmark](issue-5-retrieval-benchmark.md).

### Requests smoke coverage

- The 12-row Requests backend pilot records **6/6** smoke passes and **0/6** hidden passes on `requests_json` across built-in and Cymbal. The six `requests_copy` attempts passed. Another 12-row Requests run recorded **8/12** overall. The isolated legacy Requests row recorded a pass through the older composite field. These are separate trials and preserve the different evidence each harness provided.
- The current Requests fixture exercises the JSON exception attributes and pickle round trip on both encoding paths. Treat the old smoke-green, hidden-red candidates as unresolved examples. A future repair episode needs an executable failing baseline, an independently checked candidate and the exact revision and patch identity.

## Source inventory

`Pass` is each JSONL's recorded `passed` field. `Hidden` counts separately recorded hidden outcomes, and `—` marks absent telemetry. `Counted` excludes the interim duplicate. Reported `false_approval` flags, outcome coverage, per-arm configuration, timing, model IDs, run IDs and available commit metadata are in the [JSONL inventory](reasoningbank-trial-inventory-2026-11-26.jsonl).

| Source | Rows | Counted | Pass | Hidden | Timeouts |
| --- | ---: | ---: | ---: | ---: | ---: |
| `scripts/local_context_trial/results.jsonl` | 30 | 30 | 11/30 | — | 0 |
| `scripts/local_context_trial/heldout_results.jsonl` | 84 | 84 | 62/84 | — | 0 |
| `scripts/local_context_trial/heldout_multilang_v1.jsonl` | 35 | 35 | 18/35 | — | 0 |
| `scripts/local_context_trial/heldout_qwen36_35b_v1.jsonl` | 35 | 35 | 20/35 | — | 1 |
| `scripts/local_context_trial/issue7_qwen35b_paired.jsonl` | 20 | 20 | 15/20 | — | 0 |
| `scripts/local_context_trial/issue7_numbered_verdicts.jsonl` | 2 | 2 | 0/2 | — | 0 |
| `scripts/local_context_trial/qwen36_35b_short_v2.jsonl` | 10 | 10 | 5/10 | — | 0 |
| `scripts/local_context_trial/qwen36_35b_full_130.jsonl` | 130 | 130 | 103/130 | — | 3 |
| `/tmp/issue5-requests-8bb0f00-nov24.jsonl` | 1 | 1 | 1/1 | — | 0 |
| `$TMPDIR/aider-issue7-pilots-mkhykypb/issue7_results.jsonl` | 4 | 4 | 2/4 | — | 0 |
| `$TMPDIR/aider-issue7-pilots-mkhykypb/issue7_paired.jsonl` | 8 | 8 | 0/8 | — | 0 |
| `issue5-qwen36-35b-run/issue5.jsonl` | 60 | 60 | 36/60 | 36/60 | 6 |
| `issue5-qwen36-35b-run/requests.jsonl` | 12 | 12 | 8/12 | 8/12 | 0 |
| `issue5-qwen36-35b-cymbal-run/large_file.jsonl` | 6 | 6 | 3/6 | 3/6 | 0 |
| `issue5-qwen36-35b-cymbal-run/requests_cymbal.jsonl` | 12 | 12 | 6/12 | 6/12 | 0 |
| `issue5-qwen36-35b-cymbal-run/large_file_after_fix.jsonl` | 6 | 6 | 3/6 | 3/6 | 0 |
| `issue5-qwen36-35b-cymbal-run/fast_post_fix.jsonl` | 10 | 10 | 9/10 | 9/10 | 0 |
| `scripts/local_context_trial/c2_qwen36_stage1_interim.jsonl` | 42 | 0 | 19/42 | 19/42 | 2 |
| `issue5-c2-replay-run/qwen36_stage1.jsonl` | 150 | 150 | 62/150 | 62/150 | 13 |
| `issue5-c2-replay-run/qwen36_stage2_cymbal.jsonl` | 30 | 30 | 12/30 | 12/30 | 3 |
| `issue5-c2-replay-run/qwen36_stage2_readrange.jsonl` | 30 | 30 | 17/30 | 17/30 | 5 |
| `issue5-c2-replay-run/qwen36_stage2_facts.jsonl` | 30 | 30 | 19/30 | 19/30 | 6 |

The inventory hashes source bytes and counts explicit Boolean metrics with their recorded denominators. It omits raw patches, prompts, and machine-specific work directories. The original files with `source_availability` set to `local` or `temporary` were available when the inventory was generated. Git contains their aggregate records. There is no pooled pass rate across different tasks, revisions and model configurations. The Oracle references for keeping comparisons tied to the evaluated collection and preserving run artifacts are *Introduction to Information Retrieval*, PDF p. 188, and the BEIR guide, lines 123–175.

## Evidence gates for reuse

The large-file context spike has a diagnosed mechanism and a measured before/after pilot. Caller contracts, Cymbal, range context and fact retrieval remain conditional candidates whose effects need fresh paired checks. Preserve failures and false approvals as evidence about the checking process. A ReasoningBank repair can qualify for reuse after it links the observed failure, an independently checked patch, a pinned revision, and the conditions that would invalidate it.

The [trial runbook](issue-5-p0-trials.md) provides a 10-attempt pilot and a 240-attempt historical calibration with checkpoints. The fresh-corpus confirmation still requires a disjoint frozen corpus, repeated paired trials and A/A controls. The inventory contains no inference from the new resumable profiles.
