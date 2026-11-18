# Oracle integration verification

Local verification date: 2026-11-18.

## Environment and checks

- The user command is the editable installation of this fork. The previous
  Homebrew formula was removed.
- LM Studio reported `qwen3-coder-30b-a3b-instruct-mlx` loaded, with both
  `max_context_length` and `loaded_context_length` equal to 262144.
- The user model metadata records a 262144-token input context and an
  8192-token output budget. The server remains responsible for its actual
  combined context limit; metadata does not resize the server.
- Live Oracle initialization loaded both required foundations and supplied
  111 compact catalog filters within the configured 4096-token reference budget.
  Actual contents and counts depend on the index and token counter.
- Unit and integration checks exercise API failures, budgets, citations,
  source limits, foundation caching, history compaction, clone behavior,
  normal/streaming refresh continuations, and rejection of mixed search/edit
  messages. Existing coder, main and scripting tests were also run.
- Formatting, static checks and `git diff --check` passed.
- The final local test-environment run passed **197 tests and 29 subtests**.

The automated run uses:

```sh
.venv/bin/python -m pytest -q \
  tests/basic/test_oracle.py tests/basic/test_oracle_integration.py \
  tests/basic/test_scripting.py tests/basic/test_coder.py tests/basic/test_main.py
```

## Live Qwen/Oracle smoke

A disposable directory contained a small public example using `httpx.get`.
Aider ran with dry-run, Git disabled, shell suggestions disabled and no automatic
linting. The same question asked for a focused `httpx-timeouts` refresh about
read timeout versus an overall deadline, a two-sentence explanation, and a
passage citation. No fixture file changed.

Both prompt versions performed the required initial grounding, emitted a valid
search request, received Oracle evidence, and returned an answer with passage
IDs. There was no separate planning-model call.

| Prompt version | Observed interpretation |
| --- | --- |
| `foundation-refresh-v1` | The model incorrectly described `httpx.Timeout` as a whole-operation deadline, although the passage describes separate inactivity/phase timeouts. It also forced an architectural justification into a documentation question. |
| `foundation-refresh-v2` | With explicit scope/conditions and evidence-gap instructions, the model stated that the retrieved text did not establish a single overall deadline, cited the returned IDs and origins, and explained why the architectural source was not directly applicable. |

The returned HTTPX passages were `4d4d418905639aeba3a03277` and
`78faef7407ac54fc6040e9d2`, from the indexed *Timeouts - HTTPX* document;
the [official timeout API documentation](https://www.python-httpx.org/advanced/timeouts/)
also distinguishes connect, read, write and pool limits.

The revised answer still exceeded the requested two sentences. This was one
before/after example, not a general accuracy benchmark. It demonstrates working
retrieval and an observed correction, not guaranteed source interpretation or
format compliance. The [dry-run trial](oracle-trial.md) is a separate useful
coding task for evaluating actual edits, tests and source application.

The prompt revision follows Oracle's *Google Prompt Engineering*, PDF p. 65,
passage `90595d98465d965c36f7c6b0`: test changes against examples and record
relevant RAG settings. The runtime policy is kept separately in
`aider/oracle_prompts.py`; it does not persist private queries or retrieved
passages as a new cache on disk.

## Documentation review

The Markdown instructions were also checked against the runtime policy and
configuration. The review clarified which files actually reach the model,
added concrete search/response examples, distinguished mandatory initialization
from refresh limits, and corrected the outage procedure for cached sessions.
The runtime policy was unchanged in this documentation review, so the live
model observations above still refer to the same tested prompt versions.
