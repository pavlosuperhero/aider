# Branch review against main — 2026-11-19

**Status: all five findings below have been addressed in the working tree.** The findings describe the reviewed baseline; the follow-up implemented code fixes and regression tests rather than leaving them as recommendations.

## Implemented follow-up

- Green now requires an individual `PASSED` result for every recorded red node. CLI setup adds pytest `-rA`; missing or truncated node results leave green unverified, while unrelated skips are allowed. Incomplete red results cannot release production edits.
- Completion requirements run after edit parsing, including for fenced reports. The controller no longer marks a response done before knowing whether it contains edits.
- Source defaults and virtual-environment detection receive the project root from CLI composition.
- `--oracle-doc` now loads an explicitly supplied existing read-only reference through the same path as `--read`. There is no implicit checkout-relative document and no unused `OracleConfig.doc_path` field.
- Custom script modules are registered before execution under distinct names, retained for successful loads, and removed on failed imports. Annotated dataclasses and repeated loads are covered.
- The Oracle compatibility workflow now triggers on and runs the new TDD and generic-hook test suites. The user guide describes the implemented contracts.

Current verification: **239 focused tests passed**, plus **278 compatibility tests and 29 subtests passed**. One pre-existing home-directory-write test was excluded because of the sandbox restriction recorded below. These current results supersede the baseline counts later in this report. No live-model or cross-version run was performed.

Oracle grounding: the original five-source evidence below was reused. A focused follow-up query, “How should explicit postconditions and special-case tests verify that program state reflects the operation that actually completed?”, returned Think Python PDF p. 109 (`d74ea9c5bf4bd735790711fc`) and p. 221 (`31fd4b06c7c90d31904a15fb`). Application: verify actual node outcomes and test the failure cases at the component boundary. Source evidence supports the method; regression tests establish the concrete fixes.

## Scope

- Branch: `test/aider-oracle-quality`, HEAD `5e5cd6b6d95a8b24c24fb8438f88fedf3f212251`.
- Local `main` and merge base: `8448ce9f8af3e5bd2c5e9a7cf9767c36b42b7488`.
- Reviewed `main...HEAD`: eight commits, 22 files, 4,218 insertions and 124 deletions, including code, tests, requirements and documentation. No remote refresh was performed.
- Pre-existing untracked `.ignore`, `docs/tasks/oracle-quality-report.md` and `docs/tasks/oracle-tdd-agent-handoff.md` are outside the branch diff and were preserved.

## Findings

### 1. P1 — Green does not establish that the red regression passed

`aider/tdd.py:352-355,801-835`

The classifier accepts any exit-zero run containing at least one passing test. The controller does not check whether the previously failing node ran and passed. An integration probe adds an assertion that fails, then changes production code to call `pytest.skip()` on that input. The old test passes, the new regression skips, and the controller reaches `DONE` with `{'passed': 1, 'skipped': 1}`. Its continuation nevertheless says the new/updated test was observed red then green.

**Fix:** retain the red node identities and verify their successful execution before satisfying the regression requirement. Prefer structured runner results over inferring node outcomes from aggregate counts. If structured results are deferred, reject ambiguous green runs and narrow the completion claim. Test skipped, xfailed and deselected red nodes explicitly; unrelated pre-existing skips need not invalidate the entire suite.

**Principle:** verify postconditions and atypical cases rather than inferring a stronger guarantee from a weaker observation (Think Python, PDF pp. 81, 109; evidence below).

### 2. P1 — Fenced final reports silently bypass unmet completion requirements

`aider/tdd.py:620-624,704-711`

In `REPORT`, any response containing triple backticks bypasses `_unmet_requirements`. When the edit parser finds no edits, `check_edits` accepts the empty proposal. A real diff-coder probe completes red/green with required `docs/*.md`, then replies with a fenced text summary. Aider ends the turn with no corrective continuation, no documentation update and state still `REPORT`. The same path bypasses the controller's optional required-citation check. Oracle's separate unknown-ID check still applies when enabled.

