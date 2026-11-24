import hashlib
import shutil
import sqlite3 as real_sqlite3
from pathlib import Path

import pytest

from aider.history import ChatSummary
from aider.project_facts import (
    DEFAULT_FACTS_FILENAME,
    MAX_TOP_K,
    FactProvenance,
    FactStatus,
    FactStoreReadOnlyError,
    FactValidationError,
    FakeFactRepository,
    Fts5UnavailableError,
    ProjectFact,
    SourceSnapshot,
    SqliteFtsFactRepository,
    SqliteUnavailableError,
    VerificationState,
    capture_source_snapshots,
    format_facts_context,
    hash_worktree_file,
    open_project_fact_store,
    parse_fact_command_args,
    resolve_facts_db_path,
    worktree_scope_id,
)


class _NoFts5Connection:
    """Proxy SQLite connection that simulates a SQLite build without FTS5."""

    def __init__(self, inner):
        self._inner = inner

    def execute(self, sql, *args, **kwargs):
        if "fts5" in sql.lower():
            raise real_sqlite3.OperationalError("no such module: fts5")
        return self._inner.execute(sql, *args, **kwargs)

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _NoFts5SqliteModule:
    Row = real_sqlite3.Row
    OperationalError = real_sqlite3.OperationalError
    DatabaseError = real_sqlite3.DatabaseError

    def connect(self, *args, **kwargs):
        return _NoFts5Connection(real_sqlite3.connect(*args, **kwargs))


class _LossyFakeModel:
    """Deterministic offline model for exercising ChatSummary compaction."""

    name = "fake-lossy-model"
    info = {"max_input_tokens": 4096}

    def token_count(self, messages):
        if isinstance(messages, dict):
            return len(str(messages.get("content", "")).split())
        if isinstance(messages, list):
            return sum(self.token_count(m) for m in messages)
        return len(str(messages).split())

    def simple_send_with_retries(self, _messages):
        return "Discussed routine cleanup and formatting adjustments."


def _make_repository(backend: str, tmp_path: Path):
    if backend == "fake":
        return FakeFactRepository()
    if backend == "sqlite":
        return SqliteFtsFactRepository(tmp_path / "facts.sqlite3")
    raise ValueError(f"Unknown backend: {backend}")


def test_disabled_mode_does_not_open_or_create_store(tmp_path):
    default_db = tmp_path / DEFAULT_FACTS_FILENAME
    custom_db = tmp_path / "custom" / "facts.sqlite3"

    with open_project_fact_store(tmp_path, enabled=False) as store:
        assert not store.enabled
        assert not store.available
        assert not store.active
        assert store.db_path is None
        assert "disabled" in store.diagnostic.lower()
        assert store.search("anything") == ()
        assert store.list_for_paths(["app.py"]) == ()
        res = store.handle_fact_command("All timestamps must be UTC.")
        assert not res.ok
        assert "disabled" in res.message.lower()

    with open_project_fact_store(tmp_path, enabled=False, facts_db=custom_db) as store:
        assert not store.active

    assert not default_db.exists()
    assert not custom_db.exists()
    assert not custom_db.parent.exists()


