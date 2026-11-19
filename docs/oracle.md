# Oracle documentation context

Oracle is an opt-in, read-only context provider for this fork. It runs as a
separate HTTP service. Aider continues to own model calls, repo maps, edit
parsing, linting, tests and Git. Nothing changes when `--oracle` is absent.

## Which instructions reach the model

`aider/oracle_prompts.py` contains the versioned operating policy that the
module supplies to the coding model. This Markdown guide describes that policy;
it is not automatically loaded as a model instruction or ingested into Oracle.
The trial's `--message-file` explicitly supplies its task prompt. A Markdown
file needed for another task can be added explicitly with Aider's `--read`
option; reference documents remain read-only.

The adaptive action policy is appended to Aider's system instructions after
successful foundation initialization. Models without a system role receive it
through Aider's existing instruction fallback. Retrieved passages and the source
catalog stay in read-only reference messages; they are never promoted into the
system instructions. The model receives these alongside the project context. It then
produces either an ordinary coding response or an exact focused-search request.
The adapter validates the latter before any edit processing.

## When and what gets searched

The default `adaptive` mode combines mandatory foundations with focused refreshes:

1. On the first normal user turn, read the indexed source catalog and retrieve
   one passage each from the configured foundations. Defaults are
   `clean-architecture` and `think-python`, which are present in the verified
   catalog. The adapter performs this lookup regardless of model confidence;
   this does not verify that the model correctly reads or applies the evidence.
2. Keep that bounded foundation and catalog in Aider's read-only message chunk,
   outside summarizable conversation history. Ordinary turns and coder clones
   reuse it without repeating HTTP queries or duplicating it in chat history.
   It still occupies model context. Start a new Aider session to reload these
   references after changing the Oracle index or available source catalog.
3. Aider supplies the project code normally. Before proposing a change that
   depends on a dependency, API or standard's documented behavior, the policy
   instructs the model to refresh the relevant contract even when it feels
   confident. Applicable evidence already retrieved for the same question and
   version can be reused. Unresolved architecture decisions and debugging
   hypotheses may also need external evidence. Mechanical edits supported by
   the existing context need no additional search.
4. The model can emit one exact `<oracle_search>` envelope containing a JSON
   `query` and catalog `source`. Aider validates it, queries Oracle and supplies
   the cited evidence in a continuation before applying any edits. This is a
   text protocol handled by the module, not a shell command or a LiteLLM plugin.
   There is no separate planning-model call on every turn.

The policy asks the model to connect relevant principles to actual code and
report evidence gaps. By default this is a model-followed action protocol; an
ordinary response can still omit a lookup. For tasks that require fresh evidence,
enable `--oracle-require-search` as described below. Neither mode verifies that
the model chose a relevant passage or applied it correctly. Tests and review
remain necessary.

### Require retrieval before accepting a response

Use `--oracle --oracle-require-search` for an adaptive session where each normal
user turn must include a successful focused lookup. This uses the existing
response interception point before Aider parses edits or dispatches commands;
it is not a Git hook or a user-supplied shell hook.

Required foundations alone do not satisfy this condition. If the model skips
the lookup, the module holds its response and sends one corrective continuation.
A second skipped lookup stops the turn without applying those responses.
Empty results and failed lookups do not satisfy the condition. The existing
search and reflection limits still bound the interaction. The condition resets
for the next user turn, while internal reflections share its state.

While a lookup is pending, Oracle replaces the normal edit reminder with the
search protocol through a generic hook method. This uses Aider's configured
reminder channel and token checks, so ordinary edit instructions no longer
follow the search correction. Once focused evidence arrives, the normal edit
reminder returns. A rejected response explicitly reports that none of its
proposed edits or commands were applied; the model must use the actual supplied
file contents rather than assume its previous patch was accepted.

This setting defaults to false and requires adaptive mode with fail-open
disabled. Use it explicitly for evidence-dependent work; ordinary adaptive mode
still permits mechanical edits without another lookup. A successful lookup
proves that reference material was returned, not that it supports the patch.
The guard does not establish task completion, citation accuracy or test quality.

