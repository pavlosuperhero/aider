import contextlib
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

        # Independently disable facts on passive while keeping read-range off and backend set
        flags_no_facts = run.resolve_arm_flags(
            "passive",
            backend="ast",
            read_range=False,
            facts=False,
        )
        self.assertEqual(
            flags_no_facts,
            [
                "--no-local-callers",
                "--local-callers-mode",
                "passive",
                "--no-local-callers-audit",
                "--no-local-callers-read-range",
                "--local-callers-backend",
                "ast",
            ],
        )
        self.assertNotIn("--local-callers", flags_no_facts)

        # Independently enable facts on baseline
        flags_with_facts = run.resolve_arm_flags("baseline", facts=True)
        self.assertEqual(flags_with_facts, ["--local-callers"])

        with self.assertRaises(ValueError):
            run.resolve_arm_flags("passive", backend="   ")
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
                "--no-local-callers-facts",
            ]
        )
        self.assertEqual(
            heldout.resolve_control_grid(single_args, parser),
            [("graft", True, False)],
        )


class TestTrialUsageAndOutcomes(unittest.TestCase):
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
    ) -> tuple[dict, list[str]]:
        captured_cmd: list[str] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and any("runpy.run_module('aider'" in str(part) for part in cmd):
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
        self.assertIs(ok_res["commit_passed"], True)
        self.assertEqual(ok_res["hidden_outcome"], "pass")
        self.assertIs(ok_res["hidden_passed"], True)
        self.assertIs(ok_res["smoke_passed"], True)
        self.assertIs(ok_res["passed"], True)
        self.assertIs(ok_res["false_approval"], False)
        self.assertEqual(ok_res["tokens"], 1250)
        self.assertIs(ok_res["tokens_measured"], True)
        self.assertEqual(ok_res["editor_tokens"], 1000)
        self.assertEqual(ok_res["audit_tokens"], 250)
        self.assertEqual(ok_res["local_callers_backend"], "graft")
        self.assertIs(ok_res["local_callers_read_range"], True)
        self.assertIs(ok_res["local_callers_facts"], True)
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
        self.assertIs(bad_hidden["commit_passed"], True)
        self.assertIs(bad_hidden["smoke_passed"], True)
        self.assertIs(bad_hidden["hidden_passed"], False)
        self.assertIs(bad_hidden["passed"], False)
        self.assertIs(bad_hidden["false_approval"], True)

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
                    "commit_passed": True,
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
                    "commit_passed": True,
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
        self.assertIn("commit=3/3", output)
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


if __name__ == "__main__":
    unittest.main()
