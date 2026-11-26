"""Focused unit and integration tests for scripts/local_context_trial/replay_corpus.py."""

from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from typing import Any
import unittest

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import replay_corpus  # noqa: E402


def _git_test_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Replay Test"
    env["GIT_AUTHOR_EMAIL"] = "replay-test@example.invalid"
    env["GIT_COMMITTER_NAME"] = "Replay Test"
    env["GIT_COMMITTER_EMAIL"] = "replay-test@example.invalid"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_git_test_env(),
    )
    return proc.stdout.strip()


def _create_sample_checkout(root: Path) -> dict[str, Any]:
    """Create a real local git repository with a pinned buggy commit and fix commit."""
    repo = root / "checkout"
    repo.mkdir()
    _git(repo, "init")

    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "normalizer.py").write_text(
        "def normalize_key(key: str) -> str:\n"
        "    return key.strip()\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "store.py").write_text(
        "from pkg.normalizer import normalize_key\n\n"
        "class Bag:\n"
        "    def __init__(self, items=None):\n"
        "        self._data = {normalize_key(k): v for k, v in (items or {}).items()}\n\n"
        "    def get(self, key: str):\n"
        "        return self._data[normalize_key(key)]\n\n"
        "    def set(self, key: str, value):\n"
        "        self._data[normalize_key(key)] = value\n\n"
        "    def copy(self):\n"
        "        return self\n",
        encoding="utf-8",
    )
    (repo / "assets").mkdir()
    (repo / "assets" / "logo.bin").write_bytes(b"\x00\x01\x02\xffbinary")
    (repo / "ignored.txt").write_text("exclude me\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "base buggy state")
    base_commit = _git(repo, "rev-parse", "HEAD")
    base_tree = _git(repo, "rev-parse", f"{base_commit}^{{tree}}")

    # Apply multi-file fix in a second commit and include a hidden test file in git history
    (repo / "pkg" / "normalizer.py").write_text(
        "def normalize_key(key: str) -> str:\n"
        "    return key.strip().lower()\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "store.py").write_text(
        "from pkg.normalizer import normalize_key\n\n"
        "class Bag:\n"
        "    def __init__(self, items=None):\n"
        "        self._data = {normalize_key(k): v for k, v in (items or {}).items()}\n\n"
        "    def get(self, key: str):\n"
        "        return self._data[normalize_key(key)]\n\n"
        "    def set(self, key: str, value):\n"
        "        self._data[normalize_key(key)] = value\n\n"
        "    def copy(self):\n"
        "        return Bag(self._data.copy())\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_regression.py").write_text(
        "import unittest\n"
        "from pkg.store import Bag\n\n"
        "class BagRegressionTest(unittest.TestCase):\n"
        "    def test_case_insensitive_independent_copy(self):\n"
        "        original = Bag({'X-Key': 'old'})\n"
        "        cloned = original.copy()\n"
        "        cloned.set('x-key', 'new')\n"
        "        self.assertEqual(original.get('x-key'), 'old')\n"
        "        self.assertEqual(cloned.get('X-Key'), 'new')\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "fix normalization and copy")
    fix_commit = _git(repo, "rev-parse", "HEAD")

    # Dirty the working tree deliberately to prove materialization ignores working tree state
    (repo / "pkg" / "store.py").write_text(
        "DIRTY WORKING TREE SHOULD BE IGNORED\n", encoding="utf-8"
    )

    # External hidden test directory outside the git checkout
    hidden_root = root / "external_hidden"
    hidden_root.mkdir()
    hidden_test = hidden_root / "test_bag_hidden.py"
    hidden_test_content = (
        "import unittest\n"
        "from pkg.store import Bag\n\n"
        "class ExternalHiddenBagTest(unittest.TestCase):\n"
        "    def test_case_insensitive_and_copy(self):\n"
        "        original = Bag({'Header': 'v1'})\n"
        "        cloned = original.copy()\n"
        "        cloned.set('header', 'v2')\n"
        "        self.assertEqual(original.get('header'), 'v1')\n"
        "        self.assertEqual(cloned.get('HEADER'), 'v2')\n"
    )
    hidden_test.write_text(hidden_test_content, encoding="utf-8")
    hidden_sha = hashlib.sha256(hidden_test_content.encode("utf-8")).hexdigest()

    return {
        "repo": repo,
        "base_commit": base_commit,
        "base_tree": base_tree,
        "fix_commit": fix_commit,
        "hidden_test": hidden_test,
        "hidden_sha": hidden_sha,
    }


def _make_manifest_dict(
    fixture: dict[str, Any],
    *,
    placement: str = "hidden_dir",
    use_git_hidden: bool = False,
    frozen: bool = True,
) -> dict[str, Any]:
    if use_git_hidden:
        hidden_file_spec = {
            "target_path": "tests/test_hidden_git.py",
            "git_commit": fixture["fix_commit"],
            "git_path": "tests/test_regression.py",
            "placement": "workspace",
            "sha256": None,
        }
        hidden_cmd = ["{python}", "-m", "unittest", "tests/test_hidden_git.py"]
    elif placement == "hidden_dir":
        hidden_file_spec = {
            "target_path": "test_bag_hidden.py",
            "source_path": str(fixture["hidden_test"]),
            "placement": "hidden_dir",
            "sha256": fixture["hidden_sha"],
        }
        hidden_cmd = [
            "{python}",
            "-m",
            "unittest",
            "discover",
            "-s",
            "{hidden_dir}",
            "-p",
            "test_*.py",
        ]
    else:
        hidden_file_spec = {
            "target_path": "hidden_suite/test_bag_hidden.py",
            "source_path": str(fixture["hidden_test"]),
            "placement": "workspace",
            "sha256": fixture["hidden_sha"],
        }
        hidden_cmd = [
            "{python}",
            "-m",
            "unittest",
            "discover",
            "-s",
            "hidden_suite",
            "-p",
            "test_*.py",
        ]

    return {
        "schema_version": "1.0",
        "preregistration": {
            "protocol_version": "1.0",
            "preregistration_id": "replay-unit-test-v1",
            "registered_at": "2026-11-24T00:00:00Z",
            "frozen": frozen,
            "hypothesis": (
                "Multi-file context improves hidden test pass rate on pinned replay bugs."
            ),
            "primary_metric": "hidden_pass_rate",
            "secondary_metrics": ["wall_time_seconds"],
            "inclusion_criteria": [
                "Reproducible fail-on-base and pass-on-fix in local pinned git checkout."
            ],
            "exclusion_criteria": ["Tasks requiring network access during verification."],
            "stopping_rule": "Evaluate all cases in the frozen manifest exactly once per arm.",
            "leakage_controls": [
                "Hidden test files are stored outside the materialized workspace.",
                "Source files are read from pinned commit OIDs.",
            ],
            "cutoff_date": "2026-01-01",
            "cutoff_direction": "on_or_after",
            "manifest_sha256": None,
        },
        "cases": [
            {
                "case_id": "bag_case_copy",
                "task_prompt": (
                    "Fix key normalization and Bag.copy so keys are case-insensitive"
                    " and copies are independent."
                ),
                "explicit_guidance": (
                    "Normalize keys to lowercase and clone internal storage in Bag.copy."
                ),
                "editable_files": ["pkg/normalizer.py", "pkg/store.py"],
                "source": {
                    "repo_path": str(fixture["repo"]),
                    "base_commit": fixture["base_commit"],
                    "fix_commit": fixture["fix_commit"],
                    "expected_tree_sha": fixture["base_tree"],
                    "expected_files_sha256": None,
                    "subdir": "",
                    "include_globs": ["pkg/**/*.py"],
                    "exclude_globs": ["ignored.txt"],
                    "max_file_bytes": 65536,
                    "skip_binary": True,
                    "version_label": "0.1.0-base",
                },
                "hidden": {
                    "command": hidden_cmd,
                    "files": [hidden_file_spec],
                    "cwd": None,
                    "env": {"TEST_MARKER": "1"},
                    "timeout_seconds": 30.0,
                    "expected_exit_code": 0,
                },
                "smoke": {
                    "command": [
                        "{python}",
                        "-c",
                        "from pkg.store import Bag; assert Bag({'a': 1}).get('a') == 1",
                    ],
                    "script": None,
                    "timeout_seconds": 15.0,
                },
                "preregistration": {
                    "selection_rationale": "Two-file state-isolation bug reproducible offline.",
                    "source_origin": "local/bag-fixture#1",
                    "commit_date": "2026-06-01",
                    "expected_difficulty": "medium",
                    "file_scope_category": "multi_file",
                    "leakage_checked": True,
                    "preregistered_notes": "Verified offline.",
                },
            }
        ],
    }


def _make_stub_run_module() -> types.SimpleNamespace:
    @dataclass(frozen=True)
    class StubCaseSpec:
        name: str
        files: dict[str, str]
        task: str
        hidden: str
        explicit: str
        editable_file: str
        additional_editable: tuple[str, ...] = ()

    def py_case(name, files, task, hidden, explicit, *, editable_file):
        return StubCaseSpec(
            name=name,
            files=dict(files),
            task=task,
            hidden=hidden,
            explicit=explicit,
            editable_file=editable_file,
        )

    return types.SimpleNamespace(CaseSpec=StubCaseSpec, py_case=py_case)


class ReplayCorpusTests(unittest.TestCase):
    def test_preregistration_docs_cover_all_dataclass_fields(self) -> None:
        corpus_fields = {f.name for f in dataclasses.fields(replay_corpus.PreregistrationSpec)}
        case_fields = {f.name for f in dataclasses.fields(replay_corpus.CasePreregistration)}
        self.assertEqual(set(replay_corpus.PREREGISTRATION_FIELD_DOCS), corpus_fields)
        self.assertEqual(set(replay_corpus.CASE_PREREGISTRATION_FIELD_DOCS), case_fields)

    def test_no_fictional_default_corpus(self) -> None:
        old = os.environ.pop(replay_corpus.MANIFEST_ENV_VAR, None)
        try:
            with self.assertRaisesRegex(ValueError, "manifest_path is required"):
                replay_corpus.cases()
        finally:
            if old is not None:
                os.environ[replay_corpus.MANIFEST_ENV_VAR] = old

    def test_pinned_materialization_ignores_dirty_worktree_and_validates_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)
            source = replay_corpus.SourcePin(
                repo_path=str(fixture["repo"]),
                base_commit=fixture["base_commit"],
                expected_tree_sha=fixture["base_tree"],
                include_globs=("pkg/**/*.py", "assets/**"),
                exclude_globs=("ignored.txt",),
                skip_binary=True,
            )
            files, digest = replay_corpus.materialize_source(
                source,
                base_dir=root,
                editable_files=("pkg/normalizer.py", "pkg/store.py"),
            )
            self.assertEqual(
                sorted(files.keys()),
                ["pkg/__init__.py", "pkg/normalizer.py", "pkg/store.py"],
            )
            self.assertIn("return self", files["pkg/store.py"])
            self.assertNotIn("DIRTY WORKING TREE", files["pkg/store.py"])

            pinned_source = dataclasses.replace(source, expected_files_sha256=digest)
            _, digest2 = replay_corpus.materialize_source(
                pinned_source,
                base_dir=root,
                editable_files=("pkg/normalizer.py", "pkg/store.py"),
            )
            self.assertEqual(digest, digest2)

            bad_digest_source = dataclasses.replace(source, expected_files_sha256="0" * 64)
            with self.assertRaisesRegex(
                replay_corpus.FixtureValidationError, "Materialized files digest mismatch"
            ):
                replay_corpus.materialize_source(bad_digest_source, base_dir=root)

            bad_tree_source = dataclasses.replace(source, expected_tree_sha="f" * 40)
            with self.assertRaisesRegex(
                replay_corpus.FixtureValidationError, "Git tree pin mismatch"
            ):
                replay_corpus.materialize_source(bad_tree_source, base_dir=root)

    def test_rejects_remote_urls_and_unpinned_git_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)

            for remote in (
                "https://github.com/example/repo.git",
                "git@github.com:example/repo.git",
                "file:///tmp/repo",
            ):
                src = replay_corpus.SourcePin(
                    repo_path=remote, base_commit=fixture["base_commit"]
                )
                with self.assertRaisesRegex(
                    replay_corpus.FixtureValidationError, "Remote repository URLs are forbidden"
                ):
                    replay_corpus.materialize_source(src, base_dir=root)

            for unpinned in ("HEAD", "main", fixture["base_commit"][:12]):
                src = replay_corpus.SourcePin(
                    repo_path=str(fixture["repo"]), base_commit=unpinned
                )
                with self.assertRaisesRegex(
                    replay_corpus.FixtureValidationError, "full 40- or 64-char"
                ):
                    replay_corpus.materialize_source(src, base_dir=root)

    def test_full_executable_validation_hidden_dir_and_manifest_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)
            manifest_dict = _make_manifest_dict(fixture, placement="hidden_dir")
            manifest_obj = replay_corpus.ReplayManifest.from_dict(manifest_dict)
            manifest_dict["preregistration"]["manifest_sha256"] = (
                replay_corpus.compute_manifest_digest(manifest_obj)
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest_dict), encoding="utf-8")

            loaded, base_dir = replay_corpus.load_manifest(manifest_path)
            report = replay_corpus.validate_manifest(
                loaded, base_dir=base_dir, run_checks=True, require_frozen=True
            )
            self.assertTrue(report.ok, msg=f"Unexpected errors: {report.errors}")
            res = report.case_results["bag_case_copy"]
            self.assertTrue(res.base_smoke_passed)
            self.assertTrue(res.base_hidden_failed)
            self.assertTrue(res.fix_hidden_passed)
            self.assertTrue(res.fix_smoke_passed)

            # Tampering with task_prompt after pinning manifest_sha256 must fail validation
            manifest_dict["cases"][0]["task_prompt"] = "Tampered prompt after preregistration."
            tampered = replay_corpus.ReplayManifest.from_dict(manifest_dict)
            tampered_report = replay_corpus.validate_manifest(
                tampered, base_dir=base_dir, run_checks=False
            )
            self.assertFalse(tampered_report.ok)
            self.assertIn(
                "manifest_digest_mismatch",
                {issue.code for issue in tampered_report.errors},
            )

    def test_workspace_hidden_overlay_cleans_up_and_git_hidden_source_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)

            # Workspace placement from external file cleans up overlays even when check fails
            manifest_ws = replay_corpus.ReplayManifest.from_dict(
                _make_manifest_dict(fixture, placement="workspace")
            )
            mat = replay_corpus.materialize_case(manifest_ws.cases[0], base_dir=root)
            script = replay_corpus.build_hidden_runner_script(
                mat.entry.hidden, mat.hidden_files
            )
            ws_dir = root / "ws_run"
            ws_dir.mkdir()
            replay_corpus._write_workspace_files(ws_dir, mat.files)

            proc = replay_corpus._run_python_script_in_workspace(
                script, ws_dir, timeout_seconds=30.0
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse((ws_dir / "hidden_suite").exists())

            # Git-sourced hidden file from fix_commit passes fail-on-base / pass-on-fix checks
            manifest_git = replay_corpus.ReplayManifest.from_dict(
                _make_manifest_dict(fixture, use_git_hidden=True)
            )
            report = replay_corpus.validate_manifest(
                manifest_git, base_dir=root, run_checks=True
            )
            self.assertTrue(report.ok, msg=f"Unexpected errors: {report.errors}")
            self.assertTrue(report.case_results["bag_case_copy"].base_hidden_failed)
            self.assertTrue(report.case_results["bag_case_copy"].fix_hidden_passed)

    def test_leakage_and_tamper_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)

            # 1. Hidden source_path inside repo_path is rejected
            bad_inside_repo = _make_manifest_dict(fixture, placement="hidden_dir")
            bad_inside_repo["cases"][0]["hidden"]["files"][0]["source_path"] = str(
                fixture["repo"] / "ignored.txt"
            )
            bad_inside_repo["cases"][0]["hidden"]["files"][0]["sha256"] = None
            report = replay_corpus.validate_manifest(
                replay_corpus.ReplayManifest.from_dict(bad_inside_repo), base_dir=root
            )
            self.assertFalse(report.ok)
            self.assertTrue(
                any("must be external to repo_path" in e.message for e in report.errors)
            )

            # 2. Workspace hidden file colliding with materialized source is rejected
            bad_collision = _make_manifest_dict(fixture, placement="workspace")
            bad_collision["cases"][0]["hidden"]["files"][0]["target_path"] = "pkg/store.py"
            report2 = replay_corpus.validate_manifest(
                replay_corpus.ReplayManifest.from_dict(bad_collision), base_dir=root
            )
            self.assertFalse(report2.ok)
            self.assertTrue(
                any(
                    "collides with materialized workspace files" in e.message
                    for e in report2.errors
                )
            )

            # 3. SHA-256 mismatch on external hidden file is rejected
            bad_sha = _make_manifest_dict(fixture, placement="hidden_dir")
            bad_sha["cases"][0]["hidden"]["files"][0]["sha256"] = "a" * 64
            report3 = replay_corpus.validate_manifest(
                replay_corpus.ReplayManifest.from_dict(bad_sha), base_dir=root
            )
            self.assertFalse(report3.ok)
            self.assertTrue(
                any("SHA-256 mismatch" in e.message for e in report3.errors)
            )

    def test_preregistration_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)
            bad = _make_manifest_dict(fixture, frozen=False)
            bad["preregistration"]["registered_at"] = "2026-11-24"  # missing timezone
            bad["cases"][0]["preregistration"]["file_scope_category"] = "single_file"
            bad["cases"][0]["preregistration"]["leakage_checked"] = False
            bad["cases"][0]["preregistration"]["commit_date"] = "2025-12-31"
            bad["cases"][0]["preregistration"]["expected_difficulty"] = "impossible"

            report = replay_corpus.validate_manifest(
                replay_corpus.ReplayManifest.from_dict(bad),
                base_dir=root,
                require_frozen=True,
            )
            codes = {issue.code for issue in report.errors}
            self.assertTrue(
                {
                    "manifest_not_frozen",
                    "invalid_registered_at",
                    "file_scope_mismatch",
                    "leakage_not_checked",
                    "cutoff_date_violation",
                    "invalid_expected_difficulty",
                }.issubset(codes),
                msg=f"Missing expected error codes; got {codes}",
            )

            # Unknown keys are rejected on parse
            bad_unknown = _make_manifest_dict(fixture)
            bad_unknown["unexpected_top_key"] = True
            with self.assertRaisesRegex(
                replay_corpus.FixtureValidationError, "Unknown keys"
            ):
                replay_corpus.ReplayManifest.from_dict(bad_unknown)

    def test_broken_fixture_detected_by_run_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)
            # Hidden command that trivially exits 0 even on buggy base_commit must fail validation
            trivially_passing = _make_manifest_dict(fixture)
            trivially_passing["cases"][0]["hidden"]["command"] = [
                "{python}",
                "-c",
                "assert True",
            ]
            report = replay_corpus.validate_manifest(
                replay_corpus.ReplayManifest.from_dict(trivially_passing),
                base_dir=root,
                run_checks=True,
            )
            self.assertFalse(report.ok)
            self.assertIn(
                "base_hidden_unexpectedly_passed",
                {issue.code for issue in report.errors},
            )

    def test_cases_compatible_with_stub_and_real_run_casespec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(_make_manifest_dict(fixture, placement="hidden_dir")),
                encoding="utf-8",
            )

            # 1. Verify against stub matching requests_cases' exact run.py_case / CaseSpec contract
            stub_run = _make_stub_run_module()
            built = replay_corpus.cases(manifest_path, run_module=stub_run)
            self.assertEqual(len(built), 1)
            spec = built[0]
            self.assertIsInstance(spec, stub_run.CaseSpec)
            self.assertEqual(spec.name, "bag_case_copy")
            self.assertEqual(spec.editable_file, "pkg/normalizer.py")
            self.assertEqual(spec.additional_editable, ("pkg/store.py",))
            self.assertNotIn("test_bag_hidden.py", spec.files)
            self.assertIn("smoke.py", spec.files)

            # Execute produced CaseSpec's smoke and hidden scripts directly
            ws = root / "exec_ws"
            ws.mkdir()
            replay_corpus._write_workspace_files(ws, spec.files)
            smoke_proc = replay_corpus._run_python_script_in_workspace(
                spec.files["smoke.py"], ws, timeout_seconds=15.0
            )
            self.assertEqual(smoke_proc.returncode, 0, msg=smoke_proc.stderr)
            hidden_base_proc = replay_corpus._run_python_script_in_workspace(
                spec.hidden, ws, timeout_seconds=30.0
            )
            self.assertNotEqual(hidden_base_proc.returncode, 0)

            # 2. Verify against sibling run.py when present in scripts/local_context_trial/
            try:
                import run as real_run  # type: ignore[import-not-found]
            except ImportError:
                real_run = None

            if real_run is not None and hasattr(real_run, "CaseSpec"):
                real_built = replay_corpus.cases(manifest_path, run_module=real_run)
                self.assertEqual(len(real_built), 1)
                real_spec = real_built[0]
                self.assertIsInstance(real_spec, real_run.CaseSpec)
                self.assertEqual(real_spec.additional_editable, ("pkg/store.py",))
                self.assertEqual((real_spec.language, real_spec.kind), ("python", "caller"))
                # Ensure no dict field on real_spec exposes the external hidden file
                for value in dataclasses.asdict(real_spec).values():
                    if isinstance(value, dict):
                        self.assertNotIn("test_bag_hidden.py", value)

    def test_replay_cases_keep_preregistered_language_and_category(self) -> None:
        import run

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)
            manifest = _make_manifest_dict(fixture)
            manifest["cases"][0]["preregistration"]["preregistered_notes"] = (
                "Verified offline; language = javascript; category=control; repo=test/project"
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            [case] = replay_corpus.cases(manifest_path, run_module=run)

            self.assertEqual((case.language, case.kind), ("javascript", "control"))

    def test_cli_schema_digest_list_and_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _create_sample_checkout(root)
            manifest_dict = _make_manifest_dict(fixture)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest_dict), encoding="utf-8")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = replay_corpus.main(["schema"])
            self.assertEqual(rc, 0)
            schema_out = json.loads(buf.getvalue())
            self.assertEqual(schema_out["schema_version"], replay_corpus.SCHEMA_VERSION)
            self.assertIn("hypothesis", schema_out["preregistration_fields"])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = replay_corpus.main(["digest", str(manifest_path)])
            self.assertEqual(rc, 0)
            digest_out = json.loads(buf.getvalue())
            manifest_dict["preregistration"]["manifest_sha256"] = digest_out["manifest_sha256"]
            manifest_path.write_text(json.dumps(manifest_dict), encoding="utf-8")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = replay_corpus.main(["list", str(manifest_path)])
            self.assertEqual(rc, 0)
            list_out = json.loads(buf.getvalue())
            self.assertEqual(len(list_out), 1)
            self.assertEqual(list_out[0]["case_id"], "bag_case_copy")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = replay_corpus.main(
                    [
                        "validate",
                        str(manifest_path),
                        "--run-checks",
                        "--require-frozen",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            val_out = json.loads(buf.getvalue())
            self.assertTrue(val_out["ok"])


if __name__ == "__main__":
    unittest.main()