The placement follows the existing module boundaries: `OracleSession` owns the
retrieval precondition and `OracleResponseHook` translates its result. Aider only
dispatches generic response hooks before applying edits. Oracle's *Think Python*, PDF p. 58, passage
`37125e7b0eee4eebee434db3`, defines interface preconditions and postconditions;
p. 81, `0284bd306f4d5bc7d5237220`, describes guardian checks. The *Clean
Architecture* blog, passage `a892b437bacbc32f4e64e3db` (no page/line locator),
separates use-case orchestration from external-service adapters. *Cosmic Python*,
"Our First Use Case: Flask API and Service Layer", passage
`354853d1e08d13856ad0eb16` (no page/line locator), supports testing orchestration
in memory. These principles inform the implementation; they do not establish
that retrieval improves a model's reasoning.

### Generic response hooks

The scripting API accepts `Coder.create(..., response_hooks=(callback,))`.
A callback receives `content` and keyword-only `token_count` and `io` arguments.
Return `None` to let the next hook run, or
`aider.hooks.ResponseHookResult(continuation="...")` to hold the response and
ask the model to continue. `ResponseHookResult()` stops the turn. The first
interception ends dispatch for that response; ordinary reflection limits bound
continuations. Held responses do not reach edit or command processing.

A callable hook may optionally implement `prompt_reminder()`. Return literal
text to replace the edit reminder, or `None` to keep the default. The first
override wins; literal JSON braces are preserved. This method supplies
instructions only: it does not retrieve evidence or satisfy a response guard.

Oracle registers its callback only for an enabled adaptive provider. Aider
without Oracle has no Oracle callback; other callers may register their own
callbacks independently. Coder clones preserve callbacks and Oracle's shared
session state. This is an in-process extension point, not arbitrary shell-hook
execution. The Oracle lookup requirement, error handling and corrective prompt
all live in the Oracle module.

### Generic test-result hooks

`Coder.create(..., test_result_hooks=(callback,))` lets an optional module observe
each `/test` or auto-test run, passing or failing. A callback is called as
`callback(observation, io=io)` with an immutable `aider.hooks.TestRunObservation`
(`command`, `cwd`, `exit_status`, `output`, `truncated`). `output` keeps the last
20,000 characters. Return `None` for no change, or
`ResponseHookResult(continuation="...")` to send that text as the next reflected
message, even when tests passed. The first continuation wins and counts against
`max_reflections`; no new loop exists. Hooks never change what `/test` prints or
returns, and without hooks behavior is unchanged. A continuation is applied by the
auto-test flow only; `/test` just notifies hooks. If the continuation turn edits
files, auto-test runs again; core does not retain earlier green results. Hooks do
not enable tests: `--auto-test`/`--test-cmd` still decide that.

### Generic edit hooks and TDD workflow

`Coder.create(..., edit_hooks=(callback,))` runs before file creation, dirty commits,
file writes, auto-commits or suggested shell commands. Each callback receives an
immutable `aider.hooks.EditProposal(paths, shell_commands)` with normalized,
project-relative paths and returns `None` or `ResponseHookResult`. A veto drops
the response's edits and shell commands and reports that nothing was applied.

`aider/tdd.py` composes `response_hooks`, `edit_hooks` and `test_result_hooks`
into an optional test-first controller (`TddController`), enabled with `--tdd`
(or `tdd: true` in `.aider.conf.yml`; override with `--no-tdd` or
`AIDER_TDD=false`). Required options:

- `--tdd` / `--no-tdd`: enable or disable the TDD workflow (default: false).
- `--tdd-test-glob`: repeatable fnmatch glob for test files (required with `--tdd`).
- `--tdd-source-glob`: repeatable fnmatch glob for production files (required with `--tdd`).
- `--tdd-doc-glob`: optional repeatable fnmatch glob for documentation files in scope.
- `--auto-test` and a pytest `--test-cmd` (not compatible with `--edit-format architect`).

Observable phases (`TddState.phase`):

1. `test_only_edit`: only paths matching `--tdd-test-glob` may be edited. Production/doc
   edits, combined test+production patches, out-of-scope or ambiguous paths, and shell
   commands before red are vetoed before any write.
