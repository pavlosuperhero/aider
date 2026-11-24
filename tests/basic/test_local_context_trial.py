import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