**Fix:** use parsed edits to distinguish an edit from a non-edit report. Apply completion requirements to an empty parsed proposal, even when the response contains formatted examples. Test fenced reports with missing docs and required citations, as well as valid fenced reports that should finish normally.

**Principle:** model text formatting is not execution state; validate workflow postconditions at the boundary that has the parsed observation. See Think Python p. 81 and Cosmic Python service-layer evidence.

### 3. P2 — Default source scope uses a different root from the edit hooks

`aider/tdd.py:80-87,140-152` and `aider/main.py:982-984`

`TddConfig.from_args` normalizes chat filenames against the terminal working directory. Edit hooks normalize them against `coder.root`, which is the Git project root. Launching inside `feature/` with `feature/calc.py` open adds `calc.py` to the default source globs, while the hook sees `feature/calc.py`. A probe confirms the file is classified as outside scope. The default test-command environment detection also uses the working directory rather than the eventual runner root.

**Fix:** resolve one project root at composition time and use it for source-glob expansion and default test-command detection. Add an integration test launched from a nested directory with a repository-root virtual environment.

**Principle:** keep the representation crossing a boundary consistent and explicit (Clean Architecture, passage `62436485f5653f4d006da35c`).

### 4. P2 — `--oracle-doc` is an inert public option

`aider/args.py:119-123`, `aider/oracle.py:66,144`, `docs/oracle.md:155-158,309,352-355`

The new option is copied into `OracleConfig.doc_path` but never read by retrieval, prompt assembly or composition. Supplying a custom policy/reference path has no effect. The guide correctly says Markdown is not automatically loaded, yet also advertises this flag as a customizable policy/reference path and demonstrates it in setup. The default `docs/` location is also not guaranteed to exist in a wheel installation.

**Fix:** define and implement the option's behavior, including missing-file and installed-package handling, or remove the option and direct users to the existing `--read` mechanism. Keep retrieved references distinct from trusted system instructions. A field-assignment assertion alone would not verify this feature.

**Principle:** documented interfaces create a public contract (PEP 8, lines 1150-1201). Keep prompt/reference behavior explicit and test the actual outgoing context.

### 5. P2 — Custom controller loading breaks normal annotated dataclasses

`aider/tdd.py:856-860`

The dynamic loader executes the custom module without registering it in `sys.modules`. Dataclasses with postponed/string annotations inspect that registration. Copying the current controller into a custom script with `from __future__ import annotations` and loading it raises `AttributeError: 'NoneType' object has no attribute '__dict__'` on Python 3.12. The existing custom-script test covers only a minimal subclass, so it misses this common customization.

**Fix:** register the module before execution, handle cleanup/restoration on failure, and test a custom controller containing annotated dataclasses. Use a deliberate module-naming strategy for repeated loads.

**Principle:** test the real adapter boundary as well as fake orchestration (Cosmic Python service-layer evidence). The observed runtime failure, rather than the architectural reference, establishes this bug.

## Architecture, Python style and documentation assessment

The optional controller depends on plain hook data rather than `Coder` or `Commands`; `main` composes it and injects the citation validator. This follows the retrieved dependency-direction and service-layer guidance. A wholesale architecture rewrite is not justified. The concrete problems are observation fidelity, completion enforcement and adapter/default handling.

The changed Python files pass the repository's Black settings. PEP 8 is Active in the indexed source; its guidance explicitly favors project consistency over mechanical style conformance. There is no reason to impose a different line length here. Generic hook interfaces are documented, and Oracle's fixed public errors avoid exposing transport detail. No new bare `except:` issue was identified.

The guide appropriately separates passage identity from semantic support and retrieval success from model quality. However, its red/green and completion claims should be corrected alongside findings 1 and 2. Prompt constants are versioned and separated from integration code; fabricated-response tests establish protocol behavior, not live-model reliability. No live-model evaluation was performed in this review.

The existing `.github/workflows/oracle-compatibility.yml` runs Oracle and scripting tests, but does not run the newly added TDD/edit-hook/test-result-hook suites. Add those suites and their path triggers to automated validation before relying on CI for this feature.

