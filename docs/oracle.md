# Oracle documentation context

Oracle is an opt-in, read-only context provider for this fork. It runs as a
separate HTTP service. Aider continues to own model calls, repo maps, edit
parsing, linting, tests and Git. Nothing changes when `--oracle` is absent.

## When and what gets searched

This implementation uses deterministic retrieval: enabling `--oracle` queries
each normal user turn once, using its preprocessed message as the query. The
coding model does not discover Oracle, select sources, or formulate searches.
Source filters are selected by the user. Commands and internal editor messages
do not trigger searches.

Model-directed retrieval would need an additional planning or tool interface,
with the source catalog and explicit routing instructions. A candidate policy
would search for unresolved architecture, API, language-specification and
documented debugging questions, and skip mechanical edits or tasks already
supported by supplied evidence. It would turn a broad task into focused
technical queries. That policy is a proposal, not behavior implemented here.
Oracle's retrieved *Google Prompt Engineering*, PDF pp. 18–19, passages
`8421356d1139085ef9e033ec` and `772852df727c4d9b3f1c5c00`, supports providing
clear purpose, task context and output instructions; it does not establish
the correctness of these particular routing decisions. They require tests.

## Flags and configuration

```sh
aider --oracle --oracle-source cosmic-python --oracle-top-k 3 \
  --oracle-max-context-tokens 2048 --message "Explain the service boundary" app.py
```

The same settings work in `.aider.conf.yml` through Aider's existing parser:

```yaml
oracle: true
oracle-base-url: http://127.0.0.1:8742
oracle-token-env: ORACLE_TOKEN
oracle-top-k: 3
oracle-timeout-seconds: 10
oracle-max-context-tokens: 2048
oracle-source:
  - clean-architecture
  - cosmic-python
oracle-fail-open: false
```

Use `--no-oracle` to override enabled YAML configuration. Environment settings
use the existing `AIDER_` prefix, for example `AIDER_ORACLE=true`. The bearer
token itself belongs only in the environment named by `oracle-token-env`;
never put it in the URL or YAML. Load it using your normal secret management.
No raw-token flag is provided.

| Setting | Default | Contract |
| --- | --- | --- |
| `oracle` | false | Explicitly enables retrieval |
| `oracle-base-url` | `http://127.0.0.1:8742` | HTTP(S) URL without credentials, query or fragment |
| `oracle-token-env` | `ORACLE_TOKEN` | Environment variable name, independent of model API keys |
| `oracle-top-k` | 3 | 1–20 complete candidate passages in total |
| `oracle-timeout-seconds` | 10 | Positive, finite HTTPX inactivity timeout per request |
| `oracle-max-context-tokens` | 2048 | Positive budget, measured with the coding model's tokenizer |
| `oracle-source` | none | Repeatable path substring filters, at most eight |
| `oracle-fail-open` | false | On failure, cancel the turn; true warns and uses the original instruction |

Source filters use Oracle's **source path substring** semantics, not an exact
catalog identity. They are queried in configured order and share the total
candidate limit. Earlier sources can consume the entire limit. Duplicate IDs
are included once. There is no cross-source reranking or session cache.

## Local Qwen and Oracle