2. `red_observed`: reached only after an intended pytest red run (exit status 1, no
   collection/setup errors, assertion-like `FAILED <node_id> - <reason>` lines in edited
   test files). Passing runs, exit codes `>=2`, or failures outside edited tests are
   rejected as invalid red.
3. `implement`: production/doc edits after `red_observed` trigger the configured pytest
   command. Further test edits invalidate red/green and start a new cycle.
4. `report` and `refactor_recheck`: a passing pytest run after `implement` moves to
   `report`. The continuation supplies the actual before (red) and after (green)
   commands, exit statuses, failed test node IDs and reasons, and a visible completion
   checklist reminder that regression coverage, documentation and the final report are
   not satisfied by an old suite passing. Further production/doc edits move to
   `refactor_recheck` and invalidate green until pytest passes again.
5. `done` / `aborted`: a non-edit report response with all requirements met moves to
   `done`. Early completion claims, stale green results, untouched `--tdd-doc-glob`
   files, or fabricated passage citations report explicit unmet requirements. Exhausted
   cycle, correction or refactor budgets (or a non-pytest runner) move to `aborted` with
   an explicit `StopReason`.

### Citation grounding and completion limits

`OracleSession` records each returned foundation and focused passage in
`session.evidence` (`PassageEvidence` with `id`, `title`, `source`, `source_uri`,
`citation` and locators). `OracleSession.unknown_cited_ids(text)` detects 24-hex
passage IDs cited in a model response that were never returned in the current
session. Both `OracleSession.respond` and `TddController` (via an injected
`citation_validator` callback composed in `aider/main.py`) reject responses that
cite unknown or fabricated passage IDs.

What is **not** enforced by code:

- Valid passage identity proves that a passage ID was returned in the session; it
  does **not** prove semantic support for the model's claim.
- Touching a file matching `--tdd-doc-glob` proves the file was edited; it does
  **not** prove that the documentation claims are accurate.
- Observing a red-then-green assertion proves test execution sequence; it does
  **not** guarantee that the test is meaningful or the implementation is bug-free.

### Concrete search decisions

These examples express the intended policy, not guaranteed model decisions:

| Task or knowledge gap | Expected action |
| --- | --- |
| Rename a local variable with no semantic change | Use the project code and existing foundations; make no additional lookup. |
| Decide where to isolate an external service | Apply relevant foundation evidence; refresh a displayed Cosmic Python source if the boundary question remains unresolved. |
| Determine exactly what an HTTPX read timeout limits | Request a focused `httpx-timeouts` lookup, if that filter is in the supplied catalog. |
| Returned text describes phase timeouts but not a total deadline | State the supported scope and the missing guarantee; do not invent an overall deadline. |
| No relevant passage is returned | Identify the evidence gap, then refine within the remaining limit or continue with explicitly limited claims. |

For the timeout example, the complete model response requesting a lookup is:

```xml
<oracle_search>{"query":"What does an HTTPX read timeout limit?","source":"httpx-timeouts"}</oracle_search>
```

The envelope contains exactly `query` and `source`; the source must be one of
the displayed filters. A request cannot include edits, shell commands or extra
JSON fields. After the evidence is supplied, continue the original task and cite
the actual passage ID, origin and available page/line locator for supported
claims. General book advice must be connected to the relevant code decision.

`--oracle-mode always` retains deterministic retrieval of each preprocessed
user message. Commands and internal editor messages do not trigger a new
initial retrieval; architect/editor clones still receive pinned foundations.

