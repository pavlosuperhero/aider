import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

TRIAL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "local_context_trial"
if str(TRIAL_DIR) not in sys.path:
    sys.path.insert(0, str(TRIAL_DIR))

import heldout  # noqa: E402
import run  # noqa: E402


class TestLocalContextTrialControls(unittest.TestCase):
    def test_resolve_arm_flags_preserves_existing_defaults(self):
        self.assertEqual(run.resolve_arm_flags("baseline"), ["--no-local-callers"])
        self.assertEqual(
            run.resolve_arm_flags("passive"),
            [
                "--local-callers",
                "--local-callers-mode",
                "passive",
                "--no-local-callers-audit",
            ],
        )
        self.assertEqual(run.resolve_arm_flags("context"), run.resolve_arm_flags("passive"))
        self.assertEqual(
            run.resolve_arm_flags("contract+audit"),
            [
                "--local-callers",
                "--local-callers-mode",
                "contract",
                "--local-callers-audit",
            ],
        )

    def test_resolve_arm_flags_independent_backend_range_and_facts(self):
        flags = run.resolve_arm_flags(
            "baseline",
            backend="graft",
            read_range=True,
        )
        self.assertEqual(
            flags,
            [
                "--no-local-callers",
                "--local-callers-read-range",
                "--local-callers-backend",
                "graft",
            ],
        )

        # Independently disable project facts while keeping passive --local-callers active
        flags_no_facts = run.resolve_arm_flags(
            "passive",
            backend="ast",
            read_range=False,
            facts=False,
            facts_file=".aider.custom.jsonl",
        )
        self.assertEqual(
            flags_no_facts,
            [
                "--local-callers",
                "--local-callers-mode",
                "passive",
                "--no-local-callers-audit",
                "--no-local-callers-read-range",
                "--local-callers-backend",
                "ast",
                "--no-facts",
                "--facts-file",
                ".aider.custom.jsonl",
            ],
        )

        # Independently enable project facts with SQLite DB and override --local-callers
        flags_with_facts = run.resolve_arm_flags(
            "baseline",
            facts=True,
            facts_db=".aider.facts.sqlite3",
            local_callers=True,
        )
        self.assertEqual(
            flags_with_facts,
            [
                "--local-callers",
                "--facts",
                "--facts-db",
                ".aider.facts.sqlite3",
            ],
        )

        flags_lc_off = run.resolve_arm_flags("passive", local_callers=False)
        self.assertEqual(flags_lc_off[0], "--no-local-callers")
        self.assertNotIn("--local-callers", flags_lc_off)

        tag = run.control_tag(
            backend="builtin",
            read_range=True,
            facts=True,
            facts_db=".aider.facts.sqlite3",
            facts_file="custom.jsonl",
            local_callers=False,
        )
        self.assertEqual(
            tag,
            "be-builtin-rr-on-facts-on-fdb-.aider.facts.sqlite3-ffile-custom.jsonl-lc-off",
        )

        with self.assertRaises(ValueError):
            run.resolve_arm_flags("passive", backend="   ")
        with self.assertRaises(ValueError):
            run.resolve_arm_flags("passive", facts_db="  ")
        with self.assertRaises(ValueError):
            run.resolve_arm_flags("passive", facts_file="  ")
        with self.assertRaises(ValueError):
            run.resolve_arm_flags("unknown-arm")

    def test_heldout_control_grid_defaults_and_factorial(self):
        parser = heldout.build_arg_parser()
        default_args = parser.parse_args([])
        self.assertEqual(heldout.resolve_control_grid(default_args, parser), [(None, None, None)])

        grid_args = parser.parse_args(
            [
                "--local-callers-backend",
                "graft,ast",
                "--read-range-modes",
                "off,on",
                "--facts-modes",
                "off,on",
            ]
        )
        grid = heldout.resolve_control_grid(grid_args, parser)
        self.assertEqual(len(grid), 8)
        self.assertIn(("graft", False, False), grid)
        self.assertIn(("ast", True, True), grid)

        single_args = parser.parse_args(
            [
                "--local-callers-backend",
                "graft",
                "--local-callers-read-range",
                "--no-facts",
                "--facts-db",
                ".aider.facts.sqlite3",
                "--facts-file",
                "custom.jsonl",
                "--no-local-callers-override",
            ]
        )
        self.assertEqual(
            heldout.resolve_control_grid(single_args, parser),
            [("graft", True, False)],
        )
        self.assertEqual(single_args.facts_db, ".aider.facts.sqlite3")
        self.assertEqual(single_args.facts_file, "custom.jsonl")
        self.assertIs(single_args.local_callers_override, False)

        # Backwards-compatible alias --no-local-callers-facts maps to local_callers_override
        legacy_lc_args = parser.parse_args(["--facts", "--no-local-callers-facts"])
        self.assertEqual(heldout.resolve_control_grid(legacy_lc_args, parser), [(None, None, True)])
        self.assertIs(legacy_lc_args.local_callers_override, False)


