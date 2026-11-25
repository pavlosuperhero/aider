from .architect_prompts import ArchitectPrompts
from .ask_coder import AskCoder
from .base_coder import Coder


class ArchitectCoder(AskCoder):
    edit_format = "architect"
    gpt_prompts = ArchitectPrompts()
    auto_accept_architect = False

    def _editor_outcome_message(self, editor_coder, edited_files):
        if not isinstance(edited_files, (set, frozenset, list, tuple)):
            return "I made those changes to the files."

        if edited_files:
            if (
                getattr(editor_coder, "dry_run_edited", False) is True
                or getattr(editor_coder, "dry_run", False) is True
            ):
                return "Dry run: no files were modified."
            if (
                getattr(editor_coder, "local_caller_auditor", None) is not None
                and not getattr(editor_coder, "caller_advisory", False)
                and getattr(editor_coder, "_audit_originals", None)
            ):
                return (
                    "I edited the files, but strict caller verification "
                    "left the changes uncommitted."
                )
            return "I made those changes to the files."

        contract = getattr(editor_coder, "local_caller_contract_hook", None)
        if contract is not None and getattr(contract, "blocked", False) is True:
            return (
                "No changes were made to the files because "
                "caller contract verification did not pass."
            )
        return "No changes were made to the files."

    def reply_completed(self):
        content = self.partial_response_content

        if not content or not content.strip():
            return

        if not self.auto_accept_architect and not self.io.confirm_ask("Edit the files?"):
            return

        kwargs = dict()

        # Use the editor_model from the main_model if it exists, otherwise use the main_model itself
        editor_model = self.main_model.editor_model or self.main_model

        kwargs["main_model"] = editor_model
        kwargs["edit_format"] = self.main_model.editor_edit_format
        kwargs["suggest_shell_commands"] = False
        kwargs["map_tokens"] = 0
        kwargs["total_cost"] = self.total_cost
        kwargs["cache_prompts"] = False
        kwargs["num_cache_warming_pings"] = 0
        kwargs["summarize_from_coder"] = False

        new_kwargs = dict(io=self.io, from_coder=self)
        new_kwargs.update(kwargs)

        editor_coder = Coder.create(**new_kwargs)
        editor_coder.cur_messages = []
        editor_coder.done_messages = []

        if self.verbose:
            editor_coder.show_announcements()

        handoff_appendix = self.handoff_local_caller_turn_state(editor_coder)
        editor_message = f"{content}{handoff_appendix}" if handoff_appendix else content
        editor_coder.run(with_message=editor_message, preproc=False)

        self.total_cost = editor_coder.total_cost
        self.aider_commit_hashes = editor_coder.aider_commit_hashes

        is_dry_run = (
            getattr(editor_coder, "dry_run_edited", False) is True
            or getattr(editor_coder, "dry_run", False) is True
        )
        edited_files = getattr(editor_coder, "aider_edited_files", None)
        if isinstance(edited_files, (set, frozenset, list, tuple)) and not is_dry_run:
            if isinstance(self.aider_edited_files, set):
                self.aider_edited_files.update(edited_files)
            elif edited_files:
                self.aider_edited_files = set(edited_files)

        unreviewed = getattr(editor_coder, "_unreviewed_paths", None)
        audit_originals = getattr(editor_coder, "_audit_originals", None)
        if isinstance(getattr(self, "_unreviewed_paths", None), dict):
            if isinstance(unreviewed, dict):
                self._unreviewed_paths.update(unreviewed)
            if isinstance(audit_originals, dict) and not is_dry_run:
                self._unreviewed_paths.update(audit_originals)

        outcome_message = self._editor_outcome_message(editor_coder, edited_files)
        self.move_back_cur_messages(outcome_message)