The routing instructions live in `aider/oracle_prompts.py`, version
`foundation-refresh-v5`. Oracle's retrieved *Google Prompt Engineering*, PDF pp. 18–19, passages
`8421356d1139085ef9e033ec` and `772852df727c4d9b3f1c5c00`, supports providing
clear purpose, task context and output instructions; it does not establish
the correctness of these particular routing decisions. Its p. 65, passage
`90595d98465d965c36f7c6b0`, supports testing prompt changes and tracking RAG
settings. Tests verify mandatory initialization, request validation and
continuation behavior; a real-model trial is needed to assess query quality.
Its p. 37, passage `a6d4e6e89737d00ed31c0eac`, describes the tool
action/observation loop; p. 56, passage `bac1d6367a4987d87c1f7f18`, supports
specific output instructions. These informed v3's pre-edit search example and
normal-edit continuation. They do not establish that v3 improves this model's
behavior; compare fresh trials with the recorded v2 checkpoints.
Version v5 applies the same action/observation principle to explicit rejection
feedback and a pending-search reminder. Tests verify outgoing message order;
they do not establish that a live model will follow the corrected instructions.

## Flags and configuration

```sh
aider --oracle --oracle-source cosmic-python --oracle-top-k 3 \
  --oracle-max-context-tokens 4096 --message "Explain the service boundary" app.py
```

The same settings work in `.aider.conf.yml` through Aider's existing parser:

```yaml
oracle: true
oracle-base-url: http://127.0.0.1:8742
oracle-token-env: ORACLE_TOKEN
oracle-top-k: 3
oracle-timeout-seconds: 10
oracle-max-context-tokens: 4096
oracle-mode: adaptive
oracle-foundation-source:
  - clean-architecture
  - think-python
oracle-max-searches: 2
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
| `oracle-top-k` | 3 | 1–20 passages per focused refresh; a shared total across filters in always mode; initialization separately retrieves one passage per required foundation |
| `oracle-timeout-seconds` | 10 | Positive, finite HTTPX inactivity timeout per request |
| `oracle-max-context-tokens` | 4096 | Positive reference budget, including the policy, foundation, catalog and reminder |
| `oracle-source` | none | Repeatable path substring filters, at most eight |
| `oracle-fail-open` | false | On failure, cancel the turn; true warns and continues without the failed lookup |
| `oracle-mode` | adaptive | Required foundations plus focused requests; `always` retrieves each user message |
| `oracle-require-search` | false | Require successful nonempty focused retrieval each user turn; adaptive mode only, incompatible with fail-open |
| `oracle-foundation-source` | clean-architecture, think-python | 1–4 required initial source filters, independently configured from refresh filters |
| `oracle-max-searches` | 2 | 1–3 focused refreshes per user turn, also bounded by Aider's existing reflection limit |

Source filters use Oracle's **source path substring** semantics, not an exact
catalog identity. In adaptive mode they restrict the refresh catalog; required
foundations have their own independent filters. The catalog uses compact managed
source-directory filters when available, otherwise actual source paths, and is
trimmed to the reference budget. Missing required books or an insufficient
foundation budget fail explicitly. A refresh chooses one displayed filter and
at most `top_k` passages. In always mode filters are queried in configured order
and share a total candidate limit. Duplicate IDs are included once per retrieval.

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
   path. To test an outage, stop Oracle and start a fresh Aider process so the
   required foundations are not already cached. The default should cancel its
   first model turn; repeat with `--oracle-fail-open` to check the warning and
   continuation without new evidence. A warm session can continue from cached
   foundations until it requests a refresh that needs the service.

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
    io=InputOutput(),
    auto_commits=False,
    oracle_config=OracleConfig(enabled=True, source_ids=("cosmic-python",)),
)
adapter.run("Improve the interface using relevant retrieved guidance")
```

`InputOutput()` retains normal confirmations. Unattended scripts can explicitly
choose `InputOutput(yes=True)` if automatic confirmation is intended.

For direct use, pass `create_oracle_provider(config)` as `oracle_provider` to
`Coder.create`. `OracleContextProvider` itself is the low-level deterministic HTTP
adapter. Tests can inject another `ContextProvider` implementation or an
HTTPX transport. The adapter delegates to Aider's ordinary `run` path; retrieval
occurs after command, URL and file-mention preprocessing and before sending the
first model message. Lint/error reflections reuse existing evidence; validated
search envelopes intentionally cause a bounded retrieval continuation.
Internal `run(..., preproc=False)` calls, including architect-to-editor handoffs,
bypass initial context preparation; an initialized shared provider still supplies
its pinned foundations and can handle focused requests within the shared search
budget. `run_stream` prepares context before sending, then handles focused-search
continuations without running the evidence through user-command preprocessing.
`clone` retains the provider through the existing `original_kwargs` mechanism.

