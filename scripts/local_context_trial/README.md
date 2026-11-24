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

- `--suite replay --replay-manifest PATH`: load a local frozen replay manifest (`--replay-manifest` is rejected for non-replay suites).
- `--arms`: comma-separated arms from `baseline`, `passive`, `contract`, `audit`, `contract+audit`.
- `--local-callers-backend`: single backend or comma-separated list (e.g., `ast,cymbal`).
- `--read-range-modes`: comma-separated factorial grid values from `default,off,on`.
- `--facts-modes`: comma-separated factorial grid values from `default,off,on`.
- `--check-fixtures`: offline smoke/hidden fixture self-check with no LLM calls.
- `--summarize`: print grouped pass rates, paired sign tests, 95% CIs, and arm telemetry from `--out`.
