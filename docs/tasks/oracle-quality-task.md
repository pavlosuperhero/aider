# Improve Oracle timeout diagnostics

## Objective

Make Oracle retrieval timeout failures more useful for a user to diagnose.
The current public error is too generic to explain what part of retrieval
stalled. Read the implementation and relevant dependency behavior before
choosing the smallest coherent improvement.

## Scope

Work in these files:

- `aider/oracle.py`
- `tests/basic/test_oracle.py`
- `docs/oracle.md`

Keep `OracleTimeoutError` usable as the public catch type. Preserve configured
timeout values, authentication handling, response/context budgets, source
selection, foundation policy, and fail-open/closed behavior. Keep diagnostics
free of credentials, request URLs and raw service exception text. This task
does not add retries, new configuration, or another retrieval mechanism.

## Work

1. Inspect the existing retrieval and error paths. Identify the information
   users currently lose and check the dependency contract needed for the fix.
   Follow the session's configured retrieval policy and decide which knowledge
   needs a focused refresh after reading the code.
2. Add focused regression tests using the existing injectable transport.
   Demonstrate the missing behavior before the fix, then implement it. Exercise
   the relevant timeout cases and a safe fallback for less-specific failures.
   Keep tests independent of live services, sleeps and network access.
3. Update the documented error contract to match the implemented behavior.
   Distinguish verified dependency behavior from any assumption or inference.
4. Run `.venv/bin/python -m pytest -q tests/basic/test_oracle.py
   tests/basic/test_oracle_integration.py` and report the actual result.

Leave the changes uncommitted. Keep the task instructions and evaluation
criteria unchanged, and limit production changes to the scope above.

## Final response

Return three concise items:

1. Root cause, behavior changed, and why the change belongs at that boundary.
2. Commands run and observed before/after results, including anything untested.
3. Documentation claims used in the decision, with actual returned passage
   IDs, origins and available locators when retrieval was available. Explain
   how each cited claim applies to this code. Label missing evidence honestly;
   do not invent citations or force unrelated book principles into the answer.
