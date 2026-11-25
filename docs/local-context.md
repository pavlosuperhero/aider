# Local context: callers, ReadRange, and project facts

Local context adds three opt-in, read-only context primitives to this Aider fork:

1. **Local caller discovery** (`--local-callers`): attaches bounded excerpts from
   callers of symbols named in your task, using either Aider's built-in AST and
   Tree-sitter scanners or an optional external **Cymbal** index.
2. **Bounded ReadRange** (`--local-callers-read-range`, `/read-range`): streams
   numbered line slices from workspace files instead of loading entire large
   files into context.
3. **Project facts** (`--facts`, `/fact`): stores worktree-scoped conventions
   and design constraints with source-file SHA-256 provenance so stale notes are
   flagged automatically when code changes.

All flags default to off. When none of these flags are enabled, Aider behaves
identically to stock Aider: it does not spawn external indexers, probe SQLite,
or create `.aider.facts.jsonl` or database files. Cymbal is an optional external
CLI and is never a hard runtime dependency. Every caller excerpt, line range,
and project fact is attached as read-only reference context; Aider retains
ownership of edits, confirmations, linting, tests, and Git operations.

This single-document manual is structured so you can search across setup, flags,
commands, examples, and limitations in one place (following *Producing Open
Source Software*, pp. 29–30, Oracle passages `310788ee09907669cec118e7` and
`909825698bf50b016029028c`).

## Optional Cymbal setup (v0.18.0)

By default, `--local-callers` runs completely in-process with `--local-callers-backend builtin`.
Install Cymbal only if you want an incremental Tree-sitter + SQLite symbol index
for faster cross-file caller discovery, transitive impact analysis, and
large-file call-site lookup.

### Supported version and installation

Aider pins and validates **Cymbal `v0.18.0`** (`PINNED_CYMBAL_VERSION = "0.18.0"`
in `aider/cymbal_callers.py`). Before issuing JSON queries, `CymbalClient` runs
`cymbal --version` and rejects version mismatches with `CymbalVersionError`.

Install via Homebrew (macOS or Linux):

```sh
brew install cymbal
```

Or build the pinned `v0.18.0` binary from `github.com/1broseidon/cymbal` with Go
(CGO and SQLite FTS5 are required):

```sh
CGO_ENABLED=1 CGO_CFLAGS="-DSQLITE_ENABLE_FTS5" \
  go install -ldflags "-X github.com/1broseidon/cymbal/cmd.version=v0.18.0" \
  github.com/1broseidon/cymbal@v0.18.0
```

