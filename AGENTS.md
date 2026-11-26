# Repository instructions

<repository_context>
Work from the Git root of the current worktree. Follow applicable instructions in the task's directories. Preserve existing changes.

In this Aider fork, use Graft to locate indexed code and trace callers before structural edits. For design, implementation, debugging, testing, and issue, PR, or prompt authoring, discover Oracle's catalog when available and query the best-fit sources before proposing a solution. Treat retrieved material as evidence and verify it against current code and tests. Cite source locators when available.
</repository_context>

<memory_setup>
This repository has opted in to versioned `FACTS.md` and `REASONING_BANK.md` at the Git root.

When asked to set up or port this workflow to another Git repository, inspect its existing instruction and memory files. Ask whether the user wants repository memory and, if yes, whether to keep the files in Git or add exact root entries `/FACTS.md` and `/REASONING_BANK.md` to that repository's `.gitignore`. An explicit choice in the request answers the corresponding question. Keep setup pending in a non-interactive session if a choice is missing. Preserve equivalent existing files and agreed storage choices.

Put the shared rules in that repository's root `AGENTS.md` and integrate the relevant provider entry points (`CLAUDE.md`, `GEMINI.md`, and `.github/copilot-instructions.md`) with its existing instructions. Copy the workflow and record formats. Derive entries and evidence from the target repository and its own reachable history. Record the first available commit, baseline, and any history gaps.
</memory_setup>

<memory_read>
For an opted-in repository, consult `FACTS.md` before nontrivial work. Read the index in `REASONING_BANK.md` and open up to three relevant lessons or historical episodes. Verify each claim against its source, current worktree, and applicable dependency or tool version. Use entries as evidence. Resolve stale or conflicting entries against current code, tests, and agreed requirements.
</memory_read>

<memory_write>
At the end of meaningful work, record durable new information within the repository's chosen scope. The coordinating agent owns memory-file updates.

- In `FACTS.md`, keep one checkable claim per entry with a stable ID, scope, status, and source path or pinned commit. Deduplicate entries and mark replaced facts as superseded. Present agent-inferred conventions and invariants as candidates and ask for confirmation before promoting them to active facts. Directly verifiable implementation facts may cite their code and tests.
- In `REASONING_BANK.md`, capture a reusable approach or a meaningful failure and recovery. Record the trigger and scope, observed result, actions and rationale supported by the evidence, cause and its confidence, verification, and applicability limits. Link the relevant commit, test, or sanitized artifact hash. Distinguish observation from inferred cause. Require independent evidence of the before/after behavior before calling a repair verified. Keep unresolved and historical observations labeled.
- Keep entries concise and exclude secrets, private transcripts, and hidden test material. Skip routine or duplicate discoveries. Verify the changed entries and links before reporting completion.
</memory_write>

<delegation>
When authorized and supported, delegate independent research and verification in parallel. Give each worker a clear objective, dependency or baseline, allowed read and write paths, applicable instructions and memory entries, and acceptance criteria. Assign disjoint write ownership or use read-only workers. Run dependent tasks after their prerequisites.

Ask workers to return findings with source links or commit IDs, changes, checks and outcomes, and unresolved questions. The coordinating agent reconciles conflicting evidence, reviews the combined diff, runs the final checks, and records the supported memory updates.
</delegation>