## Verification

- Oracle, TDD, edit-hook and test-result-hook suites: **216 passed**.
- Coder, repo-map, commands and main suites: **236 passed, 29 subtests passed; one sandbox-related failure**. `TestCommands.test_cmd_read_only_with_tilde_path` tries to write `/Users/Pavlo_Fabuliak/test_read_only_file.txt`, outside the writable roots. This is not evidence of a branch regression.
- Scripting compatibility: **1 passed**.
- Whole-file, edit-block and unified-diff parser regression suites: **41 passed**.
- Total repository tests across these runs: **494 passed, 29 subtests passed, one sandbox-related failure**. The four diagnostic probes below are additional.
- Four temporary review probes: **4 passed**, meaning they reproduced the undesirable behaviors in findings 1, 2, 3 and 5. They are diagnostic probes, not regression tests asserting the desired fixed behavior. Location: `/private/tmp/test_branch_review_probes.py`.
- `black --check --line-length 100 --preview` on all 17 changed Python files: passed. Used the available global Black because the project virtual environment does not contain it.
- `git diff --check main...HEAD`: passed.
- Python execution used the project's Python 3.12 virtual environment. Other supported Python versions, a wheel installation, full-repository tests and live-model behavior remain unverified.

## Oracle evidence and queries

The live catalog was discovered once. All five requested source groups returned evidence. The Clean Architecture item is the indexed article, not the complete book. The prompt-engineering table-of-contents hit was not used as support.

| Source filter | Focused query | Applied passages and locations |
| --- | --- | --- |
| `clean-architecture` | How should application workflow policy separate from external tools and infrastructure, and what makes a component testable? | Clean Coder Blog, `76c84fcb503f247ba9139a0e`, `a892b437bacbc32f4e64e3db`, `62436485f5653f4d006da35c`; no page/line locator. |
| `cosmic-python` | How should service layer workflow orchestration use dependency injection and test doubles to test failure paths and side effects? | Our First Use Case: Flask API and Service Layer, `04241f9719324da23e250d96`, `354853d1e08d13856ad0eb16`, `3e5856efac2187a0c1f9b7e1`; no page/line locator. |
| `think-python` | How should function preconditions, invariants and incremental testing detect boundary cases and incorrect state transitions? | Think Python, 2nd ed., `0284bd306f4d5bc7d5237220`, PDF p. 81; `d74ea9c5bf4bd735790711fc`, p. 109; `12b4141b6f936927d5dbc493`, p. 76. |
| `peps/pep-0008` | What are the guidelines for exception handling, public interfaces, readability and keeping documentation up to date? | PEP 8, `9dd3c88d64ffd6bbbba235ec`, lines 1286-1337; `f63a2ae30b4e3e58c1bd29e2`, lines 1113-1161; `7988cf19bb4167cfcea9978d`, lines 1150-1201. |
| `prompt-engineering` | How should retrieved reference text be separated from instructions and prompts evaluated with representative tests? | Google Prompt Engineering, February 2025, `90595d98465d965c36f7c6b0`, PDF p. 65; `a85c0612f27a0d4af6804b6d`, p. 57. Supports evaluation and clear action instructions; these returned passages do not themselves establish an injection-isolation guarantee. |
| `peps/pep-0008` | What does PEP 8 say about comments contradicting code, keeping comments up to date, and project consistency? | `2f5cc1ec0a890ecf1acaaa48`, lines 1-59. Supports Active status and project consistency; the returned passage does not address comment maintenance. |

Source origins returned by Oracle are under `/Users/Pavlo_Fabuliak/.config/oracle/data/`: `clean-architecture/clean_architecture.html`, `cosmic-python-service/cosmic-service.html`, `think-python/think_python_2e.pdf`, `peps/pep-0008.rst`, and `prompt-engineering/google_prompt_engineering.pdf`. PDF locators above are PDF page numbers. Findings and recommended fixes are this review's application of those principles to reproduced repository behavior.
