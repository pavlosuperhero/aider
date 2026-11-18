# Oracle feature-quality evaluation

This is the operator/evaluator guide. Supply only
[`oracle-quality-task.md`](oracle-quality-task.md) to the coding model. Do not
add this guide or its expected facts to the model's read-only context.

## What this run measures

The task is a useful, bounded change to the real Oracle module, not a request
for a demonstration answer. Evaluate five independent questions:

1. Were the required foundations supplied before the first response?
2. Did Aider select relevant knowledge after seeing the code?
3. Do its technical claims and citations agree with the retrieved passages?
4. Does the patch improve behavior, with meaningful regression tests?
5. Was the result obtained within the configured retrieval/context limits?

A search call or a green test run alone does not answer all five questions.
Do not treat the model's final report as independent proof.

## Start the Oracle-enabled trial

The prepared branch is `test/aider-oracle-quality`, based on merged main
revision `8448ce9f8af3e5bd2c5e9a7cf9767c36b42b7488`. Run from the checkout root
with no pending tracked code changes. Leave unrelated local files untouched.
The installed command is an editable installation of
this checkout, so it follows the checked-out code.

The `.aider-oracle-quality/` directory is covered by the existing ignore rules.
It holds local evaluation artifacts, including the exact model inputs; keep
these logs out of commits.

```sh
mkdir -p .aider-oracle-quality
git rev-parse HEAD > .aider-oracle-quality/base.txt
aider --oracle --oracle-mode adaptive \
  --no-auto-commits --no-dirty-commits --no-restore-chat-history --no-analytics \
  --message-file docs/tasks/oracle-quality-task.md \
  --llm-history-file .aider-oracle-quality/llm.md \
  --chat-history-file .aider-oracle-quality/chat.md \
  --input-history-file .aider-oracle-quality/input.history \
  aider/oracle.py tests/basic/test_oracle.py docs/oracle.md
```

Use a fresh process and fresh artifact files for each attempt: history logs
can append to existing files. Record the model/server version, actual loaded
context limit, sampling/output settings, prompt version, foundation/source
filters, passage/context/search budgets and timeout setting. Record elapsed
time and the usage figures Aider reports; leave unavailable metrics marked
unavailable rather than estimating them from prose. Do not copy secrets into
the evaluation record.

Model history shows which evidence was supplied and which refresh envelopes
were emitted; it does not by itself prove the number of HTTP requests Oracle
received. Mark that count unavailable without a service/request trace. Repeated
pinned evidence in model inputs is expected and is not a repeated server fetch.
Use the existing cache tests as separate mechanism evidence.

Normal confirmation prompts remain active. Review proposed shell commands.
After the model finishes, independently capture the result:

```sh
.venv/bin/python -m pytest -q \
  tests/basic/test_oracle.py tests/basic/test_oracle_integration.py \
  > .aider-oracle-quality/tests.log 2>&1
git diff --check
git diff > .aider-oracle-quality/changes.diff
git status --short > .aider-oracle-quality/status.txt
```

Retain the command exit status as well as its output. A test failure is an
evaluation result, not a reason to silently edit the patch before recording it.
Ask for evaluation with the transcript, raw model history, diff and test log.

## Evaluator reference facts

Check these against current Oracle evidence when reviewing the result:

- The HTTPX timeout hierarchy distinguishes connection establishment, reading
  data, writing data and waiting for a connection-pool slot. The task should
  preserve useful distinctions without breaking the public Oracle catch type.
- A read timeout limits the wait for a data chunk. The cited phase/inactivity
  settings do not establish a single whole-request wall-clock deadline.
- A generic timeout still needs a safe diagnostic. Raw exception text can
  contain a private URL or credential-like data and should remain suppressed.
- Error classification must not change fail-open/closed outcomes or execute
  anything from a retrieved document.

Oracle reference: *Timeouts - HTTPX*, passages
`4d4d418905639aeba3a03277` and `78faef7407ac54fc6040e9d2` (HTML has no
page/source-line locator). Current primary references are the
[HTTPX timeout documentation](https://www.python-httpx.org/advanced/timeouts/)
and [exception hierarchy](https://www.python-httpx.org/exceptions/).
These references are evaluator material, not prefilled answers for the model.

## Review rubric

Use this practical rubric to make the review consistent. It is a proposed
review scale, not a validated benchmark. Award partial credit with a reason.

| Dimension | Points | Evidence required |
| --- | ---: | --- |
| Retrieval choice | 0–4 | A relevant focused question and source when the task requires dependency facts; no unrelated lookups or fabricated search claims. Foundation delivery is checked separately as module behavior. |
| Source interpretation | 0–6 | Claims match actual returned text and scope, citations resolve to the claimed evidence, missing support is acknowledged, and foundations are applied only where relevant. |
| Implementation and tests | 0–6 | A scoped, useful diagnostic change; retained public catch behavior and redaction; meaningful before/after tests including timeout categories and fallback; independent tests pass. |
| Efficiency and control | 0–4 | Search/context limits respected, no unnecessary planning loop or repeated foundation fetch, no unrelated edits or commits, and usage figures recorded honestly. |

Report the score per dimension, the observed shortcomings and the overall
verdict. Fabricated citations, leaked secrets or command execution from
retrieved text are failures regardless of the numeric score. Passing tests
cannot compensate for an incorrect explanation of the evidence.

## Comparing with Oracle disabled

One successful run establishes a local example, not that Oracle generally
improves this model. For a comparison, repeat the identical task in a separate
fresh checkout at the recorded preparation commit, with the same model and
settings, changing only retrieval to `--no-oracle`. Use fresh conversations and
artifact directories; do not let either run read the other run's patch or logs.
Each checkout needs its own editable runtime and test environment: install with
`uv pip install -e . pytest pytest-env` into that checkout's `.venv`, then use
its `.venv/bin/aider`. Verify the imported `aider` source path. Reusing a global
editable command that points at the other run's checkout would contaminate the
comparison.

Compare common outcomes: correct code, unsupported claims, verified tests,
tokens, model calls and elapsed time. Report Oracle-specific retrieval behavior
separately; do not penalize the disabled baseline for lacking mandatory
foundations or a search tool. Repeat paired trials before drawing broader
quality conclusions. Record index/catalog changes that affect comparability.

## Oracle basis for the evaluation design

*Google Prompt Engineering* (February 2025), PDF p. 65, passage
`90595d98465d965c36f7c6b0`, recommends evaluating prompt changes and recording
the RAG settings and context that affect an answer. This motivates capturing
the actual model inputs, configuration and observed output rather than relying
on the model's self-assessment. The task, rubric and comparison protocol above
are this project's application of that guidance, not claims made by the book.

Queries used:

- `prompt-engineering`, top-k 1: “How should retrieval augmented generation
  prompts be evaluated and retrieval context recorded when changing model behavior?”
- `httpx-timeouts`, top-k 2: “What does an HTTPX read timeout measure and is it
  a total request deadline?”
