# Локальний код виклику в запиті до Qwen

Пілот перевіряє, чи може Aider самостійно знайти фрагмент коду, де використовується функція з поточної задачі, та покращити патч локальної моделі. Oracle працює у звичайному процесі Aider. Пошук викликів використовує Python AST і список відстежуваних Git-файлів, які дозволяє Aider.

## Повторити

У середовищі з установленими залежностями форка, налаштованим Oracle й локальним LM Studio виконай з кореня цього worktree:

~~~sh
python scripts/local_context_trial/run.py --rounds 2
python scripts/local_context_trial/run.py --map-tokens 1024 --specify-behavior
~~~

Для звичайної задачі з цієї гілки можна ввімкнути експеримент прапорцем `--local-callers`. За замовчуванням він вимкнений. Наразі запит має містити точне ім’я Python-функції верхнього рівня, оголошеної у файлі чату. Прототип розпізнає прямий абсолютний імпорт цієї функції, включно з псевдонімом. Відносні імпорти, методи та проєкти з окремим коренем імпорту на кшталт `src/` потребують подальшої перевірки й підтримки. Фрагмент навколо виклику може обрізати важливу умову. Пошук читає робочі файли на кожен новий запит, пропускає символічні посилання та файли понад 128 КіБ і повертає до трьох фрагментів по 700 символів. Швидкодію на великих репозиторіях цей пілот не вимірює.

## Як отримано оцінку

Кожен запуск починається в окремому тимчасовому репозиторії. Модель бачить задачу, файл для редагування та звичайний контекст Aider. Робоче дерево містить також споживача API й відкриту перевірку. Обидва варіанти використовують той самий локальний ідентифікатор моделі, формат diff та adaptive Oracle з обов’язковим успішним початковим завантаженням джерел. Додатковий пошук Oracle добровільний. Увімкнено звичайний auto-test; контролер TDD вимкнено для ізоляції ефекту контексту. При додатковому повторі порядок запуску пари змінюється. Серверні параметри семплінгу успадковано, seed не фіксувався. Час включає старт Aider й отримання контексту; це не вимірювання чистої швидкості генерації.

Незалежні перевірки записані у сценарії до запуску. Вони виконуються після виходу Aider та не потрапляють у тестовий репозиторій. Вони перевіряють результат через публічну функцію споживача. Такий критерій відповідає outside-in підходу з *Test-Driven Development with Python*, розділ 24, Oracle passage `f06ebccaa778a5b7dbbba157`. Повторення на тих самих задачах перевіряє стабільність. Чотири синтетичні задачі не дають оцінки загальної точності на реальних issue.

Сценарій друкує шлях до каталогу конкретного запуску. У ньому залишаються журнали Aider, кандидати патчів та діагностика. У `results.jsonl` поруч із цим README збережено підсумки виконаних серій і фактичний патч кожної спроби.

## Результати 30 локальних запусків

| Серія | База, пройшли | Медіана, с | З контекстом, пройшли | Медіана, с |
|---|---:|---:|---:|---:|
| reference | 0/3 | 7.2 | 1/3 | 7.2 |
| task | 2/8 | 6.5 | 3/8 | 9.7 |
| explicit | 1/4 | 7.2 | 4/4 | 8.5 |

У серії `reference` (коміт `09949d4`) Aider вкладав фрагмент у загальний блок матеріалів лише для читання. Три задачі, по одному запуску на варіант. У серії `task` фрагмент було перенесено до поточного запиту з підказкою перевірити використання значення. Додано контрольну задачу clamp, кожну пару пройдено двічі. В обох серіях карта репозиторію вимкнена. У серії `explicit` обидва варіанти отримали явну вимогу до поведінки споживача і звичайну карту на 1024 токени. Між цими серіями одночасно змінилися вимога й карта, тому окремий вплив кожної з цих двох змін не виміряно.

У фінальній серії базовий варіант виконав лише clamp, експериментальний — усі чотири задачі. Для cursor вимога пояснювала, що порожній ввід має перезапустити пагінацію. Внутрішнє значення -7 було видно лише в коді. У попередній серії модель з фрагментом правильно описувала значення -7, але все одно повертала 0. Окремий критерій успіху дозволив це виявити.