## Verified service boundary and failure behavior

The live `/openapi.json` was inspected on 2026-11-18. Adaptive initialization reads
`GET /sources` once. This implementation uses
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
metadata, source-filter violations, invalid model requests and excessive
responses raise distinct `OracleError` subclasses. The native path displays the error and cancels that
turn unless fail-open is enabled. Missing required foundation evidence cancels
initialization by default; fail-open explicitly permits continuing without it.
Empty focused results return an evidence-gap message to the model. Empty results
in always mode warn and continue. If no whole
passage fits, a budget error follows the configured failure policy. The budget
includes the evidence envelope and metadata, excluding the original user
instruction. Initialization additionally counts its policy, catalog and reminder.
Reference counts use Aider's `Model.token_count`; estimates can differ from the
server tokenizer. Each focused refresh has its own reference budget; chat history
remains governed by Aider's context settings. Oversized individual passages are omitted, never silently
cut mid-citation. A one MiB decompressed response limit applies to each request.
HTTP redirects and environment-derived HTTP proxies are disabled for retrieval.
Timeouts are inactivity limits, not an overall wall-clock deadline; multiple
source queries each have their own timeout.

Retrieved text and metadata are XML-escaped and labeled as untrusted evidence.
The module does not execute commands, follow source URLs, read cited file paths,
or automatically add repository contents to retrieval requests. Adaptive mode
sends fixed foundation questions and the model's focused queries, which may
include project identifiers. Always mode sends the current preprocessed user
instruction; context explicitly included by the user or Aider's existing URL
preprocessing can therefore be sent to Oracle.
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

Repository automation has two separate workflows:

- `sync-upstream.yml` runs weekly or manually. It checks `Aider-AI/aider`, prepares
  a review branch in this fork when changes are available, and opens or updates
  a pull request here. It does not run integration tests or push changes back
  to the upstream project.
- `oracle-compatibility.yml` runs the Oracle integration and scripting API tests
  on relevant pull requests or a manual run, using Python 3.10 and 3.12. It has
  read-only repository permissions and does not merge or push changes. The sync
  workflow preserves this file when removing unrelated upstream workflows.

For sync PRs opened with `GITHUB_TOKEN`, GitHub can require a maintainer to
select **Approve workflows to run** before PR checks start. A manual test run
can also select the sync branch. See
[GitHub's workflow-trigger rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow).
Review the compatibility results before accepting an upstream merge.
Real Oracle/Qwen smoke steps above remain available and require separately
running services. The local
live-service smoke and its limitations are recorded in
[the verification notes](oracle-verification.md).

## Markdown instruction review

This guide, [the trial instructions](oracle-trial.md),
[the trial prompt](oracle-trial-prompt.txt), and
[the verification record](oracle-verification.md) serve different purposes:
operating contract, coding task, and observed evidence. Only the runtime policy
and explicitly supplied task files reach the model automatically through this
integration. Keep these documents aligned with the implementation rather than
treating documentation text as an independently executed policy.

The Markdown review used Oracle's *Google Prompt Engineering*, PDF p. 55,
passage `1718bc208ed1472ae14c4086`, for clear, concise instructions, and p. 56,
passage `bac1d6367a4987d87c1f7f18`, for specific expected output. Applied changes
include the explicit input/output contract, concrete search cases, the complete
request example, and cache-aware outage steps. This is an application of those
principles; it does not establish that the model will follow every instruction.

Retrieval query, with `--source prompt-engineering --top-k 3`:
“Design with simplicity. Be specific about desired output. Use clear concise
prompts and provide examples of expected behavior.” The table-of-contents result
was not used as supporting evidence.

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
Required foundations and the explicit search-envelope continuation were added
following the user's hybrid policy; they extend the original wrapper-only MVP
without adding a separate agent or a per-turn planning request.
Transport exception details are deliberately suppressed in favor of safe typed
failure categories, so server diagnostics and credential-bearing exception text
cannot leak through the native error display.
