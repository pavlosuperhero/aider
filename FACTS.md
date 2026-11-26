# Project facts

These entries describe checkable behavior in this Aider fork. Read the relevant source at the current revision before relying on an entry. The initial implementation facts were checked against `c4dd6b3`.

| ID | Scope | Status | Fact | Source |
| --- | --- | --- | --- | --- |
| F-001 | Repository memory | Active | Repository agents share versioned Markdown memory at the Git root. | [Repository instructions](AGENTS.md) |
| F-002 | Aider CLI | Active | Project fact injection and the `/fact` command are opt-in via `--facts`. | [CLI flags](aider/args.py), [Project FactStore](aider/project_facts.py) |
| F-003 | Aider Facts storage | Active | The default Facts backend uses `.aider.facts.jsonl` at the project root. | [CLI flags](aider/args.py), [Backend routing tests](tests/basic/test_project_facts.py#L130) |
| F-004 | Aider Facts verification | Active | Model candidates are excluded from default retrieval until explicitly confirmed. | [Candidate confirmation test](tests/basic/test_project_facts.py#L538) |
| F-005 | Aider Facts provenance | Active | A change to a bound source invalidates a retrieved fact's currentness. | [Source invalidation test](tests/basic/test_project_facts.py#L576) |
| F-006 | Aider caller evidence | Active | The built-in index is the default backend when local callers are enabled. | [CLI flags](aider/args.py) |
| F-007 | Aider caller context | Active | Bounded read-only line-range inspection is opt-in via `--local-callers-read-range`. | [CLI flags](aider/args.py) |
| F-008 | Trial acceptance | Active | Issue #13's Accepted@1 counts a first edit proposal only after independent acceptance checks and scope verification. | [Acceptance criteria in issue #13](https://github.com/pavlosuperhero/aider/issues/13) |
| F-009 | Aider Facts storage | Active | Passing `--facts-db` selects SQLite when Facts is enabled. | [CLI flags](aider/args.py), [Backend routing tests](tests/basic/test_project_facts.py#L130) |
| F-010 | Aider caller evidence | Active | The `auto` caller backend uses Cymbal with a built-in fallback. | [CLI flags](aider/args.py) |
| F-011 | Aider caller selection | Active | `select_target_definitions` uses exact then sub-token matching without an unconditional fallback; zero-overlap prompts return `NO_EVIDENCE`. | [select_target_definitions](aider/local_callers.py#L435), [Precision gate tests](tests/basic/test_local_callers.py#L1115), [Oracle integration test](tests/basic/test_oracle_integration.py#L758) |
| F-012 | Aider caller filtering | Active | C and C++ definition extraction excludes `ALL_CAPS_MACRO` identifiers via `is_c_cpp_macro_name` in both builtin and Cymbal adapters. | [is_c_cpp_macro_name](aider/local_callers.py#L430), [Cymbal C/C++ extractor](aider/cymbal_callers.py#L2497), [Precision gate tests](tests/basic/test_local_callers.py#L1141) |
| F-013 | Aider caller discovery | Active | Builtin and Cymbal discover same-file callers (ranking cross-file production callers ahead of same-file callers) and Bash/shebang definitions, while marking ambiguous Rust trait-method receivers `INCOMPLETE`. | [local_callers](aider/local_callers.py), [cymbal_callers](aider/cymbal_callers.py), [Same-file & Bash tests](tests/basic/test_local_callers.py#L1169) |
| F-014 | Trial isolation | Active | `--no-default-model-settings` skips home and CWD `.aider.model.settings.yml` auto-discovery so trials pin hermetic model settings provenance. | [CLI flags](aider/args.py), [Main loader](aider/main.py), [Hermetic tests](tests/basic/test_local_context_trial.py) |

`FACTS.md` is the repository instruction workflow's memory surface. Aider's CLI reads its own JSONL or SQLite store.

## Research and provenance

The atomic entries apply the representation studied in [On the Structural Memory of LLM Agents](https://arxiv.org/abs/2412.15266). Scope, evidence, and supersession follow the coding-project memory design described in [Ontology-Grounded Project Memory for Coding Agents, §4](https://arxiv.org/html/2608.13662v1#S4). [Issue #5](https://github.com/pavlosuperhero/aider/issues/5) covers the Aider runtime store, [issue #11](https://github.com/pavlosuperhero/aider/issues/11) specifies confirmation for generated candidates, and [issue #13, Phase 4 and 4A](https://github.com/pavlosuperhero/aider/issues/13) connects facts with causal repair episodes.