Попередній висновок: на цих підібраних задачах локальний фрагмент дає виграш, коли постановка чітко описує потрібну поведінку. Для перенесення висновку на реальні issue потрібні нові, не використані для налаштування задачі.

## Налаштування для проєкту

У `.aider.conf.yml` параметр `local-callers: true` вмикає автоматичний добір.
Стандартна підказка відповідає фінальній серії експерименту. Щоб замінити її,
задай текст у `local-callers-hint`. Значення `local-callers-hint: ""` залишає
фрагмент із позначкою джерела. Підказка з’являється лише за наявності фрагмента.
На один запуск налаштування можна перевизначити через
`--local-callers-hint="..."`, `--local-callers-hint=` або `--no-local-callers`.

## Issue 5: Project Facts, Bounded Read-Range, and Backend Evaluation

The self-contained `issue5` suite in `scripts/local_context_trial/issue5_cases.py`
evaluates five focused fixtures covering confirmed project facts (`facts_currency`,
`facts_sentinel`), source-hash stale-fact invalidation (`facts_stale_control`),
bounded read-range beyond the 128 KiB limit (`read_range_large_file`), and scoped
multi-consumer caller discovery (`caller_multi_scope`).

### 1. Offline fixture self-check (`--check-fixtures`)

Verify that every Issue 5 fixture seeds cleanly, passes its smoke check, and fails
its hidden check without calling any LLM:

~~~sh
python scripts/local_context_trial/heldout.py --suite issue5 --check-fixtures
python scripts/local_context_trial/heldout.py \
  --suite issue5 \
  --cases facts_currency,read_range_large_file \
  --check-fixtures
~~~

### Small-model input-token trial

Run one model at a time with LM Studio and Oracle available. Set `MODEL` to the
Aider model identifier served locally, for example `lm_studio/<served-id>`.
Use a fresh output directory for each model. Record its weights revision, quantization,
context window, inference server version and sampling settings alongside the results.
Keep those settings fixed across arms.

~~~sh
bash scripts/local_context_trial/issue5_input_trial.sh "$MODEL" issue5-small-model-run
~~~

The launcher runs three rounds with vague prompts, the builtin caller finder and a
1,024-token repository-map budget. The reference is passive caller excerpts with facts
and extra ranges off. The five Issue 5 fixtures cross facts off/on with ranges off/on
(60 attempts). Compare each single-feature arm with the reference; use the combined
arm to inspect interaction. Two multi-file tasks from Requests 2.34.2 then compare
ranges off/on with facts off (12 attempts). They exercise copy independence and
exception pickling. Both suites check hidden behavior after Aider exits.

Read these measures together:

- **First input** (`first_prompt`) gives the paired change in tokens sent on the first
  editor request. **Cumulative input** (`prompt`) includes repeated context on later
  calls. The report gives the mean difference in tokens per attempt, percentage change
  relative to matched reference inputs, and the number of measured pairs. Aider uses
  its tokenizer estimate when the editor provider omits usage. Older total-only logs
  have no input breakdown.
- **Correct edits** must pass smoke and hidden checks and satisfy the runner's file-scope
  check. Inspect paired wins and losses on the same case and round, especially regressions
  on the stale-fact control. **Input tokens per solved task** (`in/sol`) charges the
  arm for every attempt, including failures. Missing input usage or zero solves yields
  an unavailable value.
- Inspect `facts_attached` and `read_range_attached` in each row. The range option
  needs a discovered caller. Include runs without attachments in the primary result;
  this measures the behavior users would actually get.
- Report each case separately as well as aggregate results. Three repeats help reveal
  model variability; the pilot has seven distinct tasks. Use a separately frozen
  real-repository replay set before generalizing or choosing defaults. Oracle reference:
  *Introduction to Information Retrieval*, p. 320, passage `069dc983a616649ad3adc765`.

### 2. Fast economy & efficiency workflow (`--preset issue5-economy`)

Use `--preset issue5-economy` to measure Aider token economy and wall-clock efficiency on the
Issue 5 suite without repeating redundant multi-hour `contract`/`audit` sweeps. By default the
preset expands to:

- `--suite issue5`
- `--arms passive`
- `--specs vague`
- `--rounds 1`
- `--read-range-modes off,on`
- `--facts-modes off,on`

Any flag (for example `--rounds`, `--cases`, `--facts-db`, `--facts-file`, or
`--local-callers-backend`) can still be overridden explicitly on the CLI:

~~~sh
# Default JSONL project facts + bounded read-range economy grid (1 round, passive arm)
python scripts/local_context_trial/heldout.py \
  --preset issue5-economy \
  --out /path/to/issue5_economy_jsonl.jsonl

# Same fast preset with SQLite FTS5 project facts via --facts-db
python scripts/local_context_trial/heldout.py \
  --preset issue5-economy \
  --facts-db .aider.facts.sqlite3 \
  --out /path/to/issue5_economy_sqlite.jsonl

# Summarize economy & efficiency metrics from recorded JSONL rows
python scripts/local_context_trial/heldout.py \
  --summarize \
  --out /path/to/issue5_economy_jsonl.jsonl
~~~

### 3. Economy & efficiency metrics

`heldout.py --summarize` (and `build_summary`) prints a dedicated **Economy & efficiency** table
alongside grouped pass rates, paired sign tests, and `Run metadata` (including `facts` and
`facts_backend` when present):

- **Token distribution (`tok_avg`, `tok_p50`, `tok_p95`)**: per-arm mean, median (`p50`), and
  tail (`p95`) total tokens across measured runs.
- **Tokens per solved task (`tok/sol`)**: `tokens_total / passed` when `passed > 0` and every run
  in the arm has measured token counts (`n/a` otherwise).
- **Latency distribution (`s_avg`, `s_p50`, `s_p95`)**: per-arm mean, `p50`, and `p95` wall-clock
  seconds.
- **Prompt/completion & call efficiency (`prompt`, `compl`, `calls`)**: per-arm
  `mean_prompt_tokens`, `mean_completion_tokens`, and `mean_editor_calls`.
- **Paired % deltas vs reference (`d_tok`, `d_sec`)**: `delta_tokens_pct` and `delta_seconds_pct`
  computed over matched `(case, spec, round)` pairs against the reference arm (also included in
  `Paired overall intervals (95% CI)`).

### 4. Targeted Issue 5 comparisons (optional)

- **Project facts (JSONL default with deterministic `chat_id`)**: compares `--no-facts`
  against `--facts` using `.aider.facts.jsonl` (override path with `--facts-file`):

~~~sh
python scripts/local_context_trial/heldout.py \
  --suite issue5 \
  --cases facts_currency,facts_sentinel,facts_stale_control \
  --arms passive \
  --facts-modes off,on \
  --rounds 3 \
  --out /path/to/issue5_facts_jsonl.jsonl
~~~

- **Project facts (SQLite FTS5 via `--facts-db`)**: compares `--no-facts` against
  `--facts --facts-db .aider.facts.sqlite3` (accepts a path or `sqlite://` URL):

~~~sh
python scripts/local_context_trial/heldout.py \
  --suite issue5 \
  --cases facts_currency,facts_sentinel,facts_stale_control \
  --arms passive \
  --facts-modes off,on \
  --facts-db .aider.facts.sqlite3 \
  --rounds 3 \
  --out /path/to/issue5_facts_sqlite.jsonl
~~~

- **Bounded read-range (`--read-range-modes off,on`)**: tests reading call sites deep
  inside files larger than 128 KiB (`read_range_large_file`):

~~~sh
python scripts/local_context_trial/heldout.py \
  --suite issue5 \
  --cases read_range_large_file \
  --arms passive \
  --read-range-modes off,on \
  --rounds 3 \
  --out /path/to/issue5_read_range.jsonl
~~~

- **Caller backend comparison (`--local-callers-backend builtin,auto`)**: compares
  `builtin` (AST/tree-sitter) against `auto` (Cymbal with builtin fallback, or add
  `cymbal` directly when pinned) on the fast `passive` arm:

~~~sh
python scripts/local_context_trial/heldout.py \
  --preset issue5-economy \
  --local-callers-backend builtin,auto \
  --out /path/to/issue5_backend_economy.jsonl

python scripts/local_context_trial/heldout.py \
  --summarize \
  --out /path/to/issue5_backend_economy.jsonl
~~~

## Issue 5: Frozen Replay Corpus Comparison

No replay corpus manifest or external task archive is bundled in this repository, and no replay results are fabricated here. A human operator supplies a local frozen JSON manifest pointing to local version-pinned git checkouts and external hidden verification files, then runs the factorial comparison through `scripts/local_context_trial/heldout.py`.

### 1. Preregister and pin before running

Record the following pins in your preregistration log and manifest before invoking any LLM:

- **Model and revision**: exact `--model` identifier plus local model revision, quantization, and inference server build.
- **Randomization seed**: fixed `--seed` value controlling paired job ordering (default `20261123`).
- **Repo map token budget**: fixed `--map-tokens` across every arm and control cell (e.g., `1024`).
- **Frozen manifest digest**: compute the canonical digest with `replay_corpus.py digest`, set `preregistration.frozen: true` and `preregistration.manifest_sha256` in the manifest, and verify fail-on-base / pass-on-fix integrity before running:

~~~sh
python scripts/local_context_trial/replay_corpus.py digest /path/to/manifest.json
python scripts/local_context_trial/replay_corpus.py validate /path/to/manifest.json \
  --require-frozen --run-checks
~~~

- **Backend tool version**: pin Cymbal to `v0.18.0` when comparing the `cymbal` backend against `ast` or `graft`, and keep the `.meta.json` sidecar written alongside `--out` for the Aider git commit and Python version.

### 2. Verify fixtures offline (`--check-fixtures`)

`--suite replay` enforces `require_frozen=True` and never executes external hidden commands during manifest loading. To verify that every materialized replay case passes smoke and fails hidden checks without calling an LLM:

~~~sh
python scripts/local_context_trial/heldout.py \
  --suite replay \
  --replay-manifest /path/to/manifest.json \
  --check-fixtures
~~~

### 3. Run the comparison and summarize (`--summarize`)

Run the paired comparison across arms and independent backend/read-range/facts controls, then summarize the recorded JSONL rows:

~~~sh
python scripts/local_context_trial/heldout.py \
  --suite replay \
  --replay-manifest /path/to/manifest.json \
  --model <model-id> \
  --seed 20261123 \
  --map-tokens 1024 \
  --rounds 3 \
  --arms baseline,passive,contract,audit,contract+audit \
  --local-callers-backend ast,cymbal \
  --read-range-modes off,on \
  --facts-modes off,on \
  --out /path/to/replay_results.jsonl

python scripts/local_context_trial/heldout.py \
  --summarize \
  --out /path/to/replay_results.jsonl
~~~

Key CLI flags:

- `--preset issue5-economy`: fast economy & efficiency defaults (`--suite issue5 --arms passive`
  `--specs vague --rounds 1 --read-range-modes off,on --facts-modes off,on`), overridable via CLI.
- `--suite {synthetic,requests,issue5,replay}`: select benchmark suite (`--replay-manifest PATH`
  is required for `replay` and rejected for non-replay suites).
- `--arms`: comma-separated arms from `baseline`, `passive`, `contract`, `audit`, `contract+audit`.
- `--local-callers-backend`: single backend or comma-separated list (e.g., `builtin,auto`).
- `--read-range-modes`: comma-separated factorial grid values from `default,off,on`.
- `--facts` / `--no-facts`, `--facts-modes`: toggle or sweep Issue 5 project facts
  (`default,off,on`).
- `--facts-db`, `--facts-file`: route project facts to a SQLite FTS5 database or JSONL file.
- `--local-callers-override` (`--no-local-callers-override`): override `--local-callers`
  independently of arm defaults.
- `--check-fixtures`: offline smoke/hidden fixture self-check with no LLM calls.
- `--summarize`: print grouped pass rates, paired sign tests, 95% CIs, Economy & efficiency
  table, and arm telemetry from `--out`.