1. Run Oracle separately with its own Python 3.14+ environment and configured
   embedding provider. For an existing index, run `oracle serve start`, then
   `oracle sources` and a focused `oracle query` to verify retrieval. Consult
   the [Oracle deployment guide](https://github.com/pavlosuperhero/oracle-rag#configure-and-run)
   for initial installation and explicit document ingestion.
2. In LM Studio, load your chosen Qwen coding model and start its local API
   server. Obtain the actual model identifier from that server. Set
   `LM_STUDIO_API_BASE=http://localhost:1234/v1` and
   `LM_STUDIO_API_KEY=dummy-api-key` for a server without authentication.
3. In a disposable project, run:

   ```sh
   aider --model lm_studio/YOUR_QWEN_MODEL_ID --oracle \
     --oracle-source cosmic-python --no-auto-commits greeting.py \
     --message "Use the retrieved interface-design guidance to improve this file"
   ```

   Check the diff and citations. Repeat with `--no-oracle` to confirm the normal
   path. Stop Oracle and verify the default cancels the turn; then try
   `--oracle-fail-open` to see the warning and ordinary Aider behavior.

The [Aider LM Studio guide](https://aider.chat/docs/llms/lm-studio.html) documents
that model prefix and environment setup. A vLLM server can instead use the
[OpenAI-compatible configuration](https://aider.chat/docs/llms/openai-compat.html),
with `OPENAI_API_BASE`, `OPENAI_API_KEY`, and `--model openai/MODEL_ID`.
Existing Bedrock coding-model configuration also remains independent. Oracle's
embedding model and dimensions belong to Oracle's configuration; this module
does not select or alter them. Ingestion is never automatic.

## Python scripting

```python
from aider.io import InputOutput
from aider.models import Model
from aider.oracle import AiderOracleAdapter, OracleConfig

adapter = AiderOracleAdapter.create(
    main_model=Model("lm_studio/YOUR_QWEN_MODEL_ID"),
    fnames=["greeting.py"],
    io=InputOutput(yes=True),
    auto_commits=False,
    oracle_config=OracleConfig(enabled=True, source_ids=("cosmic-python",)),
)
adapter.run("Improve the interface using relevant retrieved guidance")
```

For direct use, pass an `OracleContextProvider` as `oracle_provider` to
`Coder.create`. Tests can inject another `ContextProvider` implementation or an
HTTPX transport. The adapter delegates to Aider's ordinary `run` path; retrieval
occurs after command, URL and file-mention preprocessing and before sending the
first model message. Reflections reuse that evidence rather than querying again.
Internal `run(..., preproc=False)` calls, including architect-to-editor handoffs,
bypass retrieval; `run_stream` retrieves once before sending its message.
`clone` retains the provider through the existing `original_kwargs` mechanism.

## Verified service boundary and failure behavior

The live `/openapi.json` was inspected on 2026-11-18. This implementation uses
`POST /query` with `{query, top_k, source?}`. Queries contain 1–4000 characters;
the optional source contains 1–1000. The response is an object with a `results`
list of complete passages containing `id`, `text`, `title`, `source`,
`source_uri`, `citation`, and nullable `page`, `line_start`, `line_end` fields.
It does not infer a schema from an endpoint name.

HTTP `/query` already returns complete passage text and provenance, so a second
fetch is unnecessary. The alternate MCP `search` returns passage identities,
titles and source URLs; MCP `fetch` or HTTP `/documents/{id}` retrieves one
indexed passage without re-embedding. See Oracle's
[citation and identity contract](https://github.com/pavlosuperhero/oracle-rag/blob/main/docs/codex-sources.md).
MCP transport is not implemented in this MVP.

401/403, timeouts, network/index unavailability, malformed JSON, missing passage
metadata, source-filter violations and excessive responses raise distinct
`OracleError` subclasses. The native path displays the error and cancels that
turn unless fail-open is enabled. Empty results warn and continue. If no whole
passage fits, a budget error follows the configured failure policy. The budget
includes the complete evidence envelope and metadata, excluding the original
user instruction. Oversized individual passages are omitted, never silently
cut mid-citation. A one MiB decompressed response limit applies to each request.
HTTP redirects and environment-derived HTTP proxies are disabled for retrieval.
Timeouts are inactivity limits, not an overall wall-clock deadline; multiple
source queries each have their own timeout.

Retrieved text and metadata are XML-escaped and labeled as untrusted evidence.
The module does not execute commands, follow source URLs, read cited file paths,
or add repository contents to retrieval requests. It sends the current
preprocessed user instruction only; context explicitly included by the user or
Aider's existing URL preprocessing can therefore be sent to Oracle.
Prompt labeling is not a security sandbox for a coding model. Model-generated
edits and shell suggestions remain governed by Aider's normal confirmations and
permissions; the tests establish that the retrieval adapter itself never
dispatches passage commands, not that every model resists prompt injection.

The Aider fork retains Apache-2.0 and upstream attribution. There is no Oracle
package dependency or copied Oracle implementation. Oracle is separately
distributed under AGPL-3.0-only. This records the implementation boundary rather
than offering a legal conclusion about every deployment.

## Compatibility and verification

The baseline is Aider fork revision
`853f64b82e42e8c86f5ba088ed7d4fda8c8e7659` (`0.86.3.dev`).
The [upstream scripting documentation](https://aider.chat/docs/scripting.html)
explicitly warns that this Python API is unsupported and may change without
backwards compatibility. `tests/basic/test_oracle_integration.py` exercises real
`Coder.create`, `run`, `clone`, YAML/flags and edit parsing without a real coding
model. Run:

```sh
python -m pytest -q tests/basic/test_oracle.py tests/basic/test_oracle_integration.py \
  tests/basic/test_scripting.py
```

The existing upstream-sync workflow runs these checks against a proposed merge
before pushing its review branch, and on relevant pull requests. A failed check
is the alert for incompatible scripting API changes. Real Oracle/Qwen smoke
steps above remain optional and require separately running services.

## Design evidence retrieved through Oracle

The following references informed specific implementation choices. Passage IDs
identify the indexed evidence read during this task; where an HTML document has
no page or source-line metadata, no locator is invented.

| Reference and retrieved locator | Application |
| --- | --- |
| [Clean Architecture, “Interface Adapters” and “What data crosses the boundaries”](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html), passages `a892b437bacbc32f4e64e3db`, `62436485f5653f4d006da35c` | HTTP details stay in the provider; the coder receives plain context text. |
| [Cosmic Python, chapter 13, explicit dependencies and bootstrap recap](https://www.cosmicpython.com/book/chapter_13_dependency_injection.html), passages `6354c7447756102ba5eb852c`, `454b7f4ebe6d9e640c333cc1` | Provider and transport injection allow tests without a live retrieval service; CLI composition happens in `main`. |
| [Think Python, second edition](https://greenteapress.com/thinkpython2/thinkpython2.pdf), PDF p. 58 (printed p. 36), §4.10, passage `37125e7b0eee4eebee434db3` | Document configuration preconditions and observable postconditions, with explicit validation at the service boundary. Runtime validation is this implementation's application of that interface-contract guidance. |
| [PEP 544, specification/terminology](https://peps.python.org/pep-0544/#terminology), source lines 234–297, passage `9706f13cdbc6b21b650a76ad` | A structural `ContextProvider` protocol describes the retrieval port without requiring fake providers to inherit the HTTP implementation. |
| [PEP 8, programming recommendations](https://peps.python.org/pep-0008/#programming-recommendations), source lines 1286–1337, passage `9dd3c88d64ffd6bbbba235ec` | Exceptions derive from `Exception`, use an `Error` suffix, and distinguish failures callers need to handle. Only known retrieval failures are caught. |
| [Python API: `typing.Protocol`](https://docs.python.org/3.11/library/typing.html#typing.Protocol) | Defines the structural typing API used by that port; annotations are not runtime validation. |
| [Python tutorial/API: user-defined exceptions](https://docs.python.org/3.11/tutorial/errors.html#user-defined-exceptions), passage `f9dd56fb283a8fec79a37595` | A small `Exception` hierarchy lets the adapter handle retrieval failures without suppressing unrelated programming errors. |
| [HTTPX timeout API](https://www.python-httpx.org/advanced/timeouts/), passage `4d4d418905639aeba3a03277` | Explicit network inactivity timeouts and `TimeoutException` handling. |

Queries used (all `oracle query`, top-k 2 or 3):

- `clean-architecture`: “How should interface adapters isolate external services and pass simple data across boundaries?”
- `cosmic-python-di`: “How can dependency injection isolate an external HTTP service adapter and enable fake dependencies in tests?”
- `think-python`: “How does design by contract validate preconditions and raise exceptions for invalid input?”
- `pep-0544`: “How do Protocol classes support structural typing for injectable implementations without explicit inheritance?”
- `pep-0008`: “How should exception class names and inheritance distinguish application errors?”
- `python-exceptions`: “How should user defined exceptions derive from Exception and be organized into an exception hierarchy?”
- `httpx-timeouts`: “How should HTTP clients set explicit timeouts and handle timeout exceptions?”
- `prompt-engineering`: “How should retrieved documents be separated from instructions to mitigate prompt injection?” The retrieved passages did **not** establish an injection defense; no security guarantee is attributed to them.

Evidence review: Cosmic Python illustrates an ABC; this implementation uses a
PEP 544 protocol to retain structural compatibility with Aider's duck typing.
The user-requested native flags require a small hook in `Coder.run_one`, beyond
the issue's initial wrapper-only proposal. That hook preserves the existing
preprocessing, edit parser and Git implementation.
Transport exception details are deliberately suppressed in favor of safe typed
failure categories, so server diagnostics and credential-bearing exception text
cannot leak through the native error display.