class TestTrialUsageAndOutcomes(unittest.TestCase):
    def test_editor_usage_round_trip_includes_resends(self):
        from aider.coders.base_coder import Coder

        coder = mock.Mock(total_tokens_sent=0, total_tokens_received=0, usage_report="Usage")
        with mock.patch.dict(os.environ, AIDER_TRIAL_USAGE="1"):
            for sent, received in [(640, 45), (810, 28)]:
                coder.message_tokens_sent = sent
                coder.message_tokens_received = received
                Coder.show_usage_report(coder)
        log = "\n".join(call.args[0] for call in coder.io.tool_output.call_args_list)
        usage = run.parse_trial_usage(log)
        self.assertEqual(usage["first_editor_prompt_tokens"], 640)
        self.assertEqual(usage["prompt_tokens"], 1450)
        self.assertEqual(usage["completion_tokens"], 73)

    def test_parse_trial_usage_measured_and_unmeasured(self):
        classic = "\n".join(
            [
                "Local caller evidence: SELECTED",
                "Trial usage: editor 1200",
                "Trial usage: audit 350",
            ]
        )
        parsed = run.parse_trial_usage(classic)
        self.assertTrue(parsed["measured"])
        self.assertEqual(parsed["tokens"], 1550)
        self.assertEqual(parsed["editor_tokens"], 1200)
        self.assertEqual(parsed["audit_tokens"], 350)
        self.assertEqual(parsed["editor_calls"], 1)
        self.assertEqual(parsed["audit_calls"], 1)
        self.assertEqual(parsed["extra_calls"], 1)

        detailed = "\n".join(
            [
                "Trial usage: editor prompt=900 completion=150 total=1050",
                "Trial usage: audit prompt=200 completion=50",
            ]
        )
        parsed_detailed = run.parse_trial_usage(detailed)
        self.assertTrue(parsed_detailed["measured"])
        self.assertEqual(parsed_detailed["tokens"], 1300)
        self.assertIsNone(parsed["first_editor_prompt_tokens"])
        self.assertEqual(parsed_detailed["first_editor_prompt_tokens"], 900)
        self.assertEqual(parsed_detailed["prompt_tokens"], 1100)
        self.assertEqual(parsed_detailed["completion_tokens"], 200)

        partial = "Trial usage: editor 800\nTrial usage: audit None\n"
        parsed_partial = run.parse_trial_usage(partial)
        self.assertFalse(parsed_partial["measured"])
        self.assertIsNone(parsed_partial["tokens"])
        self.assertEqual(parsed_partial["editor_tokens"], 800)
        self.assertIsNone(parsed_partial["audit_tokens"])

    def _run_spec_with_fake_aider(
        self,
        *,
        case_spec: run.CaseSpec,
        arm: str,
        log_content: str,
        edit_fn=None,
        exit_code: int = 0,
        backend: str | None = None,
        read_range: bool | None = None,
        facts: bool | None = None,
        facts_db: str | None = None,
        facts_file: str | None = None,
        local_callers: bool | None = None,
    ) -> tuple[dict, list[str]]:
        captured_cmd: list[str] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and any(
                "runpy.run_module('aider'" in str(part) for part in cmd
            ):
                captured_cmd.extend(cmd)
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write(log_content)
                if edit_fn is not None:
                    edit_fn(Path(kwargs["cwd"]))
                return subprocess.CompletedProcess(cmd, exit_code)
            return real_run(cmd, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="aider-trial-test-") as tmp:
            output = Path(tmp)
            with mock.patch.object(run.subprocess, "run", side_effect=fake_run):
                result = run.run_spec(
                    output=output,
                    case_spec=case_spec,
                    arm=arm,
                    round_id=0,
                    map_tokens=512,
                    spec="vague",
                    backend=backend,
                    read_range=read_range,
                    facts=facts,
                    facts_db=facts_db,
                    facts_file=facts_file,
                    local_callers=local_callers,
                    metadata={"schema_version": 2, "run_id": "testrun", "git_commit": "abc1234567"},
                )
        return result, captured_cmd

    def test_run_spec_separates_audit_commit_and_hidden_outcomes(self):
        case_spec = heldout.CASE_BY_NAME["duration"]

        # 1. Audit PASS, clean edit applied, hidden PASS
        def fix_duration(repo: Path):
            (repo / "api.py").write_text(
                "def parse_duration(text):\n"
                "    if text.endswith('m'):\n"
                "        return int(text[:-1]) * 60\n"
                "    return int(text)\n"
            )

        ok_res, cmd = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="audit",
            log_content=(
                "required foundations loaded\n"
                "Local caller evidence: SELECTED\n"
                "Local caller audit: PASS\n"
                "Trial usage: editor 1000\n"
                "Trial usage: audit 250\n"
            ),
            edit_fn=fix_duration,
            backend="graft",
            read_range=True,
        )
        self.assertIn("--local-callers-backend", cmd)
        self.assertIn("graft", cmd)
        self.assertIn("--local-callers-read-range", cmd)
        self.assertEqual(ok_res["audit_outcome"], "pass")
        self.assertIs(ok_res["audit_passed"], True)
        self.assertEqual(ok_res["commit_outcome"], "applied")
        self.assertIs(ok_res["commit_passed"], False)
        self.assertEqual(ok_res["commits_added"], 0)
        self.assertEqual(ok_res["hidden_outcome"], "pass")
        self.assertIs(ok_res["hidden_passed"], True)
        self.assertIs(ok_res["smoke_passed"], True)
        self.assertIs(ok_res["passed"], True)
        self.assertIs(ok_res["patch_hidden_passed"], True)
        self.assertIs(ok_res["strict_accepted"], True)
        self.assertIs(ok_res["committed"], False)
        self.assertIs(ok_res["false_approval"], False)
        self.assertEqual(ok_res["tokens"], 1250)
        self.assertIs(ok_res["tokens_measured"], True)
        self.assertEqual(ok_res["editor_tokens"], 1000)
        self.assertEqual(ok_res["audit_tokens"], 250)
        self.assertEqual(ok_res["local_callers_backend"], "graft")
        self.assertIs(ok_res["local_callers_read_range"], True)
        self.assertIs(ok_res["local_callers_enabled"], True)
        self.assertIs(ok_res["local_callers_facts"], True)
        self.assertIsNone(ok_res["project_facts"])
        self.assertEqual(ok_res["facts_backend"], "off")
        self.assertEqual(ok_res["metadata"]["git_commit"], "abc1234567")

        # 2. Audit PASS, clean edit applied, smoke PASS, hidden FAIL -> false_approval
        def wrong_unit(repo: Path):
            (repo / "api.py").write_text(
                "def parse_duration(text):\n"
                "    if text.endswith('m'):\n"
                "        return int(text[:-1])\n"
                "    return int(text)\n"
            )

        bad_hidden, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="audit",
            log_content=(
                "required foundations loaded\n"
                "Local caller evidence: SELECTED\n"
                "Local caller audit: PASS\n"
                "Trial usage: editor 900\n"
                "Trial usage: audit 200\n"
            ),
            edit_fn=wrong_unit,
        )
        self.assertEqual(bad_hidden["audit_outcome"], "pass")
        self.assertIs(bad_hidden["audit_passed"], True)
        self.assertEqual(bad_hidden["commit_outcome"], "applied")
        self.assertIs(bad_hidden["commit_passed"], False)
        self.assertIs(bad_hidden["smoke_passed"], True)
        self.assertIs(bad_hidden["hidden_passed"], False)
        self.assertIs(bad_hidden["passed"], False)
        self.assertIs(bad_hidden["false_approval"], True)
        self.assertIs(bad_hidden["patch_hidden_passed"], False)
        self.assertIs(bad_hidden["strict_accepted"], False)

        # 3. Audit FAIL, no repo edit -> commit_outcome=no_change
        no_edit, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="audit",
            log_content=(
                "required foundations loaded\n"
                "Local caller evidence: SELECTED\n"
                "Local caller audit: FAIL\n"
                "Trial usage: editor 700\n"
                "Trial usage: audit 180\n"
            ),
            edit_fn=None,
        )
        self.assertEqual(no_edit["audit_outcome"], "fail")
        self.assertIs(no_edit["audit_passed"], False)
        self.assertEqual(no_edit["commit_outcome"], "no_change")
        self.assertIs(no_edit["commit_passed"], False)
        self.assertIs(no_edit["hidden_passed"], False)
        self.assertIs(no_edit["passed"], False)

        # 4. Out-of-scope file touched while hidden test passes -> separate outcomes!
        def out_of_scope_fix(repo: Path):
            fix_duration(repo)
            (repo / "extra.py").write_text("# stray file\n")

        oos_res, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="passive",
            log_content=(
                "required foundations loaded\n"
                "Local caller evidence: SELECTED\n"
                "Trial usage: editor 850\n"
            ),
            edit_fn=out_of_scope_fix,
        )
        self.assertEqual(oos_res["audit_outcome"], "not_run")
        self.assertIsNone(oos_res["audit_passed"])
        self.assertEqual(oos_res["commit_outcome"], "out_of_scope")
        self.assertIs(oos_res["commit_passed"], False)
        self.assertIs(oos_res["hidden_passed"], True)
        self.assertIs(oos_res["passed"], False)
        self.assertIs(oos_res["patch_hidden_passed"], False)

        blocked_res, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="contract+audit",
            log_content=(
                "Local caller contract check: NO_EVIDENCE (pass-through)\n"
                "Local caller audit: UNKNOWN\n"
                "Trial usage: editor 850\n"
            ),
            edit_fn=fix_duration,
        )
        self.assertIs(blocked_res["contract_rejected"], False)
        self.assertIs(blocked_res["patch_hidden_passed"], True)
        self.assertIs(blocked_res["strict_accepted"], False)
        self.assertIs(blocked_res["committed"], False)

        rejected_res, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="contract",
            log_content=(
                "Local caller contract check: BLOCKED (enforcement exhausted)\n"
                "Trial usage: editor 850\n"
            ),
        )
        self.assertIs(rejected_res["contract_rejected"], True)

        telemetry = heldout.build_summary([ok_res, blocked_res])["arm_outcomes"]
        self.assertEqual(telemetry["audit"]["strict_accepted"], 1)
        self.assertEqual(telemetry["contract+audit"]["strict_accepted"], 0)
        self.assertEqual(telemetry["contract+audit"]["patch_hidden_passed"], 1)

    def test_p2_4_applied_vs_committed_outcomes(self):
        case_spec = heldout.CASE_BY_NAME["duration"]

        # Direct unit check of classify_commit_outcome
        self.assertEqual(
            run.classify_commit_outcome(0, [" M api.py"], "diff", True, commits_added=0),
            ("applied", False),
        )
        self.assertEqual(
            run.classify_commit_outcome(0, [], "diff", True, commits_added=1),
            ("committed", True),
        )

        # Applied edit with commits_added == 0 -> commit_outcome=="applied", commit_passed is False
        def apply_only(repo: Path):
            (repo / "api.py").write_text(
                "def parse_duration(text):\n"
                "    if text.endswith('m'):\n"
                "        return int(text[:-1]) * 60\n"
                "    return int(text)\n"
            )

        applied_res, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="passive",
            log_content="Trial usage: editor 500\n",
            edit_fn=apply_only,
        )
        self.assertEqual(applied_res["commit_outcome"], "applied")
        self.assertEqual(applied_res["commits_added"], 0)
        self.assertIs(applied_res["commit_passed"], False)
        self.assertIs(applied_res["passed"], True)

        # Real git commit with commits_added == 1 -> commit_outcome=="committed", commit_passed True
        def apply_and_commit(repo: Path):
            apply_only(repo)
            subprocess.run(["git", "add", "api.py"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Trial",
                    "-c",
                    "user.email=trial@localhost",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-qm",
                    "fix duration",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )

        committed_res, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="passive",
            log_content="Trial usage: editor 500\n",
            edit_fn=apply_and_commit,
        )
        self.assertEqual(committed_res["commit_outcome"], "committed")
        self.assertEqual(committed_res["commits_added"], 1)
        self.assertIs(committed_res["commit_passed"], True)
        self.assertIs(committed_res["committed"], True)
        self.assertIs(committed_res["only_target_modified"], True)
        self.assertIs(committed_res["passed"], True)

        def commit_unrelated_and_apply_target(repo: Path):
            (repo / "consumer.py").write_text(
                (repo / "consumer.py").read_text() + "\n# unrelated edit\n"
            )
            subprocess.run(
                ["git", "add", "consumer.py"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Trial",
                    "-c",
                    "user.email=trial@localhost",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-qm",
                    "unrelated change",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            apply_only(repo)

        mixed, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="audit",
            log_content="Local caller audit: PASS\nTrial usage: editor 500\n",
            edit_fn=commit_unrelated_and_apply_target,
        )
        self.assertIs(mixed["committed"], True)
        self.assertIs(mixed["only_target_modified"], False)
        self.assertIs(mixed["strict_accepted"], False)
        self.assertIs(mixed["false_approval"], True)

        # heldout._extract_row_outcomes derives commit_passed from commits_added / commit_outcome
        self.assertIs(
            heldout._extract_row_outcomes(
                {"commit_outcome": "applied", "commit_passed": True, "commits_added": 0}
            )["commit_passed"],
            False,
        )
        self.assertIs(
            heldout._extract_row_outcomes(
                {"commit_outcome": "applied", "commit_passed": True}
            )["commit_passed"],
            False,
        )
        self.assertIs(
            heldout._extract_row_outcomes(
                {"commit_outcome": "committed", "commit_passed": False, "commits_added": 1}
            )["commit_passed"],
            True,
        )
        self.assertIs(
            heldout._extract_row_outcomes({"commit_outcome": "committed"})["commit_passed"],
            True,
        )

    def test_p2_5_missing_completion_recorded_as_error_without_raising(self):
        case_spec = heldout.CASE_BY_NAME["duration"]
        res, _ = self._run_spec_with_fake_aider(
            case_spec=case_spec,
            arm="passive",
            log_content="APIError: provider disconnected before completion\n",
            edit_fn=None,
            exit_code=1,
        )
        self.assertEqual(res["exit"], 1)
        self.assertEqual(res["editor_calls"], 0)
        self.assertEqual(res["commit_outcome"], "error")
        self.assertIs(res["commit_passed"], False)
        self.assertEqual(res["trial_error"], "No editor completion recorded")
        self.assertIs(res["passed"], False)

    def test_build_error_row_preserves_schema_and_controls(self):
        case_spec = heldout.CASE_BY_NAME["duration"]
        row = heldout._build_error_row(
            case_spec=case_spec,
            arm="passive",
            round_id=2,
            spec="explicit",
            model="test-model",
            map_tokens=512,
            backend="cymbal",
            read_range=True,
            facts=True,
            facts_db=".aider.facts.sqlite3",
            facts_file=None,
            local_callers_override=False,
            exc=RuntimeError("boom"),
            elapsed_seconds=1.24,
            workdir=Path("/tmp/work"),
            metadata={"seed": 7},
        )
        self.assertEqual(row["schema_version"], run.SCHEMA_VERSION)
        self.assertEqual(row["case"], "duration")
        self.assertEqual(row["arm"], "passive")
        self.assertEqual(row["round"], 2)
        self.assertIs(row["local_callers_enabled"], False)
        self.assertIs(row["local_callers_facts"], False)
        self.assertEqual(row["facts_backend"], "sqlite")
        self.assertEqual(row["exit"], -1)
        self.assertEqual(row["trial_error"], "RuntimeError: boom")
        self.assertEqual(row["commit_outcome"], "error")
        self.assertIs(row["commit_passed"], False)
        self.assertEqual(row["hidden_outcome"], "error")
        self.assertIs(row["hidden_passed"], False)
        self.assertEqual(row["smoke_outcome"], "error")
        self.assertIs(row["smoke_passed"], False)
        self.assertEqual(row["audit_outcome"], "not_run")
        self.assertIsNone(row["audit_passed"])
        self.assertIs(row["passed"], False)
        self.assertEqual(row["seconds"], 1.2)
        self.assertEqual(row["workdir"], "/tmp/work")
        self.assertEqual(row["metadata"], {"seed": 7})

    def test_p2_5_heldout_main_records_error_row_and_continues_after_exception(self):
        with tempfile.TemporaryDirectory(prefix="aider-heldout-resilient-") as tmp:
            out_path = Path(tmp) / "results.jsonl"
            seen_cases: list[str] = []

            def fake_run_spec(**kwargs):
                case_spec = kwargs["case_spec"]
                seen_cases.append(case_spec.name)
                if len(seen_cases) == 1:
                    raise ConnectionError("LM Studio socket reset")
                return {
                    "schema_version": run.SCHEMA_VERSION,
                    "model": kwargs["model"],
                    "round": kwargs["round_id"],
                    "spec": kwargs["spec"],
                    "kind": case_spec.kind,
                    "language": case_spec.language,
                    "case": case_spec.name,
                    "arm": kwargs["arm"],
                    "local_callers_backend": kwargs["backend"],
                    "local_callers_read_range": kwargs["read_range"],
                    "local_callers_enabled": False,
                    "local_callers_facts": False,
                    "project_facts": kwargs["facts"],
                    "facts_backend": "off",
                    "exit": 0,
                    "trial_error": None,
                    "evidence": False,
                    "audit_ran": False,
                    "audit_outcome": "not_run",
                    "audit_passed": None,
                    "commit_outcome": "applied",
                    "commit_passed": False,
                    "commits_added": 0,
                    "smoke_outcome": "pass",
                    "smoke_passed": True,
                    "hidden_outcome": "pass",
                    "hidden_passed": True,
                    "tokens": 500,
                    "tokens_measured": True,
                    "passed": True,
                    "seconds": 2.0,
                    "patch": "",
                    "metadata": dict(kwargs["metadata"]),
                }

            buf = io.StringIO()
            with mock.patch.object(heldout.run, "run_spec", side_effect=fake_run_spec):
                with contextlib.redirect_stdout(buf):
                    heldout.main(
                        [
                            "--cases",
                            "duration,names",
                            "--arms",
                            "baseline",
                            "--specs",
                            "vague",
                            "--rounds",
                            "1",
                            "--seed",
                            "1",
                            "--out",
                            str(out_path),
                        ]
                    )

            self.assertEqual(len(seen_cases), 2)
            recorded = heldout.load_results(out_path)
            self.assertEqual(len(recorded), 2)

            err_row, ok_row = recorded[0], recorded[1]
            self.assertEqual(err_row["case"], seen_cases[0])
            self.assertEqual(err_row["arm"], "baseline")
            self.assertEqual(err_row["round"], 0)
            self.assertEqual(err_row["spec"], "vague")
            self.assertEqual(err_row["exit"], -1)
            self.assertEqual(err_row["trial_error"], "ConnectionError: LM Studio socket reset")
            self.assertEqual(err_row["commit_outcome"], "error")
            self.assertIs(err_row["commit_passed"], False)
            self.assertEqual(err_row["hidden_outcome"], "error")
            self.assertIs(err_row["hidden_passed"], False)
            self.assertEqual(err_row["smoke_outcome"], "error")
            self.assertIs(err_row["smoke_passed"], False)
            self.assertEqual(err_row["audit_outcome"], "not_run")
            self.assertIsNone(err_row["audit_passed"])
            self.assertIs(err_row["passed"], False)
            self.assertIsInstance(err_row["seconds"], float)
            self.assertEqual(err_row["metadata"]["seed"], 1)

            self.assertEqual(ok_row["case"], seen_cases[1])
            self.assertIs(ok_row["passed"], True)
            self.assertIsNone(ok_row["trial_error"])

    def test_heldout_checkpoint_recovers_partial_tail_and_keeps_completed_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "results.jsonl"
            args = [
                "--checkpoint", "--out", str(out), "--cases", "duration", "--arms", "baseline",
                "--specs", "vague", "--rounds", "3",
            ]

            def fake_attempt(**kwargs):
                return {"round": kwargs["round_id"], "metadata": kwargs["metadata"]}

            def interrupted_attempt(**kwargs):
                if out.exists():
                    with out.open("ab") as stream:
                        stream.write(b'{"checkpoint_job":')
                    raise KeyboardInterrupt
                return fake_attempt(**kwargs)

            with mock.patch.object(heldout.run, "run_spec", side_effect=interrupted_attempt):
                with self.assertRaises(KeyboardInterrupt):
                    heldout.main(args)
            saved = out.read_bytes().splitlines()[0]
            run_id = json.loads(saved)["metadata"]["run_id"]

            with (
                mock.patch.object(heldout.run, "run_spec", side_effect=fake_attempt) as attempts,
                mock.patch.object(heldout, "summarize"),
            ):
                with self.assertRaises(SystemExit):
                    heldout.main([*args, "--seed", "42"])
                attempts.assert_not_called()

                heldout.main(args)
                self.assertEqual(attempts.call_count, 2)
                heldout.main(args)
                self.assertEqual(attempts.call_count, 2)

            self.assertEqual(out.read_bytes().splitlines()[0], saved)
            rows = heldout.load_results(out)
            self.assertEqual([row["checkpoint_job"] for row in rows], [0, 1, 2])
            self.assertEqual({row["round"] for row in rows}, {0, 1, 2})
            self.assertEqual({row["metadata"]["run_id"] for row in rows}, {run_id})

    def test_run_spec_complete_within_scope_and_project_facts_fields(self):
        import issue5_cases

        stale_case = next(c for c in issue5_cases.cases() if c.name == "facts_stale_control")

        def fix_tax(repo: Path):
            self.assertIn("TAX_POLICY_VERSION = 2", (repo / "policy.py").read_text())
            self.assertTrue((repo / ".aider.facts.sqlite3").exists())
            (repo / "api.py").write_text(
                "def compute_tax(amount_cents, rate_percent=10):\n"
                "    if amount_cents <= 0 or rate_percent <= 0:\n"
                "        return 0\n"
                "    return (amount_cents * rate_percent + 50) // 100\n"
            )

        res, cmd = self._run_spec_with_fake_aider(
            case_spec=stale_case,
            arm="passive",
            log_content=(
                "required foundations loaded\n"
                "Local caller evidence: COMPLETE_WITHIN_SCOPE\n"
                "Trial usage: editor prompt=800 completion=120 total=920\n"
            ),
            edit_fn=fix_tax,
            facts=True,
            facts_db=".aider.facts.sqlite3",
        )
        self.assertIn("--facts", cmd)
        self.assertIn("--facts-db", cmd)
        self.assertIn(".aider.facts.sqlite3", cmd)
        self.assertIs(res["evidence"], True)
        self.assertIs(res["project_facts"], True)
        self.assertEqual(res["facts_backend"], "sqlite")
        self.assertEqual(res["facts_db"], ".aider.facts.sqlite3")
        self.assertIsNone(res["facts_file"])
        self.assertIs(res["local_callers_enabled"], True)
        self.assertIs(res["only_target_modified"], True)
        self.assertIs(res["passed"], True)

        jsonl_res, jsonl_cmd = self._run_spec_with_fake_aider(
            case_spec=heldout.CASE_BY_NAME["duration"],
            arm="passive",
            log_content=(
                "required foundations loaded\n"
                "Local caller evidence: SELECTED\n"
                "Trial usage: editor 500\n"
            ),
            facts=True,
            facts_file=".aider.custom.jsonl",
        )
        self.assertIn("--facts", jsonl_cmd)
        self.assertIn("--facts-file", jsonl_cmd)
        self.assertEqual(jsonl_res["facts_backend"], "jsonl")
        self.assertEqual(jsonl_res["facts_file"], ".aider.custom.jsonl")

    def test_p0_2_and_p0_3_run_spec_disables_auto_lint_and_pins_pythonhashseed(self):
        case_spec = heldout.CASE_BY_NAME["duration"]
        captured_cmd: list[str] = []
        captured_env: dict[str, str] = {}
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and any(
                "runpy.run_module('aider'" in str(part) for part in cmd
            ):
                captured_cmd.extend(cmd)
                captured_env.update(kwargs.get("env") or {})
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write("required foundations loaded\nTrial usage: editor 500\n")
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="aider-p0-harness-") as tmp:
            output = Path(tmp)
            with mock.patch.dict(os.environ, {"PYTHONHASHSEED": "random"}):
                with mock.patch.object(run.subprocess, "run", side_effect=fake_run):
                    run.run_spec(
                        output=output,
                        case_spec=case_spec,
                        arm="baseline",
                        round_id=0,
                        map_tokens=512,
                        spec="vague",
                        metadata={"schema_version": 2, "run_id": "p0check"},
                    )

        self.assertIn("--no-auto-lint", captured_cmd)
        self.assertIn("--no-auto-add-file-mentions", captured_cmd)
        self.assertIn("--no-auto-commits", captured_cmd)
        self.assertIn("--no-dirty-commits", captured_cmd)
        self.assertNotIn("--auto-lint", captured_cmd)
        self.assertNotIn("--thinking-tokens", captured_cmd)
        self.assertEqual(captured_env.get("PYTHONHASHSEED"), "0")
        self.assertEqual(captured_env.get("AIDER_ANALYTICS"), "false")
        self.assertEqual(captured_env.get("AIDER_TRIAL_USAGE"), "1")

        # Opt-in --auto-lint and --thinking-tokens are forwarded cleanly
        optin_cmd: list[str] = []
        optin_env: dict[str, str] = {}

        def fake_run_optin(cmd, *args, **kwargs):
            if isinstance(cmd, list) and any(
                "runpy.run_module('aider'" in str(part) for part in cmd
            ):
                optin_cmd.extend(cmd)
                optin_env.update(kwargs.get("env") or {})
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write("required foundations loaded\nTrial usage: editor 500\n")
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="aider-p0-optin-") as tmp:
            output = Path(tmp)
            with mock.patch.object(run.subprocess, "run", side_effect=fake_run_optin):
                run.run_spec(
                    output=output,
                    case_spec=case_spec,
                    arm="passive",
                    round_id=0,
                    map_tokens=512,
                    spec="vague",
                    auto_lint=True,
                    thinking_tokens="4096",
                    metadata={"schema_version": 2, "run_id": "p0optin"},
                )

        self.assertIn("--auto-lint", optin_cmd)
        self.assertNotIn("--no-auto-lint", optin_cmd)
        idx = optin_cmd.index("--thinking-tokens")
        self.assertEqual(optin_cmd[idx + 1], "4096")
        self.assertEqual(optin_env.get("PYTHONHASHSEED"), "0")

        # Precondition checks fail fast on blank values
        with self.assertRaises(ValueError):
            run._resolve_harness_flags(thinking_tokens="   ")
        with self.assertRaises(ValueError):
            run._build_trial_env(pythonhashseed="  ")

        # Metadata records deterministic harness defaults and heldout CLI exposes opt-in controls
        meta = run.collect_run_metadata(suite="synthetic", seed=1, rounds=1)
        self.assertIs(meta["auto_lint"], False)
        self.assertIs(meta["auto_add_file_mentions"], False)
        self.assertIs(meta["auto_commits"], False)
        self.assertIs(meta["dirty_commits"], False)
        self.assertEqual(meta["pythonhashseed"], "0")
        self.assertIsNone(meta["thinking_tokens"])

        parser = heldout.build_arg_parser()
        default_ns = parser.parse_args([])
        self.assertIs(default_ns.auto_lint, False)
        self.assertIsNone(default_ns.thinking_tokens)

        optin_ns = parser.parse_args(["--auto-lint", "--thinking-tokens", "8192"])
        self.assertIs(optin_ns.auto_lint, True)
        self.assertEqual(optin_ns.thinking_tokens, "8192")


