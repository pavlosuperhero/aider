# First useful Aider trial

Use the existing programmatic dry-run inconsistency as a small regression-fix
task. It was observed while testing this integration: passing `dry_run=True`
to `Coder.create` with an ordinary supplied `InputOutput` can still write an
existing file. The CLI already constructs its I/O with the dry-run flag, so
that path does not reproduce this specific mismatch. This trial is intentionally
left for Aider to solve after the integration is reviewed.

Start in the fork checkout, with the required foundations enabled by the user
configuration:

```sh
cd /Users/Pavlo_Fabuliak/Documents/Personal/aider
aider --no-auto-commits --message-file docs/oracle-trial-prompt.txt \
  aider/coders/base_coder.py aider/io.py tests/basic/test_scripting.py
```

The command supplies [the task prompt](oracle-trial-prompt.txt), which states
the observed trigger, required behavior, regression-first workflow, scope and
three-part review output. The prepared `.venv` contains the test dependencies.
On another checkout, prepare it with `uv venv --python 3.12` followed by
`uv pip install -e . pytest pytest-env`.

The task file is supplied explicitly by `--message-file`. The standing Oracle
policy is supplied by the module from `aider/oracle_prompts.py`; this guide and
`docs/oracle.md` are human reference documents, not automatically loaded prompts.

Keep the Aider conversation and the diff. When requesting evaluation, include
the transcript or its history-file path and say “evaluate the Aider trial”.
Evaluation should inspect the actual source, test results and retrieved
passages; the model's self-reported success is not sufficient.

## Acceptance and evaluation

- A test demonstrates the original failure rather than only mirroring the fix.
- Existing and new files remain untouched, and no Git commit occurs in dry-run.
- A normal run still applies the intended edit.
- The change respects caller-owned I/O settings and handles cloning coherently.
- The fix is scoped to the actual side-effect boundary; Oracle and unrelated
  behavior remain unchanged.
- Required foundations appear before the first model response. Any additional
  search has a focused, relevant question and a real returned citation.
- Reasoning applies the source to the code and acknowledges missing evidence.

Reference criteria: *Think Python*, §4.10, PDF p. 58, passage
`37125e7b0eee4eebee434db3` (interface preconditions/postconditions); *Clean
Architecture*, boundary-data passage `62436485f5653f4d006da35c`; and *Cosmic
Python*, explicit-dependency passage `6354c7447756102ba5eb852c`. See
[the source links and retrieval record](oracle.md#design-evidence-retrieved-through-oracle).
These are evaluation principles, not a prescribed implementation.

## Prompt-engineering evidence from Oracle

The task prompt follows *Google Prompt Engineering* (February 2025):

- PDF p. 55, “Design with simplicity”, passage `1718bc208ed1472ae14c4086`:
  concise instructions, familiar language and explicit action verbs. Applied
  as a small task with four concrete work steps.
- PDF p. 56, “Be specific about the output”, passage `bac1d6367a4987d87c1f7f18`:
  specify the desired result. Applied as three review items, exact test commands
  and observable acceptance cases.
- PDF p. 57, instructions over excessive constraints, passage
  `a85c0612f27a0d4af6804b6d`: state what to produce and retain necessary scope
  boundaries. Applied as positive behavior requirements and a short scope rule.
- PDF p. 65, passage `90595d98465d965c36f7c6b0`: evaluate prompt changes and
  record relevant RAG settings. Applied as before/after test evidence and
  citations to the passages actually supplied.

Queries used, with `--source prompt-engineering --top-k 3`:

- “How should coding task prompts specify objectives, inputs, constraints,
  examples and required output to reduce ambiguity?”
- “Design with simplicity. Be specific about desired output. Use clear concise
  prompts and provide examples of expected behavior.”

The table-of-contents hit was excluded from the supporting evidence. These
references inform the prompt; they do not guarantee that the model will follow
every instruction or produce a correct fix.