def test_default_and_custom_db_paths(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert resolve_facts_db_path(tmp_path) == (tmp_path / DEFAULT_FACTS_FILENAME).resolve()
    assert resolve_facts_db_path(tmp_path, "sub/custom.sqlite3") == (
        tmp_path / "sub" / "custom.sqlite3"
    ).resolve()

    with open_project_fact_store(tmp_path, enabled=True) as default_store:
        assert default_store.active
        assert default_store.db_path == (tmp_path / DEFAULT_FACTS_FILENAME).resolve()
        default_store.record_fact("Use UTC everywhere.", source_paths=["app.py"])
    assert (tmp_path / DEFAULT_FACTS_FILENAME).is_file()

    custom_path = tmp_path / "alt_dir" / "custom_facts.sqlite3"
    with open_project_fact_store(tmp_path, enabled=True, facts_db=custom_path) as custom_store:
        assert custom_store.active
        assert custom_store.db_path == custom_path.resolve()
        custom_store.record_fact("Always validate inputs at boundary.", source_paths=["app.py"])
    assert custom_path.is_file()


def test_works_without_standalone_sqlite_cli_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert shutil.which("sqlite3") is None

    (tmp_path / "service.py").write_text("def handle():\n    return 'ok'\n", encoding="utf-8")
    with open_project_fact_store(tmp_path, enabled=True) as store:
        assert store.active
        fact = store.record_fact(
            "Service handlers return normalized ISO timestamps.",
            source_paths=["service.py"],
            tags=["convention"],
        )
        found = store.search("normalized ISO timestamps")
        assert [f.fact_id for f in found] == [fact.fact_id]
        by_path = store.list_for_paths(["service.py"])
        assert [f.fact_id for f in by_path] == [fact.fact_id]


def test_missing_python_sqlite_degrades_gracefully_without_touching_existing_db(
    tmp_path, monkeypatch
):
    existing_db = tmp_path / DEFAULT_FACTS_FILENAME
    sentinel = b"pre-existing-sqlite-file-must-remain-unchanged"
    existing_db.write_bytes(sentinel)
    before_files = sorted(p.name for p in tmp_path.iterdir())
    before_mtime = existing_db.stat().st_mtime_ns

    monkeypatch.setattr("aider.project_facts.sqlite3", None)

    with pytest.raises(SqliteUnavailableError):
        SqliteFtsFactRepository(existing_db)

    with open_project_fact_store(tmp_path, enabled=True) as store:
        assert store.enabled
        assert not store.available
        assert not store.active
        assert "disabled" in store.diagnostic.lower()
        assert "sqlite3" in store.diagnostic.lower()
        assert store.search("anything") == ()
        assert store.list_for_paths(["app.py"]) == ()
        cmd = store.handle_fact_command("Do not silently write to fallback store.")
        assert not cmd.ok
        assert "disabled" in cmd.message.lower()

    assert existing_db.read_bytes() == sentinel
    assert existing_db.stat().st_mtime_ns == before_mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == before_files


def test_missing_fts5_degrades_gracefully_without_touching_existing_db(tmp_path):
    existing_db = tmp_path / DEFAULT_FACTS_FILENAME
    sentinel = b"existing-db-preserved-when-fts5-missing"
    existing_db.write_bytes(sentinel)
    before_files = sorted(p.name for p in tmp_path.iterdir())
    before_mtime = existing_db.stat().st_mtime_ns
    no_fts5 = _NoFts5SqliteModule()

    with pytest.raises(Fts5UnavailableError):
        SqliteFtsFactRepository(existing_db, sqlite_module=no_fts5)

    with open_project_fact_store(tmp_path, enabled=True, sqlite_module=no_fts5) as store:
        assert store.enabled
        assert not store.available
        assert not store.active
        assert "disabled" in store.diagnostic.lower()
        assert "fts5" in store.diagnostic.lower()
        assert store.search("anything") == ()
        cmd = store.handle_fact_command("Must not save to any fallback file.")
        assert not cmd.ok
        assert "fts5" in cmd.message.lower()

    assert existing_db.read_bytes() == sentinel
    assert existing_db.stat().st_mtime_ns == before_mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == before_files


@pytest.mark.parametrize("backend", ["fake", "sqlite"])
def test_repository_add_search_list_for_paths_and_provenance(backend, tmp_path):
    wt_root = tmp_path / "repo"
    wt_root.mkdir()
    (wt_root / "pkg").mkdir()
    (wt_root / "pkg" / "pricing.py").write_text("def cents(v): return int(v * 100)\n")
    (wt_root / "pkg" / "auth.py").write_text("def verify(tok): return bool(tok)\n")
    wt_id = worktree_scope_id(wt_root)

    with _make_repository(backend, tmp_path) as repo:
        pricing_snaps = capture_source_snapshots(
            wt_root, ["pkg/pricing.py"], start_line=1, end_line=1, symbol="cents"
        )
        auth_snaps = capture_source_snapshots(wt_root, ["pkg/auth.py"])

        f_pricing = ProjectFact.confirmed(
            worktree_id=wt_id,
            statement="All monetary amounts are stored as integer cents.",
            sources=pricing_snaps,
            origin="user:/fact",
            tags=("money", "invariant"),
            commit_sha="abc1234",
            note="Confirmed in billing review",
        )
        f_auth = ProjectFact.confirmed(
            worktree_id=wt_id,
            statement="Session tokens are validated strictly before route dispatch.",
            sources=auth_snaps,
            origin="user:/fact",
            tags=("security",),
        )
        f_candidate = ProjectFact.model_candidate(
            worktree_id=wt_id,
            statement="Monetary rounding uses banker's rounding in pricing.",
            sources=pricing_snaps,
        )

        repo.add(f_pricing)
        repo.add(f_auth)
        repo.add(f_candidate)

        fetched = repo.get(f_pricing.fact_id, worktree_id=wt_id)
        assert fetched == f_pricing
        assert fetched.provenance.commit_sha == "abc1234"
        assert fetched.provenance.note == "Confirmed in billing review"
        assert fetched.provenance.sources[0].symbol == "cents"
        assert fetched.provenance.sources[0].start_line == 1

        # Search excludes unverified model candidates by default.
        search_confirmed = repo.search("monetary", worktree_id=wt_id)
        assert [f.fact_id for f in search_confirmed] == [f_pricing.fact_id]

        # Including unverified reveals the candidate while keeping it marked unverified.
        search_all = repo.search("monetary", worktree_id=wt_id, include_unverified=True)
        assert {f.fact_id for f in search_all} == {f_pricing.fact_id, f_candidate.fact_id}
        candidate_result = next(f for f in search_all if f.fact_id == f_candidate.fact_id)
        assert candidate_result.is_unverified
        assert not candidate_result.is_confirmed

        # list_for_paths matches by normalized path and respects unverified filter.
        by_pricing = repo.list_for_paths(["./pkg/pricing.py"], worktree_id=wt_id)
        assert [f.fact_id for f in by_pricing] == [f_pricing.fact_id]

        by_pricing_all = repo.list_for_paths(
            ["pkg/pricing.py"], worktree_id=wt_id, include_unverified=True
        )
        assert {f.fact_id for f in by_pricing_all} == {f_pricing.fact_id, f_candidate.fact_id}

        # Special punctuation / FTS operators must not raise syntax errors.
        assert repo.search('NOT OR "unclosed (cents): ---', worktree_id=wt_id) == ()
        assert repo.search("!!!", worktree_id=wt_id) == ()


@pytest.mark.parametrize("backend", ["fake", "sqlite"])
def test_worktree_isolation_and_bounded_top_k(backend, tmp_path):
    wt_a = tmp_path / "wt_a"
    wt_b = tmp_path / "wt_b"
    wt_a.mkdir()
    wt_b.mkdir()
    (wt_a / "api.py").write_text("A = 1\n", encoding="utf-8")
    (wt_b / "api.py").write_text("B = 2\n", encoding="utf-8")

    wt_a_id = worktree_scope_id(wt_a)
    wt_b_id = worktree_scope_id(wt_b)
    assert wt_a_id != wt_b_id

    with _make_repository(backend, tmp_path) as repo:
        snap_a = capture_source_snapshots(wt_a, ["api.py"])
        snap_b = capture_source_snapshots(wt_b, ["api.py"])

        # Add more than MAX_TOP_K facts to wt_a to verify upper cap.
        for idx in range(MAX_TOP_K + 5):
            repo.add(
                ProjectFact.confirmed(
                    fact_id=f"shared_id_{idx:02d}",
                    worktree_id=wt_a_id,
                    statement=f"Alpha worktree convention number {idx} for api pagination.",
                    sources=snap_a,
                )
            )

        # Same fact_id in wt_b must not collide with or overwrite wt_a.
        fact_b = ProjectFact.confirmed(
            fact_id="shared_id_00",
            worktree_id=wt_b_id,
            statement="Beta worktree convention for api pagination.",
            sources=snap_b,
        )
        repo.add(fact_b)

        # Worktree isolation on get, search, and list_for_paths.
        got_a = repo.get("shared_id_00", worktree_id=wt_a_id)
        got_b = repo.get("shared_id_00", worktree_id=wt_b_id)
        assert got_a is not None and "Alpha" in got_a.statement
        assert got_b is not None and "Beta" in got_b.statement

        res_b_search = repo.search("pagination", worktree_id=wt_b_id, top_k=10)
        assert [f.fact_id for f in res_b_search] == ["shared_id_00"]
        assert all(f.worktree_id == wt_b_id for f in res_b_search)

        res_b_paths = repo.list_for_paths(["api.py"], worktree_id=wt_b_id, top_k=10)
        assert [f.fact_id for f in res_b_paths] == ["shared_id_00"]

        # Bounded top-K enforcement (explicit small top_k, 0, and exceeding MAX_TOP_K).
        assert len(repo.search("pagination", worktree_id=wt_a_id, top_k=3)) == 3
        assert len(repo.list_for_paths(["api.py"], worktree_id=wt_a_id, top_k=4)) == 4
        assert repo.search("pagination", worktree_id=wt_a_id, top_k=0) == ()
        assert repo.list_for_paths(["api.py"], worktree_id=wt_a_id, top_k=-2) == ()
        assert len(repo.search("pagination", worktree_id=wt_a_id, top_k=999)) == MAX_TOP_K
        assert len(repo.list_for_paths(["api.py"], worktree_id=wt_a_id, top_k=999)) == MAX_TOP_K


def test_model_candidates_cannot_be_created_confirmed_and_require_explicit_confirmation(tmp_path):
    (tmp_path / "core.py").write_text("X = 1\n", encoding="utf-8")
    wt_id = worktree_scope_id(tmp_path)
    snaps = capture_source_snapshots(tmp_path, ["core.py"])

    with pytest.raises(FactValidationError):
        ProjectFact(
            fact_id="bad_confirmed",
            worktree_id=wt_id,
            statement="Claimed by model.",
            verification=VerificationState.CONFIRMED,
            provenance=FactProvenance(
                origin="model_candidate",
                worktree_id=wt_id,
                recorded_at="2026-01-01T00:00:00Z",
                sources=snaps,
            ),
        )

    with open_project_fact_store(tmp_path, enabled=True) as store:
        cand = store.record_candidate(
            "Model hypothesis: core.py caches results per thread.",
            source_paths=["core.py"],
        )
        assert cand.is_unverified
        assert store.search("caches results") == ()
        assert [f.fact_id for f in store.search("caches results", include_unverified=True)] == [
            cand.fact_id
        ]

        confirmed = store.confirm_fact(cand.fact_id, origin="user:/fact")
        assert confirmed is not None
        assert confirmed.is_confirmed
        assert confirmed.provenance.origin == "user:/fact"
        assert [f.fact_id for f in store.search("caches results")] == [cand.fact_id]


def test_compaction_restart_provenance_survival_and_source_invalidation(tmp_path):
    """Record a decision, drop it during ChatSummary compaction, restart, then edit/delete sources."""
    src_edited = tmp_path / "ledger.py"
    src_deleted = tmp_path / "legacy_adapter.py"
    src_edited.write_text("def balance_cents(entries): return sum(entries)\n", encoding="utf-8")
    src_deleted.write_text("def legacy_export(): return []\n", encoding="utf-8")

    orig_edited_hash = hashlib.sha256(src_edited.read_bytes()).hexdigest()
    orig_deleted_hash = hashlib.sha256(src_deleted.read_bytes()).hexdigest()
    assert hash_worktree_file(tmp_path, "ledger.py") == orig_edited_hash

    with open_project_fact_store(tmp_path, enabled=True) as store:
        cmd_edited = store.handle_fact_command(
            "--file ledger.py --tag billing --symbol balance_cents "
            "Ledger balances must always be computed in integer cents.",
            commit_sha="d2f5965",
        )
        assert cmd_edited.ok and cmd_edited.fact is not None
        fact_edited = cmd_edited.fact

        cmd_deleted = store.handle_fact_command(
            "--file legacy_adapter.py Legacy export must preserve empty list contract.",
            commit_sha="d2f5965",
        )
        assert cmd_deleted.ok and cmd_deleted.fact is not None
        fact_deleted = cmd_deleted.fact

    # Simulate conversation history where the decision is forced outside the retained tail
    # and dropped by ChatSummary compaction.
    history = [
        {
            "role": "user",
            "content": "Decision: Ledger balances must always be computed in integer cents.",
        },
        {"role": "assistant", "content": "Understood, using integer cents for ledger balances."},
    ]
    for turn in range(12):
        history.append(
            {"role": "user", "content": f"Turn {turn}: refactor unrelated UI widgets " + ("word " * 12)}
        )
        history.append(
            {"role": "assistant", "content": f"Turn {turn}: updated unrelated UI widgets " + ("done " * 12)}
        )

    summarizer = ChatSummary([_LossyFakeModel()], max_tokens=60)
    assert summarizer.too_big(history)
    compacted = summarizer.summarize(history)
    compacted_text = "\n".join(m["content"] for m in compacted)
    assert "integer cents" not in compacted_text

    # Restart: open a fresh store instance pointing at the same worktree DB.
    with open_project_fact_store(tmp_path, enabled=True) as restarted:
        recalled = restarted.search("integer cents")
        assert len(recalled) == 1
        survived = recalled[0]
        assert survived.fact_id == fact_edited.fact_id
        assert survived.is_confirmed
        assert survived.is_active
        assert survived.source_hashes == {"ledger.py": orig_edited_hash}
        assert survived.provenance.origin == "user:/fact"
        assert survived.provenance.recorded_at == fact_edited.provenance.recorded_at
        assert survived.provenance.commit_sha == "d2f5965"
        assert survived.provenance.sources[0].symbol == "balance_cents"

        # Editing the bound source file must mark the fact stale while keeping original hash/provenance.
        src_edited.write_text(
            "def balance_cents(entries): return sum(entries) / 100.0\n", encoding="utf-8"
        )
        assert restarted.search("integer cents") == ()
        assert restarted.list_for_paths(["ledger.py"]) == ()

        stale_edited = restarted.get(fact_edited.fact_id)
        assert stale_edited is not None
        assert stale_edited.status == FactStatus.STALE
        assert "source hash changed" in stale_edited.stale_reason
        assert "ledger.py" in stale_edited.stale_reason
        assert stale_edited.source_hashes == {"ledger.py": orig_edited_hash}
        assert stale_edited.provenance.recorded_at == fact_edited.provenance.recorded_at

        # Deleting a bound source file must also mark its fact stale while keeping original hash/provenance.
        src_deleted.unlink()
        report = restarted.revalidate()
        assert report.stale == 2
        assert any(
            change.fact_id == fact_deleted.fact_id
            and change.new_status == FactStatus.STALE
            and change.missing_paths == ("legacy_adapter.py",)
            for change in report.changes
        )
        stale_deleted = restarted.get(fact_deleted.fact_id)
        assert stale_deleted is not None
        assert stale_deleted.status == FactStatus.STALE
        assert "missing source" in stale_deleted.stale_reason
        assert stale_deleted.source_hashes == {"legacy_adapter.py": orig_deleted_hash}


def test_atomic_rollback_and_read_only_mode(tmp_path):
    (tmp_path / "mod.py").write_text("VAL = 10\n", encoding="utf-8")
    db_path = tmp_path / DEFAULT_FACTS_FILENAME

    with open_project_fact_store(tmp_path, enabled=True) as store:
        fact = store.record_fact("Module exposes VAL as integer.", source_paths=["mod.py"])

        # Force a mid-transaction failure during add() and confirm prior state survives atomically.
        repo = store.repository
        assert isinstance(repo, SqliteFtsFactRepository)
        conn = repo._conn
        conn.execute(
            """
            CREATE TRIGGER fail_fact_sources_insert
            BEFORE INSERT ON fact_sources
            BEGIN
                SELECT RAISE(ABORT, 'simulated mid-tx failure');
            END;
            """
        )
        replacement = ProjectFact.confirmed(
            fact_id=fact.fact_id,
            worktree_id=store.worktree_id,
            statement="This update must roll back completely.",
            sources=fact.provenance.sources,
        )
        with pytest.raises(real_sqlite3.IntegrityError):
            repo.add(replacement)
        conn.execute("DROP TRIGGER fail_fact_sources_insert")

        # Original row and FTS index must remain intact after rollback.
        still_original = store.get(fact.fact_id)
        assert still_original is not None
        assert still_original.statement == "Module exposes VAL as integer."
        assert [f.fact_id for f in store.search("VAL integer")] == [fact.fact_id]

    # Read-only mode must allow reads and in-memory staleness checks without mutating DB bytes.
    before_bytes = db_path.read_bytes()
    with open_project_fact_store(tmp_path, enabled=True, read_only=True) as ro_store:
        assert ro_store.active
        assert [f.fact_id for f in ro_store.search("VAL integer")] == [fact.fact_id]
        with pytest.raises(FactStoreReadOnlyError):
            ro_store.record_fact("Should not write.", source_paths=["mod.py"])
        cmd = ro_store.handle_fact_command("Should not write via /fact.")
        assert not cmd.ok

        # Even if source changes on disk, read-only query filters out stale fact without editing DB.
        (tmp_path / "mod.py").write_text("VAL = 20\n", encoding="utf-8")
        assert ro_store.search("VAL integer") == ()
        stale_view = ro_store.search("VAL integer", include_stale=True)
        assert len(stale_view) == 1 and stale_view[0].is_stale

    assert db_path.read_bytes() == before_bytes


def test_parse_fact_command_args_and_path_guards_and_formatting(tmp_path):
    parsed = parse_fact_command_args(
        '-f pkg/api.py --file pkg/types.py -t contract --symbol parse --line 10:25 '
        '--note "issue #5" Return tuple is (first, last).'
    )
    assert parsed.source_paths == ("pkg/api.py", "pkg/types.py")
    assert parsed.tags == ("contract",)
    assert parsed.symbol == "parse"
    assert parsed.start_line == 10 and parsed.end_line == 25
    assert parsed.note == "issue #5"
    assert parsed.statement == "Return tuple is (first, last)."

    with pytest.raises(FactValidationError):
        parse_fact_command_args("")
    with pytest.raises(FactValidationError):
        parse_fact_command_args("--file ../escape.py bad path")
    with pytest.raises(FactValidationError):
        parse_fact_command_args("--file /etc/passwd bad path")

    # Symlink escaping worktree must be rejected.
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "link.py").symlink_to(outside)
    with pytest.raises(FactValidationError):
        hash_worktree_file(wt, "link.py")

    with pytest.raises(FactValidationError):
        SourceSnapshot(path="ok.py", sha256="not-a-sha256")
    with pytest.raises(FactValidationError):
        SourceSnapshot(path="ok.py", sha256="", start_line=10, end_line=5)

    (wt / "ok.py").write_text("OK = 1\n", encoding="utf-8")
    snaps = capture_source_snapshots(wt, ["ok.py"], start_line=1, end_line=1, symbol="OK")
    fact = ProjectFact.confirmed(
        worktree_id=worktree_scope_id(wt),
        statement="OK constant is always 1.",
        sources=snaps,
    )
    rendered = format_facts_context([fact], max_chars=500)
    assert "Read-only project facts" in rendered
    assert "OK constant is always 1." in rendered
    assert "ok.py:1(OK)@" in rendered
    assert len(format_facts_context([fact], max_chars=40)) <= 40