class TestReportingAndPairedIntervals(unittest.TestCase):
    def test_paired_proportion_diff_ci_properties(self):
        self.assertEqual(heldout.paired_proportion_diff_ci(0, 0, 0), (None, None, None))

        delta_zero, lo_zero, hi_zero = heldout.paired_proportion_diff_ci(0, 0, 10)
        self.assertEqual(delta_zero, 0.0)
        self.assertLess(lo_zero, 0.0)
        self.assertGreater(hi_zero, 0.0)
        self.assertAlmostEqual(lo_zero, -hi_zero, places=10)

        delta_win, lo_win, hi_win = heldout.paired_proportion_diff_ci(8, 0, 10)
        self.assertAlmostEqual(delta_win, 0.8)
        self.assertGreater(lo_win, 0.0)
        self.assertLessEqual(hi_win, 1.0)
        self.assertTrue(lo_win <= delta_win <= hi_win)

        # Antisymmetry under swapping wins and losses
        delta_rev, lo_rev, hi_rev = heldout.paired_proportion_diff_ci(0, 8, 10)
        self.assertAlmostEqual(delta_rev, -delta_win, places=10)
        self.assertAlmostEqual(lo_rev, -hi_win, places=10)
        self.assertAlmostEqual(hi_rev, -lo_win, places=10)

    def test_paired_mean_diff_ci_properties(self):
        self.assertEqual(heldout.paired_mean_diff_ci([]), (None, None, None))
        self.assertEqual(heldout.paired_mean_diff_ci([42.0]), (42.0, None, None))

        mean, lo, hi = heldout.paired_mean_diff_ci([10.0, 20.0, 30.0, 40.0])
        self.assertAlmostEqual(mean, 25.0)
        self.assertAlmostEqual(lo, 4.4574, places=3)
        self.assertAlmostEqual(hi, 45.5426, places=3)

        # Constant diffs give zero-width interval at the mean
        c_mean, c_lo, c_hi = heldout.paired_mean_diff_ci([100.0, 100.0, 100.0])
        self.assertEqual((c_mean, c_lo, c_hi), (100.0, 100.0, 100.0))

        # Large df > 30 uses Cornish-Fisher approximation smoothly
        large_mean, large_lo, large_hi = heldout.paired_mean_diff_ci([float(i) for i in range(40)])
        self.assertAlmostEqual(large_mean, 19.5)
        self.assertLess(large_lo, large_mean)
        self.assertGreater(large_hi, large_mean)

    def test_legacy_jsonl_compatibility_in_summarize(self):
        legacy_rows = [
            {
                "round": 0,
                "spec": "vague",
                "kind": "caller",
                "case": "duration",
                "arm": "baseline",
                "passed": False,
                "evidence": False,
                "seconds": 5.0,
            },
            {
                "round": 0,
                "spec": "vague",
                "kind": "caller",
                "case": "duration",
                "arm": "context",
                "passed": True,
                "evidence": True,
                "seconds": 6.5,
            },
            {
                "round": 1,
                "spec": "vague",
                "kind": "caller",
                "case": "duration",
                "arm": "baseline",
                "passed": False,
                "evidence": False,
                "seconds": 4.5,
            },
            {
                "round": 1,
                "spec": "vague",
                "kind": "caller",
                "case": "duration",
                "arm": "context",
                "passed": True,
                "evidence": True,
                "seconds": 6.0,
            },
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            summary = heldout.summarize(legacy_rows)
        output = buf.getvalue()

        self.assertEqual(summary["arms"], ["baseline", "passive"])
        self.assertEqual(summary["arm_outcomes"]["passive"]["passed"], 2)
        self.assertEqual(summary["arm_outcomes"]["passive"]["hidden_recorded"], 0)
        self.assertEqual(summary["arm_outcomes"]["passive"]["commit_recorded"], 0)
        self.assertEqual(summary["arm_outcomes"]["passive"]["tokens_measured_n"], 0)
        self.assertIn("hidden=n/a", output)
        self.assertIn("commit=n/a", output)
        self.assertIn("detailed telemetry incomplete", output)
        self.assertIn("passive vs baseline -> +2/-0 (n=2, delta=+1.000, 95% CI", output)

    def test_new_schema_summary_separate_outcomes_tokens_and_factorial_controls(self):
        rows = []
        for r in range(3):
            rows.append(
                {
                    "schema_version": 2,
                    "model": "test-model",
                    "round": r,
                    "spec": "vague",
                    "kind": "caller",
                    "language": "python",
                    "case": "duration",
                    "arm": "passive",
                    "local_callers_backend": "graft",
                    "local_callers_read_range": False,
                    "local_callers_facts": True,
                    "evidence": True,
                    "audit_ran": True,
                    "audit_outcome": "pass",
                    "audit_passed": True,
                    "audit_failed": False,
                    "commit_outcome": "applied",
                    "commit_passed": False,
                    "commits_added": 0,
                    "hidden_outcome": "fail",
                    "hidden_passed": False,
                    "smoke_passed": True,
                    "passed": False,
                    "false_approval": True,
                    "tokens": 1000 + r * 10,
                    "tokens_measured": True,
                    "editor_tokens": 800 + r * 10,
                    "audit_tokens": 200,
                    "seconds": 3.0,
                    "metadata": {"git_commit": "deadbeef1234", "suite": "synthetic", "seed": 42},
                }
            )
            rows.append(
                {
                    "schema_version": 2,
                    "model": "test-model",
                    "round": r,
                    "spec": "vague",
                    "kind": "caller",
                    "language": "python",
                    "case": "duration",
                    "arm": "passive",
                    "local_callers_backend": "graft",
                    "local_callers_read_range": True,
                    "local_callers_facts": True,
                    "evidence": True,
                    "audit_ran": True,
                    "audit_outcome": "pass",
                    "audit_passed": True,
                    "audit_failed": False,
                    "commit_outcome": "applied",
                    "commit_passed": False,
                    "commits_added": 0,
                    "hidden_outcome": "pass",
                    "hidden_passed": True,
                    "smoke_passed": True,
                    "passed": True,
                    "false_approval": False,
                    "tokens": 1300 + r * 20,
                    "tokens_measured": True,
                    "editor_tokens": 1050 + r * 20,
                    "audit_tokens": 250,
                    "seconds": 4.0,
                    "metadata": {"git_commit": "deadbeef1234", "suite": "synthetic", "seed": 42},
                }
            )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            summary = heldout.summarize(rows)
        output = buf.getvalue()

        self.assertEqual(summary["arms"], ["passive[range=off]", "passive[range=on]"])
        self.assertEqual(summary["metadata"]["git_commits"], ["deadbeef1234"])
        self.assertEqual(summary["metadata"]["backends"], ["graft"])

        self.assertEqual(len(summary["paired_overall"]), 1)
        overall = summary["paired_overall"][0]
        self.assertEqual(overall["treatment"], "passive[range=on]")
        self.assertEqual(overall["reference"], "passive[range=off]")
        self.assertAlmostEqual(overall["passed"]["delta"], 1.0)
        self.assertAlmostEqual(overall["hidden"]["delta"], 1.0)
        self.assertAlmostEqual(overall["commit"]["delta"], 0.0)
        self.assertAlmostEqual(overall["tokens"]["mean_diff"], 310.0)
        self.assertIsNotNone(overall["tokens"]["ci_low"])
        self.assertIsNotNone(overall["tokens"]["ci_high"])

        self.assertIn("Run metadata:", output)
        self.assertIn("git=deadbeef12", output)
        self.assertIn("hidden=3/3", output)
        self.assertIn("commit=0/3", output)
        self.assertIn("audit=3/3", output)
        self.assertIn("tokens=+310.0", output)

    def test_load_results_and_metadata_sidecar_isolation(self):
        with tempfile.TemporaryDirectory(prefix="aider-meta-test-") as tmp:
            root = Path(tmp)
            jsonl_path = root / "results.jsonl"
            jsonl_path.write_text(
                json.dumps({"record_type": "metadata", "schema_version": 2})
                + "\n\n"
                + json.dumps(
                    {
                        "round": 0,
                        "spec": "vague",
                        "kind": "caller",
                        "case": "duration",
                        "arm": "baseline",
                        "passed": False,
                    }
                )
                + "\n"
            )
            loaded = heldout.load_results(jsonl_path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["case"], "duration")

            meta_path = jsonl_path.with_suffix(".meta.json")
            meta1 = run.collect_run_metadata(suite="synthetic", seed=1, rounds=1)
            meta2 = run.collect_run_metadata(suite="synthetic", seed=2, rounds=2)
            run.write_run_metadata(meta_path, meta1)
            run.write_run_metadata(meta_path, meta2)
            saved = json.loads(meta_path.read_text())
            self.assertEqual(saved["schema_version"], run.SCHEMA_VERSION)
            self.assertEqual(len(saved["runs"]), 2)
            self.assertEqual(saved["latest"]["seed"], 2)

    def test_check_fixtures_isolated_python_case(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = heldout.check_fixtures([heldout.CASE_BY_NAME["duration"]])
        self.assertEqual(rc, 0)
        self.assertIn("1/1 passed", buf.getvalue())

    def test_check_fixtures_can_expect_a_failing_smoke_but_rejects_setup_error(self):
        case = heldout.CASE_BY_NAME["duration"]
        for smoke, expected_status in (
            ("raise AssertionError('fixture still broken')\n", 0),
            ("import missing_trial_fixture_module\n", 1),
            ("pass\n", 1),
        ):
            with self.subTest(smoke=smoke), contextlib.redirect_stdout(io.StringIO()):
                modified = replace(
                    case,
                    files={**case.files, "smoke.py": smoke},
                    smoke_passes_on_base=False,
                )
                self.assertEqual(heldout.check_fixtures([modified]), expected_status)


class TestReplaySuiteCliWiring(unittest.TestCase):
    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "Replay CLI Test"
        env["GIT_AUTHOR_EMAIL"] = "replay-cli-test@example.invalid"
        env["GIT_COMMITTER_NAME"] = "Replay CLI Test"
        env["GIT_COMMITTER_EMAIL"] = "replay-cli-test@example.invalid"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _git(self, repo: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
            env=self._git_env(),
        )
        return proc.stdout.strip()

    def _create_replay_manifest(
        self,
        root: Path,
        *,
        frozen: bool = True,
        sentinel: Path | None = None,
    ) -> Path:
        import replay_corpus

        repo = root / "pinned_repo"
        repo.mkdir()
        self._git(repo, "init")
        (repo / "api.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (repo / "smoke.py").write_text(
            "from api import value\nassert value() == 1\n", encoding="utf-8"
        )
        self._git(repo, "add", ".")
        self._git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "base")
        base_commit = self._git(repo, "rev-parse", "HEAD")

        ext_dir = root / "external_hidden"
        ext_dir.mkdir()
        hidden_file = ext_dir / "check_hidden.py"
        lines = []
        if sentinel is not None:
            lines.extend(
                [
                    "from pathlib import Path",
                    f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')",
                ]
            )
        lines.append("from api import value")
        lines.append("assert value() == 2")
        hidden_content = "\n".join(lines) + "\n"
        hidden_file.write_text(hidden_content, encoding="utf-8")
        hidden_sha = hashlib.sha256(hidden_content.encode("utf-8")).hexdigest()

        manifest_dict = {
            "schema_version": "1.0",
            "preregistration": {
                "protocol_version": "1.0",
                "preregistration_id": "cli-wiring-test-v1",
                "registered_at": "2026-11-24T00:00:00Z",
                "frozen": frozen,
                "hypothesis": "Verify replay CLI wiring enforces frozen manifests.",
                "primary_metric": "hidden_pass_rate",
                "secondary_metrics": [],
                "inclusion_criteria": ["Pinned local git checkout."],
                "exclusion_criteria": ["Tasks needing network access."],
                "stopping_rule": "Run all cases once.",
                "leakage_controls": ["Hidden check stored outside repository."],
                "cutoff_date": None,
                "cutoff_direction": "on_or_after",
                "manifest_sha256": None,
            },
            "cases": [
                {
                    "case_id": "mini_case",
                    "task_prompt": "Update value() to return 2.",
                    "explicit_guidance": "Change value() to return 2.",
                    "editable_files": ["api.py"],
                    "source": {
                        "repo_path": str(repo),
                        "base_commit": base_commit,
                        "fix_commit": None,
                        "expected_tree_sha": None,
                        "expected_files_sha256": None,
                        "subdir": "",
                        "include_globs": [],
                        "exclude_globs": [],
                        "max_file_bytes": 65536,
                        "skip_binary": True,
                        "version_label": "0.1.0",
                    },
                    "hidden": {
                        "command": ["{python}", "{hidden_dir}/check_hidden.py"],
                        "files": [
                            {
                                "target_path": "check_hidden.py",
                                "source_path": str(hidden_file),
                                "placement": "hidden_dir",
                                "sha256": hidden_sha,
                            }
                        ],
                        "cwd": None,
                        "env": {},
                        "timeout_seconds": 15.0,
                        "expected_exit_code": 0,
                    },
                    "smoke": {
                        "command": ["{python}", "smoke.py"],
                        "script": None,
                        "timeout_seconds": 15.0,
                    },
                    "preregistration": {
                        "selection_rationale": "Minimal offline replay case.",
                        "source_origin": "local/test#1",
                        "commit_date": None,
                        "expected_difficulty": "easy",
                        "file_scope_category": "single_file",
                        "leakage_checked": True,
                        "preregistered_notes": "",
                    },
                }
            ],
        }
        manifest_obj = replay_corpus.ReplayManifest.from_dict(manifest_dict)
        manifest_dict["preregistration"]["manifest_sha256"] = (
            replay_corpus.compute_manifest_digest(manifest_obj)
        )
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_dict), encoding="utf-8")
        return manifest_path

    def test_parse_replay_suite_and_reject_manifest_with_non_replay_suites(self):
        parser = heldout.build_arg_parser()
        default_args = parser.parse_args([])
        self.assertEqual(default_args.suite, "synthetic")
        self.assertIsNone(default_args.replay_manifest)

        replay_args = parser.parse_args(
            ["--suite", "replay", "--replay-manifest", "/tmp/frozen.json"]
        )
        self.assertEqual(replay_args.suite, "replay")
        self.assertEqual(replay_args.replay_manifest, "/tmp/frozen.json")

        for argv in (
            ["--replay-manifest", "/tmp/frozen.json"],
            ["--suite", "synthetic", "--replay-manifest", "/tmp/frozen.json"],
            ["--suite", "requests", "--replay-manifest", "/tmp/frozen.json"],
        ):
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as ctx:
                    heldout.resolve_suite_cases(parser.parse_args(argv), parser)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("--replay-manifest", err_buf.getvalue())

            err_buf_main = io.StringIO()
            with contextlib.redirect_stderr(err_buf_main):
                with self.assertRaises(SystemExit) as ctx_main:
                    heldout.main(argv)
            self.assertEqual(ctx_main.exception.code, 2)
            self.assertIn("--replay-manifest", err_buf_main.getvalue())

        for bad_filter in (
            ["--suite", "replay", "--replay-manifest", "/tmp/m.json", "--cases", "duration"],
            ["--suite", "replay", "--replay-manifest", "/tmp/m.json", "--languages", "python"],
        ):
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as ctx:
                    heldout.resolve_suite_cases(parser.parse_args(bad_filter), parser)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("--suite replay", err_buf.getvalue())

    def test_replay_loading_enforces_require_frozen_without_running_hidden_commands(self):
        parser = heldout.build_arg_parser()
        with tempfile.TemporaryDirectory(prefix="aider-replay-cli-") as tmp:
            root = Path(tmp)
            sentinel = root / "hidden_executed.txt"
            manifest_path = self._create_replay_manifest(root, frozen=True, sentinel=sentinel)

            args = parser.parse_args(
                ["--suite", "replay", "--replay-manifest", str(manifest_path)]
            )
            loaded_cases = heldout.resolve_suite_cases(args, parser)
            self.assertEqual(len(loaded_cases), 1)
            self.assertIsInstance(loaded_cases[0], run.CaseSpec)
            self.assertEqual(loaded_cases[0].name, "mini_case")
            self.assertEqual(loaded_cases[0].editable_file, "api.py")
            self.assertFalse(
                sentinel.exists(),
                "Loading a replay manifest must never execute external hidden commands",
            )

        # Verify exact delegation contract: replay_corpus.cases(path, require_frozen=True)
        fake_case = heldout.CASE_BY_NAME["duration"]
        stub_cases = mock.Mock(return_value=[fake_case])

        class StubFixtureError(ValueError):
            pass

        stub_replay = types.SimpleNamespace(
            cases=stub_cases,
            FixtureValidationError=StubFixtureError,
        )
        with mock.patch.dict(sys.modules, {"replay_corpus": stub_replay}):
            args = parser.parse_args(
                ["--suite", "replay", "--replay-manifest", "/tmp/pinned.json"]
            )
            result = heldout.resolve_suite_cases(args, parser)
        self.assertEqual(result, [fake_case])
        stub_cases.assert_called_once_with("/tmp/pinned.json", require_frozen=True)

    def test_replay_validation_errors_translated_to_parser_error(self):
        parser = heldout.build_arg_parser()
        with tempfile.TemporaryDirectory(prefix="aider-replay-err-") as tmp:
            root = Path(tmp)
            unfrozen_path = self._create_replay_manifest(root, frozen=False)

            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as ctx:
                    heldout.resolve_suite_cases(
                        parser.parse_args(
                            ["--suite", "replay", "--replay-manifest", str(unfrozen_path)]
                        ),
                        parser,
                    )
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("manifest_not_frozen", err_buf.getvalue())

            missing_path = root / "does_not_exist.json"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as ctx:
                    heldout.resolve_suite_cases(
                        parser.parse_args(
                            ["--suite", "replay", "--replay-manifest", str(missing_path)]
                        ),
                        parser,
                    )
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("Replay manifest not found", err_buf.getvalue())

        old_env = os.environ.pop("REPLAY_CORPUS_MANIFEST", None)
        try:
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as ctx:
                    heldout.resolve_suite_cases(parser.parse_args(["--suite", "replay"]), parser)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("manifest_path is required", err_buf.getvalue())
        finally:
            if old_env is not None:
                os.environ["REPLAY_CORPUS_MANIFEST"] = old_env

    def test_synthetic_and_requests_behavior_preserved_without_eager_replay_import(self):
        parser = heldout.build_arg_parser()
        saved_replay = sys.modules.pop("replay_corpus", None)
        try:
            synth_cases = heldout.resolve_suite_cases(
                parser.parse_args(["--cases", "duration"]), parser
            )
            self.assertEqual([c.name for c in synth_cases], ["duration"])
            self.assertNotIn("replay_corpus", sys.modules)

            fake_req_case = heldout.CASE_BY_NAME["names"]
            stub_requests = types.SimpleNamespace(cases=mock.Mock(return_value=[fake_req_case]))
            with mock.patch.dict(sys.modules, {"requests_cases": stub_requests}):
                req_cases = heldout.resolve_suite_cases(
                    parser.parse_args(["--suite", "requests"]), parser
                )
                self.assertEqual(req_cases, [fake_req_case])
                stub_requests.cases.assert_called_once_with()
                self.assertNotIn("replay_corpus", sys.modules)

                err_buf = io.StringIO()
                with contextlib.redirect_stderr(err_buf):
                    with self.assertRaises(SystemExit) as ctx:
                        heldout.resolve_suite_cases(
                            parser.parse_args(["--suite", "requests", "--cases", "duration"]),
                            parser,
                        )
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("--suite requests", err_buf.getvalue())
        finally:
            if saved_replay is not None:
                sys.modules["replay_corpus"] = saved_replay


