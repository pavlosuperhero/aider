"""Optional response hooks, run before Aider processes edits or commands.

A callable hook may also implement prompt_reminder(), returning literal text
to replace the normal edit reminder, or None to leave it unchanged. The first
override wins. Aider uses its existing reminder placement and token checks.

Test-result hooks (Coder(test_result_hooks=...)) observe each /test or auto-test
run, whether it passed or failed. A hook is called as hook(observation, io=io)
with a TestRunObservation and returns None (no change) or a ResponseHookResult
whose continuation text becomes the next reflected message. The first
continuation wins and it is counted against Coder.max_reflections, so there is
no extra loop. Continuations never change what /test returns or prints. If the
continuation turn edits files, the normal auto-test flow runs again; core keeps
no memory of earlier passing runs. Core imports nothing from hook providers.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ResponseHookResult:
    """Reject the current response; continue with a message, or stop with None.

    A hook returns None instead of this object to accept the response and let
    subsequent hooks run. Hooks receive content plus keyword-only token_count
    and io arguments. The first rejection ends dispatch for that response.
    """

    continuation: str | None = None

    def __post_init__(self):
        if self.continuation is not None and (
            not isinstance(self.continuation, str) or not self.continuation.strip()
        ):
            raise ValueError("A hook continuation must be nonempty text or None.")


MAX_TEST_OUTPUT_CHARS = 20_000


@dataclass(frozen=True)
class TestRunObservation:
    """Plain data describing one finished test command.

    output keeps the tail of the combined output (failures print last) and is
    at most MAX_TEST_OUTPUT_CHARS long; truncated says whether text was dropped.
    """

    __test__ = False  # not a pytest class

    command: str
    cwd: str
    exit_status: int
    output: str
    truncated: bool = False

    @classmethod
    def create(cls, command, cwd, exit_status, output, limit=MAX_TEST_OUTPUT_CHARS):
        output = output or ""
        truncated = len(output) > limit
        if truncated:
            output = output[-limit:]
        return cls(str(command), str(cwd), int(exit_status), output, truncated)


@dataclass(frozen=True)
class EditProposal:
    """Plain data describing the edits parsed from one model response.

    paths are project-relative, normalized and unique, in first-seen order. They
    are not checked for existence and may lie outside the project (a leading ..).
    shell_commands are the shell blocks the response suggested, if any.
    """

    paths: tuple[str, ...] = ()
    shell_commands: tuple[str, ...] = ()