def test_shared_custom_db_isolates_revalidation_between_worktrees(tmp_path):
    wt_a = tmp_path / "worktree_a"
    wt_b = tmp_path / "worktree_b"
    wt_a.mkdir()
    wt_b.mkdir()
    shared_db = tmp_path / "shared.sqlite3"

    (wt_a / "api.py").write_text("VERSION = 1\n", encoding="utf-8")
    (wt_b / "api.py").write_text("VERSION = 1\n", encoding="utf-8")

    with (
        open_project_fact_store(wt_a, enabled=True, facts_db=shared_db) as store_a,
        open_project_fact_store(wt_b, enabled=True, facts_db=shared_db) as store_b,
    ):
        fact_a = store_a.record_fact("Worktree A uses schema v1.", source_paths=["api.py"])
        fact_b = store_b.record_fact("Worktree B uses schema v1.", source_paths=["api.py"])

        (wt_a / "api.py").write_text("VERSION = 2\n", encoding="utf-8")
        report_a = store_a.revalidate()
        report_b = store_b.revalidate()

        assert report_a.stale == 1 and report_a.active == 0
        assert report_b.stale == 0 and report_b.active == 1
        assert store_a.search("schema v1") == ()
        assert [f.fact_id for f in store_b.search("schema v1")] == [fact_b.fact_id]
        stale_a = store_a.get(fact_a.fact_id)
        assert stale_a is not None and stale_a.is_stale