Prebuilt release binaries are also available at
[github.com/1broseidon/cymbal/releases/tag/v0.18.0](https://github.com/1broseidon/cymbal/releases/tag/v0.18.0).

### Indexing and worktree isolation

Cymbal maintains a per-repository SQLite index inside Git repositories without a
background daemon:

- **Initial and incremental indexing:** Run `cymbal index` once at the root of
  your Git repository to build the initial index (or `cymbal index --force` to
  rebuild from scratch). Later queries refresh incrementally against repository
  changes.
- **Worktree isolation (`--no-federate`):** Every JSON invocation Aider sends to
  Cymbal (`refs`, `impact`, `changed`, and `show`) unconditionally passes
  `--json --no-federate`. `CymbalClient` refuses to run without `--no-federate`,
  preventing cross-worktree symbol federation from leaking results across
  sibling checkouts.
- **Bounded subprocess execution:** Aider executes Cymbal with `shell=False`, a
  5-second per-command timeout, a 12-second total lookup deadline, hard output
  caps (256 KiB stdout, 16 KiB stderr), process-group cleanup on timeout, and
  strict repository-relative path validation.

### Diagnostic verification

Right after installing Cymbal, run this check from your Git repository root:

```sh
cymbal --version && cymbal index && cymbal changed --json --no-federate
```

Confirm that `cymbal --version` prints `cymbal v0.18.0` and `cymbal changed`
returns valid JSON. Inside this Aider repository, you can also run the live
pinned smoke test:

```sh
python -m pytest -q tests/basic/test_cymbal_callers.py -k test_pinned_v0_18_0_cymbal_smoke
```

## Local caller discovery

When `--local-callers` is enabled inside a Git repository, Aider inspects
symbols defined in editable chat files that match your task prompt and attaches
short, read-only caller excerpts before sending the turn.

### CLI flags

| Flag | Values / Default | Behavior |
| --- | --- | --- |
| `--local-callers` / `--no-local-callers` | Default: `False` | Include short read-only excerpts from callers of named functions or symbols. |
| `--local-callers-backend` | `builtin` \| `auto` \| `cymbal` (default: `builtin`) | Selects the discovery backend (`builtin` AST/Tree-sitter, `cymbal` external index, or `auto` with fallback). |
| `--local-callers-mode` | `passive` \| `contract` (default: `passive`) | `passive` attaches a read-only caller snapshot. `contract` requests `CALLER_CONTRACT:` and `TRACE:` sections before edit blocks (format check only; read-only turns such as `/ask` stay passive). |
| `--local-callers-audit` / `--no-local-callers-audit` | Default: `False` | Runs an isolated post-edit Chain-of-Verification (CoVe) caller-contract audit against the proposed diff and caller excerpts (returns `PASS`, `FAIL`, `UNKNOWN`, or `ERROR`). |
| `--local-callers-policy` | `strict` \| `advisory` (default: `strict`) | Governs audit enforcement: `strict` blocks unverified edits from auto-commit; `advisory` permits them with a diagnostic warning. |
| `--local-callers-hint` | `TEXT` (default: built-in hint) | Overrides the hint accompanying caller excerpts. Pass `""` for code excerpts only. |

### Backend fallback and error modes

- **`builtin` (default):** Uses Aider's in-process `PythonAstCallerFinder` and
  `TreeSitterCallerFinder` (`ReportingCompositeCallerFinder`). Never invokes the
  `cymbal` binary.
- **`auto` (diagnostic fallback):** Tries Cymbal first. On any `CymbalError`
  (`not_found`, `version_mismatch`, `timeout`, `output_limit_exceeded`,
  `protocol_error`, or `execution_failed`), records a diagnostic
  (`cymbal fallback (<code>): <message>`), displays a warning, and falls back to
  the `builtin` backend for that lookup.
- **`cymbal` (fail-loud):** Requires Cymbal and never silently falls back to
  `builtin`. If Cymbal is missing, wrong-version, timed out, or returns an
  error, Aider reports `Local callers (cymbal) failed: <err>` and records
  `NO_EVIDENCE (cymbal error: <err>)`.

### Evidence states

Every caller lookup prints a `Local caller evidence:` summary with one of three
typed `EvidenceStatus` values:

- **`NO_EVIDENCE`**: No caller excerpts were returned (for example, no matching
  symbol definitions or call sites in scope, or an explicit `cymbal` backend
  failure).
- **`INCOMPLETE`**: Caller excerpts were returned, but at least one ambiguity,
  truncation, staleness, or scan limitation was recorded (such as duplicate
  symbol definitions, alias/re-export indirection, transitive callers, stale
  Cymbal index, file/character truncation, or skipped large/unreadable files).
- **`COMPLETE_WITHIN_SCOPE`**: Caller excerpts were found with no recorded
  ambiguity, truncation, or limitation within the searched scope (displayed as
  `COMPLETE_WITHIN_SCOPE (SELECTED)` by the built-in reporter). This means
  complete *within the scanned scope*, not proof that no other runtime callers
  exist.

## Bounded ReadRange

Bounded ReadRange lets Aider and the user attach targeted, numbered line slices
from large files (`>128 KiB`) without adding whole files to the editable chat
context.

### Flag and `/read-range` command

- **`--local-callers-read-range` / `--no-local-callers-read-range`** (default:
  `False`): When `--local-callers` finds caller excerpts, automatically attaches
  bounded read-only call-site slices around up to 3 discovered call sites
  (`context_lines=5`, `max_lines=20`, `max_bytes=4096`, capped to at most 1,600
  characters of supplementary evidence).
- **`/read-range`**: Manually inspects and attaches a bounded, 1-based line
  range as read-only evidence. Three argument forms are accepted:

  ```text
  /read-range <path>:<start>-<end>
  /read-range <path> <start>-<end>
  /read-range <path> <start> <end>
  ```

  Single-line slices (`/read-range path/to/file.py:42` or
  `/read-range path/to/file.py 42`) are also supported. Default native limits
  are `max_lines=120`, `max_bytes=16384` (16 KiB), and `max_scan_bytes=16 MiB`.
  Up to 8 manual ranges are retained in the session; older entries roll off
  automatically.

### Cymbal `show` reuse vs. native streaming fallback

- Manual `/read-range` commands always stream directly from disk via
  `aider.read_range.read_range` without loading the full file into memory.
- Automatic call-site ranges (`--local-callers-read-range`) reuse
  `cymbal show --json --no-federate <path>:<line>` when the turn's caller lookup
  used Cymbal, and pass the payload through `validate_cymbal_show`:
  - If the returned body passes workspace guards and matches disk, the excerpt
    is kept with `source="cymbal"`.
  - If Cymbal's index disagrees with the file on disk (stale index), Aider
    substitutes the authoritative on-disk slice with `source="native"`,
    `stale=True`, and `limitations=("cymbal_stale",)`.
  - If `cymbal show` errors under `--local-callers-backend auto`, Aider falls
    back to native streaming `read_range`; under explicit `cymbal`, it warns
    that the call-site range is unavailable.

### Read-only guarantee, `/drop`, and `/read`

- **Read-only guarantee:** Ranges attached by `/read-range` or
  `--local-callers-read-range` are stored separately from editable chat files
  and labeled with `[provenance=read-range source=<path>:<start>-<end>]`. They
  never make the referenced file editable.
- **`/drop` cleanup:** `/drop` removes matching read-only ranges alongside
  normal files:
  - `/drop path/to/file.py:10-40` removes that exact range.
  - `/drop path/to/file.py` or a glob pattern such as `/drop "src/*.py"` removes
    all attached ranges matching that path.
  - `/drop` with no arguments clears editable files and attached read-only
    ranges (preserving files originally passed as read-only at startup).
- **`/read` alias:** `/read` is an alias for `/read-only` (adds whole files or
  directories as read-only context, or converts in-chat files to read-only when
  invoked with no arguments). Use `/read-range` when you want only a bounded
  line slice instead of the whole file.

### Workspace, encoding, and size guards

Both native `read_range` and `validate_cymbal_show` enforce the same guards:

- **Path guard (`PathGuardError`):** Rejects empty paths, NUL bytes, absolute
  paths, Windows drive prefixes, `..` traversal, directories/non-regular files,
  and any path escaping the repository root.
- **Symlink guard (`PathGuardError`):** Checks every path segment for symlinks
  and opens targets with `O_NOFOLLOW`.
- **Binary guard (`BinaryFileError`):** Stops and rejects files if a NUL byte
  (`\x00`) is encountered while scanning or reading.
- **UTF-8 guard (`InvalidEncodingError`):** Validates strict UTF-8 decoding on
  all scanned bytes instead of silently substituting invalid sequences.
- **Bounds (`ScanLimitExceededError` and truncation markers):** Enforces
  `max_scan_bytes`, `max_lines`, and `max_bytes`, annotating clipped slices
  with `[TRUNCATED: ...]` and UTF-8-safe boundary cuts.

## Project facts

Project facts record durable, repository-specific constraints (invariants,
calling conventions, error-handling contracts) scoped to a local worktree and
attached as read-only context on relevant turns.

### CLI flags and storage routing

| Flag | Default | Behavior |
| --- | --- | --- |
| `--facts` / `--no-facts` | `False` | Enables project facts context injection and the `/fact` command. |
| `--facts-file PATH` | `.aider.facts.jsonl` in project root | Sets the JSONL storage path when `--facts` is enabled without `--facts-db`. |
| `--facts-db PATH_OR_URL` | `None` | Selects embedded SQLite + FTS5 storage via a local file path or `sqlite://` URL (for example `.aider.facts.sqlite3`, `sqlite:///abs/path/facts.sqlite3`, or `sqlite://rel/path/facts.sqlite3`). Overrides the JSONL default when `--facts` is enabled. |

- **Default zero-dependency JSONL:** When `--facts` is enabled without
  `--facts-db`, Aider writes UTF-8 JSONL records to `.aider.facts.jsonl` using
  atomic rewrites. Each record carries a deterministic `worktree_id`
  (`wt:<24-char sha256>` of the resolved worktree root) and `chat_id`
  (`chat:<16-char sha256>` derived from the worktree root and chat history file,
  defaulting to `.aider.chat.history.md`).
- **Fail-closed `--facts-db` diagnostics:** When `--facts-db` is specified,
  Aider validates the path/URL (rejecting non-`sqlite://` schemes, malformed
  `sqlite:`/`file:` URLs, and `:memory:` / `mode=memory` targets) and probes
  Python's embedded `sqlite3` module and FTS5 extension. If validation or the
  probe fails, Aider reports a clear diagnostic warning, leaves existing files
  untouched, disables the store (`active=False`), and **does not** silently fall
  back to JSONL.

### `/fact` subcommands

Requires `--facts`:

| Command | Action |
| --- | --- |
| `/fact` or `/fact list` (alias: `/fact ls`) | List up to 20 active confirmed facts for the current worktree. |
| `/fact search <query>` (aliases: `/fact query`, `/fact find`) | Search up to 10 matching active facts (token search in JSONL; FTS5 `unicode61` in SQLite). |
| `/fact show <id>` (alias: `/fact get <id>`) | Display a single fact by ID (for example `fact_0123456789abcdef`). |
| `/fact confirm <id>` | Promote an `unverified` candidate fact to `confirmed` so it is eligible for prompt injection. |
| `/fact invalidate <id> [reason]` | Mark a fact `invalidated` with an optional reason (default: `invalidated`). |
| `/fact [add] [options] <statement>` | Record a new confirmed fact (`add` is optional). |

Supported options when recording a fact with `/fact [add]`:

- `-f PATH` / `--file PATH` (repeatable): Associate worktree-relative source
  files (non-symlinked). If omitted, Aider attaches the currently in-chat files.
- `-t TAG` / `--tag TAG` (repeatable): Attach searchable tags.
- `--symbol NAME`: Associate a symbol name.
- `--line START` or `--line START:END`: Associate a 1-based line or line range.
- `--note TEXT`: Store an auxiliary provenance note.
- `--`: End option parsing if the statement starts with `-`.

Only `confirmed`, `active` facts are injected into prompts (up to 5 facts and
4,000 characters), labeled as supplementary read-only context. Candidate facts
originating from a model stay `unverified` until explicitly confirmed via
`/fact confirm <id>`.

### Source-hash staleness invalidation

When a fact is recorded, Aider computes and stores the SHA-256 hash of every
associated worktree file (along with the current Git `HEAD` commit SHA when
available). Before listing, searching, or injecting facts on a turn,
`revalidate()` re-hashes those files on disk:

- If a referenced file's SHA-256 digest changed or the file was deleted, the
  fact transitions automatically from `active` to `stale` with a recorded
  `stale_reason` and is excluded from automatic prompt injection.
- Re-record a current fact (or invalidate the old ID) after verifying the new
  behavior.

## Configuration examples for local Qwen models

### LM Studio (`.aider.conf.yml` and CLI)

Start LM Studio's local API server (typically `http://localhost:1234/v1`) and
load your Qwen coding model, then export:

```sh
export LM_STUDIO_API_BASE=http://localhost:1234/v1
export LM_STUDIO_API_KEY=dummy-api-key
```

Example `.aider.conf.yml` using `auto` Cymbal fallback, bounded call-site
ranges, and default JSONL project facts:

```yaml
model: lm_studio/qwen/qwen3.5-9b
local-callers: true
local-callers-backend: auto
local-callers-mode: passive
local-callers-read-range: true
facts: true
```

Equivalent CLI invocation (no `.aider.conf.yml` required):

```sh
aider \
  --model lm_studio/qwen/qwen3.5-9b \
  --local-callers \
  --local-callers-backend auto \
  --local-callers-read-range \
  --facts \
  api.py
```

For zero external binaries (`builtin` AST/Tree-sitter only) with strict
post-edit CoVe verification:

```sh
aider \
  --model lm_studio/qwen/qwen3.5-9b \
  --local-callers \
  --local-callers-backend builtin \
  --local-callers-mode contract \
  --local-callers-audit \
  --local-callers-policy strict \
  --local-callers-read-range \
  --facts \
  api.py
```

### MLX (`mlx_lm.server`, OpenAI-compatible)

If you serve a local Qwen model on Apple Silicon via `mlx_lm.server` (default
`http://127.0.0.1:8080/v1`):

```sh
export OPENAI_API_BASE=http://127.0.0.1:8080/v1
export OPENAI_API_KEY=mlx-local
```

Example `.aider.conf.yml` with explicit SQLite FTS5 facts storage:

```yaml
model: openai/mlx-community/Qwen2.5-Coder-14B-Instruct-4bit
local-callers: true
local-callers-backend: cymbal
local-callers-read-range: true
facts: true
facts-db: .aider.facts.sqlite3
```

Equivalent CLI invocation:

```sh
aider \
  --model openai/mlx-community/Qwen2.5-Coder-14B-Instruct-4bit \
  --local-callers \
  --local-callers-backend cymbal \
  --local-callers-read-range \
  --facts \
  --facts-db .aider.facts.sqlite3 \
  api.py
```

## End-to-end walkthrough

This walkthrough updates a shared helper (`parse`) in a multi-file Python repo
while inspecting a large caller file safely and tracking a contract invariant.

1. **Verify Cymbal and build the repo index** (skip if using `builtin`):

   ```sh
   cymbal --version && cymbal index && cymbal changed --json --no-federate
   ```

2. **Start Aider with local context enabled** and only the file you plan to edit:

   ```sh
   aider \
     --model lm_studio/qwen/qwen3.5-9b \
     --local-callers \
     --local-callers-backend auto \
     --local-callers-read-range \
     --facts \
     --no-auto-commits \
     api.py
   ```

3. **Record a verified project invariant** tied to `api.py`:

   ```text
   /fact -f api.py -t contract --symbol parse "parse() must raise ValueError on blank strings; callers do not check for None"
   ```

   Aider hashes `api.py` and writes a confirmed entry to `.aider.facts.jsonl`.
   Check stored facts anytime with `/fact list` or `/fact search parse`.

4. **Inspect a specific slice of a large caller file** without loading the whole
   file into editable context:

   ```text
   /read-range consumer.py:110-145
   ```

   Aider streams lines 110–145 from disk, prints the numbered slice, and
   attaches it as read-only evidence (`[provenance=read-range source=consumer.py:110-145]`).

5. **Request the edit:**

   ```text
   Update parse() to strip surrounding whitespace and reject blank input with ValueError.
   ```

   Before sending the prompt, Aider:
   - Revalidates active facts for `api.py` and injects matching confirmed facts.
   - Discovers callers of `parse` (via Cymbal with `--no-federate`, or falling
     back to `builtin` with a diagnostic warning if Cymbal is unavailable) and
     prints `Local caller evidence: ...` with its `EvidenceStatus`.
   - Attaches bounded call-site ranges around discovered callers and your
     manual `/read-range` slice as read-only context.

6. **Review, test, and clean up context:**
   - Review the diff in `api.py` and run your test suite (for example
     `/test pytest -q`).
   - Drop the temporary line slice when you no longer need it in context:

     ```text
     /drop consumer.py:110-145
     ```

   - Because `api.py` changed on disk, the next `/fact list` or prompt turn
     automatically marks the earlier `api.py` fact `stale` via its SHA-256 hash
     so outdated invariants are never silently injected.

## Limitations of AST and name-based resolution

Local caller discovery and ReadRange are designed to reduce blind edits and
token waste, not to replace compilers, type checkers, or tests.

- **Dynamic dispatch and indirection:** Reflection (`getattr`, `setattr`,
  dynamic imports), monkey-patching, dependency-injection registries,
  decorators/metaclasses, event buses, callbacks passed as values, and
  string-dispatched handlers can invoke a symbol without a static call site that
  AST or name-based indexes recognize.
- **Duplicate symbol names and aliases:** Repositories with common method names
  (`parse`, `run`, `handle`, `render`) can trigger name-collision ambiguity,
  while aliased imports and re-exports can either hide callers or produce false
  positives. When detected, Aider surfaces these under `INCOMPLETE` ambiguity
  notes.
- **Index staleness:** Cymbal's SQLite index can lag rapid uncommitted edits or
  newly added files. Aider checks `cymbal changed --json --no-federate` and
  cross-checks `cymbal show` bodies against disk, marking drifted results as
  stale (`INCOMPLETE` / `cymbal_stale`), so run `cymbal index` after large
  refactors.
- **Intentional bounds and truncation:** Built-in AST/Tree-sitter scanning
  skips candidate files larger than 128 KiB (`MAX_SOURCE_BYTES`), and both
  backends cap returned files, characters, lines, and bytes. Callers beyond
  those caps are not shown.
- **Evidence is supporting input, not proof:** Neither `COMPLETE_WITHIN_SCOPE`,
  a `CALLER_CONTRACT`/`TRACE` response, nor an isolated `VERDICT: PASS` audit
  proves runtime correctness. Treat all local-context outputs as supporting
  evidence alongside automated tests, type checks, and code review.