class TestIssue5SuiteAndEconomyReporting(unittest.TestCase):
    def test_issue5_suite_loading_filtering_and_fixture_check(self):
        parser = heldout.build_arg_parser()
        all_cases = heldout.resolve_suite_cases(parser.parse_args(["--suite", "issue5"]), parser)
        names = [c.name for c in all_cases]
        self.assertEqual(
            names,
            [
                "facts_currency",
                "facts_sentinel",
                "facts_stale_control",
                "read_range_large_file",
                "caller_multi_scope",
            ],
        )

        filtered = heldout.resolve_suite_cases(
            parser.parse_args(
                ["--suite", "issue5", "--cases", "facts_currency,caller_multi_scope"]
            ),
            parser,
        )
        self.assertEqual([c.name for c in filtered], ["facts_currency", "caller_multi_scope"])

        for bad_argv in (
            ["--suite", "issue5", "--languages", "python"],
            ["--suite", "issue5", "--replay-manifest", "/tmp/frozen.json"],
        ):
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as ctx:
                    heldout.resolve_suite_cases(parser.parse_args(bad_argv), parser)
            self.assertEqual(ctx.exception.code, 2)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = heldout.check_fixtures(all_cases)
        self.assertEqual(rc, 0)
        self.assertIn("5/5 passed", buf.getvalue())

    def test_seed_repo_facts_jsonl_sqlite_and_mutate_after_seed_staleness(self):
        import issue5_cases
        from aider.project_facts import open_project_fact_store

        by_name = {c.name: c for c in issue5_cases.cases()}

        # 1. Default JSONL store records confirmed fact with deterministic chat_id
        with tempfile.TemporaryDirectory(prefix="aider-seed-jsonl-") as tmp:
            repo = Path(tmp)
            case = by_name["facts_currency"]
            run.write_fixture_files(repo, case.files)
            mutated = run.seed_repo_facts(repo, case.seed_facts)
            self.assertEqual(mutated, [])
            jsonl_path = repo / ".aider.facts.jsonl"
            self.assertTrue(jsonl_path.exists())
            raw_lines = [line for line in jsonl_path.read_text().splitlines() if line.strip()]
            self.assertEqual(len(raw_lines), 1)
            rec = json.loads(raw_lines[0])
            self.assertEqual(rec["verification"], "confirmed")
            self.assertTrue(rec["chat_id"].startswith("chat:"))

            with open_project_fact_store(repo, enabled=True) as store:
                self.assertEqual(store.backend, "jsonl")
                found = store.list_for_paths(["api.py"])
                self.assertEqual(len(found), 1)
                self.assertIn("FREE", found[0].statement)
                self.assertTrue(found[0].chat_id.startswith("chat:"))

        # 2. SQLite FTS5 store via facts_db
        with tempfile.TemporaryDirectory(prefix="aider-seed-sqlite-") as tmp:
            repo = Path(tmp)
            case = by_name["facts_sentinel"]
            run.write_fixture_files(repo, case.files)
            run.seed_repo_facts(repo, case.seed_facts, facts_db=".aider.facts.sqlite3")
            self.assertTrue((repo / ".aider.facts.sqlite3").exists())
            with open_project_fact_store(
                repo, enabled=True, facts_db=".aider.facts.sqlite3"
            ) as store:
                self.assertEqual(store.backend, "sqlite")
                found = store.search("sentinel")
                self.assertEqual(len(found), 1)
                self.assertIn("-99", found[0].statement)
                self.assertTrue(found[0].chat_id.startswith("chat:"))

        # 3. mutate_after_seed invalidates source-hash stale fact in both JSONL and SQLite
        for kwargs in ({}, {"facts_db": ".aider.facts.sqlite3"}):
            with tempfile.TemporaryDirectory(prefix="aider-seed-stale-") as tmp:
                repo = Path(tmp)
                case = by_name["facts_stale_control"]
                run.write_fixture_files(repo, case.files)
                mutated = run.seed_repo_facts(repo, case.seed_facts, **kwargs)
                self.assertEqual(mutated, ["policy.py"])
                self.assertIn("TAX_POLICY_VERSION = 2", (repo / "policy.py").read_text())

                with open_project_fact_store(repo, enabled=True, **kwargs) as store:
                    report = store.revalidate()
                    self.assertEqual(report.stale, 1)
                    self.assertEqual(report.active, 0)
                    self.assertEqual(store.list_for_paths(["policy.py"]), ())

    def test_preset_issue5_economy_defaults_and_overrides(self):
        parser = heldout.build_arg_parser()
        args = parser.parse_args(["--preset", "issue5-economy"])
        self.assertEqual(args.preset, "issue5-economy")
        self.assertEqual(args.suite, "issue5")
        self.assertEqual(args.arms, "passive")
        self.assertEqual(args.specs, "vague")
        self.assertEqual(args.rounds, 1)
        self.assertEqual(args.read_range_modes, "off,on")
        self.assertEqual(args.facts_modes, "off,on")

        grid = heldout.resolve_control_grid(args, parser)
        self.assertEqual(
            grid,
            [
                (None, False, False),
                (None, False, True),
                (None, True, False),
                (None, True, True),
            ],
        )

        # Explicit CLI overrides take precedence, including overriding back to base defaults
        override_args = parser.parse_args(
            [
                "--preset",
                "issue5-economy",
                "--suite",
                "synthetic",
                "--rounds",
                "3",
                "--arms",
                "baseline,passive",
                "--specs",
                "explicit",
                "--read-range-modes",
                "on",
                "--facts",
                "--facts-db",
                ".aider.facts.sqlite3",
            ]
        )
        self.assertEqual(override_args.suite, "synthetic")
        self.assertEqual(override_args.rounds, 3)
        self.assertEqual(override_args.arms, "baseline,passive")
        self.assertEqual(override_args.specs, "explicit")
        self.assertEqual(override_args.read_range_modes, "on")
        self.assertIsNone(override_args.facts_modes)
        self.assertIs(override_args.facts, True)
        self.assertEqual(
            heldout.resolve_control_grid(override_args, parser),
            [(None, True, True)],
        )

    def test_economy_and_efficiency_summary_metrics_and_table(self):
        self.assertIsNone(heldout._percentile([], 50))
        self.assertEqual(heldout._percentile([42.0], 95), 42.0)
        self.assertAlmostEqual(heldout._percentile([10.0, 20.0, 30.0, 40.0], 50), 25.0)
        self.assertAlmostEqual(heldout._percentile([10.0, 20.0, 30.0, 40.0], 95), 38.5)

        off_tokens = [1000, 1200, 1400, 2400]
        on_tokens = [900, 1000, 1100, 1500]
        off_secs = [4.0, 5.0, 6.0, 10.0]
        on_secs = [3.0, 4.0, 5.0, 8.0]
        off_pass = [True, False, False, False]
        on_pass = [True, True, True, False]

        rows = []
        for idx in range(4):
            rows.append(
                {
                    "schema_version": 2,
                    "model": "qwen-test",
                    "round": idx,
                    "spec": "vague",
                    "kind": "facts",
                    "language": "python",
                    "case": "facts_currency",
                    "arm": "passive",
                    "project_facts": False,
                    "facts_backend": "off",
                    "local_callers_enabled": True,
                    "passed": off_pass[idx],
                    "hidden_passed": off_pass[idx],
                    "commit_passed": True,
                    "evidence": True,
                    "tokens": off_tokens[idx],
                    "tokens_measured": True,
                    "editor_tokens": off_tokens[idx],
                    "audit_tokens": 0,
                    "first_editor_prompt_tokens": 450 if idx else None,
                    "prompt_tokens": off_tokens[idx] - 200,
                    "completion_tokens": 200,
                    "editor_calls": 2,
                    "seconds": off_secs[idx],
                    "metadata": {"suite": "issue5", "seed": 20261123},
                }
            )
            rows.append(
                {
                    "schema_version": 2,
                    "model": "qwen-test",
                    "round": idx,
                    "spec": "vague",
                    "kind": "facts",
                    "language": "python",
                    "case": "facts_currency",
                    "arm": "passive",
                    "project_facts": True,
                    "facts_backend": "jsonl",
                    "local_callers_enabled": True,
                    "passed": on_pass[idx],
                    "hidden_passed": on_pass[idx],
                    "commit_passed": True,
                    "evidence": True,
                    "tokens": on_tokens[idx],
                    "tokens_measured": True,
                    "editor_tokens": on_tokens[idx],
                    "audit_tokens": 0,
                    "first_editor_prompt_tokens": 500,
                    "prompt_tokens": on_tokens[idx] - 150,
                    "completion_tokens": 150,
                    "editor_calls": 1,
                    "seconds": on_secs[idx],
                    "metadata": {"suite": "issue5", "seed": 20261123},
                }
            )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            summary = heldout.summarize(rows)
        output = buf.getvalue()

        self.assertEqual(summary["arms"], ["passive[facts=off]", "passive[facts=on(jsonl)]"])
        self.assertEqual(summary["metadata"]["facts"], [False, True])
        self.assertEqual(summary["metadata"]["facts_backends"], ["jsonl", "off"])

        off_stat = summary["arm_outcomes"]["passive[facts=off]"]
        on_stat = summary["arm_outcomes"]["passive[facts=on(jsonl)]"]

        self.assertAlmostEqual(off_stat["p50_tokens"], 1300.0)
        self.assertAlmostEqual(off_stat["p95_tokens"], 2250.0)
        self.assertAlmostEqual(off_stat["tokens_per_solved"], 6000.0)
        self.assertAlmostEqual(off_stat["p50_seconds"], 5.5)
        self.assertAlmostEqual(off_stat["p95_seconds"], 9.4)
        self.assertAlmostEqual(off_stat["mean_prompt_tokens"], 1300.0)
        self.assertAlmostEqual(off_stat["mean_completion_tokens"], 200.0)
        self.assertAlmostEqual(off_stat["mean_editor_calls"], 2.0)

        self.assertAlmostEqual(on_stat["p50_tokens"], 1050.0)
        self.assertAlmostEqual(on_stat["p95_tokens"], 1440.0)
        self.assertAlmostEqual(on_stat["tokens_per_solved"], 1500.0)
        self.assertAlmostEqual(on_stat["p50_seconds"], 4.5)
        self.assertAlmostEqual(on_stat["p95_seconds"], 7.55)
        self.assertAlmostEqual(on_stat["mean_prompt_tokens"], 975.0)
        self.assertAlmostEqual(on_stat["mean_completion_tokens"], 150.0)
        self.assertAlmostEqual(on_stat["mean_editor_calls"], 1.0)

        # Paired % deltas: tokens (4500 - 6000) / 6000 = -25.0%, seconds (20 - 25) / 25 = -20.0%
        overall = summary["paired_overall"][0]
        self.assertEqual(overall["first_prompt"]["n_pairs"], 3)
        self.assertEqual(overall["first_prompt"]["mean_diff"], 50)
        self.assertEqual(overall["prompt"]["mean_diff"], -325)
        self.assertAlmostEqual(overall["prompt"]["delta_pct"], -25)
        self.assertAlmostEqual(on_stat["prompt_tokens_per_solved"], 1300)
        self.assertAlmostEqual(overall["delta_tokens_pct"], -25.0)
        self.assertAlmostEqual(overall["delta_seconds_pct"], -20.0)
        self.assertAlmostEqual(on_stat["delta_tokens_pct"], -25.0)
        self.assertAlmostEqual(on_stat["delta_seconds_pct"], -20.0)
        self.assertIsNone(off_stat["delta_tokens_pct"])

        self.assertIn("Economy & efficiency:", output)
        self.assertIn("facts=off,on", output)
        self.assertIn("facts_backend=jsonl,off", output)
        self.assertIn("-25.0%", output)
        self.assertIn("-20.0%", output)
        self.assertIn("1500.0", output)

        # Verify tokens_per_solved is None when passed == 0, and backend-only arm labels work
        backend_rows = [
            {
                "round": 0,
                "spec": "vague",
                "kind": "facts",
                "case": "facts_currency",
                "arm": "passive",
                "project_facts": True,
                "facts_backend": "jsonl",
                "passed": False,
                "tokens": 1000,
                "tokens_measured": True,
                "seconds": 2.0,
            },
            {
                "round": 0,
                "spec": "vague",
                "kind": "facts",
                "case": "facts_currency",
                "arm": "passive",
                "project_facts": True,
                "facts_backend": "sqlite",
                "passed": True,
                "tokens": 900,
                "tokens_measured": True,
                "seconds": 1.5,
            },
        ]
        backend_summary = heldout.build_summary(backend_rows)
        self.assertEqual(
            backend_summary["arms"],
            ["passive[facts_backend=jsonl]", "passive[facts_backend=sqlite]"],
        )
        self.assertIsNone(
            backend_summary["arm_outcomes"]["passive[facts_backend=jsonl]"]["tokens_per_solved"]
        )
        self.assertAlmostEqual(
            backend_summary["arm_outcomes"]["passive[facts_backend=sqlite]"]["tokens_per_solved"],
            900.0,
        )

    def test_issue5_trials_launcher_plan_and_optional_thinking_tokens(self):
        launcher = TRIAL_DIR / "issue5_trials.sh"
        with tempfile.TemporaryDirectory(prefix="aider-launcher-test-") as tmp:
            tmp_path = Path(tmp)
            results_dir = tmp_path / "results"
            base_env = os.environ.copy()
            base_env.pop("TRIAL_THINKING_TOKENS", None)
            base_env["TRIAL_PYTHON"] = sys.executable

            # 1. Default --plan fast without TRIAL_THINKING_TOKENS
            proc_default = subprocess.run(
                ["bash", str(launcher), "--plan", "fast", "test-model", str(results_dir)],
                capture_output=True,
                text=True,
                env=base_env,
                check=False,
            )
            self.assertEqual(proc_default.returncode, 0, proc_default.stderr)
            self.assertEqual(
                json.loads(proc_default.stdout),
                {"suite": "issue5", "cases": 5, "attempts": 10},
            )

            # 2. --plan fast with TRIAL_THINKING_TOKENS=4096
            budget_env = dict(base_env, TRIAL_THINKING_TOKENS="4096")
            proc_budget = subprocess.run(
                ["bash", str(launcher), "--plan", "fast", "test-model", str(results_dir)],
                capture_output=True,
                text=True,
                env=budget_env,
                check=False,
            )
            self.assertEqual(proc_budget.returncode, 0, proc_budget.stderr)
            self.assertEqual(
                json.loads(proc_budget.stdout),
                {"suite": "issue5", "cases": 5, "attempts": 10},
            )

            # 3. Verify exact argv forwarding via a lightweight TRIAL_PYTHON shim
            argv_log = tmp_path / "argv.json"
            shim = tmp_path / "python_shim.sh"
            shim.write_text(
                "#!/usr/bin/env bash\n"
                f'exec "{sys.executable}" -c '
                '"import json, pathlib, sys; '
                f"pathlib.Path({str(argv_log)!r}).write_text(json.dumps(sys.argv[1:]))\" "
                '"$@"\n',
                encoding="utf-8",
            )
            shim.chmod(0o755)

            shim_env = dict(base_env, TRIAL_PYTHON=str(shim))
            res_unset = subprocess.run(
                ["bash", str(launcher), "--plan", "fast", "test-model", str(results_dir)],
                capture_output=True,
                text=True,
                env=shim_env,
                check=False,
            )
            self.assertEqual(res_unset.returncode, 0, res_unset.stderr)
            argv_unset = json.loads(argv_log.read_text(encoding="utf-8"))
            self.assertNotIn("--thinking-tokens", argv_unset)

            res_empty = subprocess.run(
                ["bash", str(launcher), "--plan", "fast", "test-model", str(results_dir)],
                capture_output=True,
                text=True,
                env=dict(shim_env, TRIAL_THINKING_TOKENS=""),
                check=False,
            )
            self.assertEqual(res_empty.returncode, 0, res_empty.stderr)
            argv_empty = json.loads(argv_log.read_text(encoding="utf-8"))
            self.assertNotIn("--thinking-tokens", argv_empty)

            res_set = subprocess.run(
                ["bash", str(launcher), "--plan", "fast", "test-model", str(results_dir)],
                capture_output=True,
                text=True,
                env=dict(shim_env, TRIAL_THINKING_TOKENS="4096"),
                check=False,
            )
            self.assertEqual(res_set.returncode, 0, res_set.stderr)
            argv_set = json.loads(argv_log.read_text(encoding="utf-8"))
            idx = argv_set.index("--thinking-tokens")
            self.assertEqual(argv_set[idx + 1], "4096")


if __name__ == "__main__":
    unittest.main()
