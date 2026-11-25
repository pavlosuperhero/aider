import hashlib
import json
import os
import shutil
import sqlite3 as real_sqlite3
import threading
from pathlib import Path

import pytest

from aider.history import ChatSummary
from aider.project_facts import (
    DEFAULT_FACTS_FILENAME,
    DEFAULT_FACTS_JSONL_FILENAME,
    MAX_TOP_K,
    FactProvenance,
    FactStatus,
    FactStoreError,
    FactStoreReadOnlyError,
    FactValidationError,
    FakeFactRepository,
    Fts5UnavailableError,
    JsonlFactRepository,
    ProjectFact,
    SourceSnapshot,
    SqliteFtsFactRepository,
    SqliteUnavailableError,
    VerificationState,
    capture_source_snapshots,
    chat_scope_id,
    format_facts_context,
    hash_worktree_file,
    open_project_fact_store,
    parse_fact_command_args,
    parse_facts_db_url,
    resolve_facts_db_path,
    resolve_facts_jsonl_path,
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
    if backend == "jsonl":
        return JsonlFactRepository(tmp_path / "facts.jsonl")
    if backend == "sqlite":
        return SqliteFtsFactRepository(tmp_path / "facts.sqlite3")
    raise ValueError(f"Unknown backend: {backend}")


def test_disabled_mode_does_not_open_or_create_store(tmp_path):
    default_jsonl = tmp_path / DEFAULT_FACTS_JSONL_FILENAME
    default_db = tmp_path / DEFAULT_FACTS_FILENAME
    custom_jsonl = tmp_path / "custom" / "facts.jsonl"
    custom_db = tmp_path / "custom" / "facts.sqlite3"

    with open_project_fact_store(tmp_path, enabled=False) as store:
        assert not store.enabled
        assert not store.available
        assert not store.active
        assert store.backend == "disabled"
        assert store.storage_path is None
        assert store.db_path is None
        assert store.chat_id == chat_scope_id(tmp_path)
        assert "disabled" in store.diagnostic.lower()
        assert store.search("anything") == ()
        assert store.list_for_paths(["app.py"]) == ()
        res = store.handle_fact_command("All timestamps must be UTC.")
        assert not res.ok
        assert "disabled" in res.message.lower()

    with open_project_fact_store(
        tmp_path, enabled=False, facts_db=custom_db, facts_file=custom_jsonl
    ) as store:
        assert not store.active
        assert store.backend == "disabled"

    assert not default_jsonl.exists()
    assert not default_db.exists()
    assert not custom_jsonl.exists()
    assert not custom_db.exists()
    assert not custom_db.parent.exists()


def test_default_jsonl_custom_file_and_db_url_routing(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert resolve_facts_jsonl_path(tmp_path) == (
        tmp_path / DEFAULT_FACTS_JSONL_FILENAME
    ).resolve()
    assert resolve_facts_jsonl_path(tmp_path, "sub/custom.jsonl") == (
        tmp_path / "sub" / "custom.jsonl"
    ).resolve()
    assert resolve_facts_db_path(tmp_path) == (tmp_path / DEFAULT_FACTS_FILENAME).resolve()
    assert resolve_facts_db_path(tmp_path, "sub/custom.sqlite3") == (
        tmp_path / "sub" / "custom.sqlite3"
    ).resolve()

    abs_url_target = (tmp_path / "url_abs" / "facts.sqlite3").resolve()
    assert parse_facts_db_url(tmp_path, f"sqlite:///{abs_url_target.as_posix().lstrip('/')}") == (
        abs_url_target
    )
    assert parse_facts_db_url(tmp_path, "sqlite://rel/facts.sqlite3") == (
        tmp_path / "rel" / "facts.sqlite3"
    ).resolve()
    assert parse_facts_db_url(tmp_path, "plain/facts.sqlite3") == (
        tmp_path / "plain" / "facts.sqlite3"
    ).resolve()

    for bad_url in (
        "",
        "   ",
        ":memory:",
        "sqlite:///:memory:",
        "sqlite://:memory:",
        "sqlite://",
        "sqlite:///",
        "postgres://user:pass@localhost/aider",
        "postgresql://localhost/aider",
        "mysql://localhost/aider",
        "http://example.com/facts.db",
    ):
        with pytest.raises(FactValidationError):
            parse_facts_db_url(tmp_path, bad_url)

    # Default enabled store uses .aider.facts.jsonl with deterministic chat hash ID.
    with open_project_fact_store(tmp_path, enabled=True) as default_store:
        assert default_store.active
        assert default_store.backend == "jsonl"
        assert default_store.storage_path == (tmp_path / DEFAULT_FACTS_JSONL_FILENAME).resolve()
        assert default_store.db_path == default_store.storage_path
        expected_chat_id = chat_scope_id(tmp_path)
        assert default_store.chat_id == expected_chat_id
        assert expected_chat_id.startswith("chat:")
        assert len(expected_chat_id) == len("chat:") + 16
        recorded = default_store.record_fact("Use UTC everywhere.", source_paths=["app.py"])
        assert recorded.provenance.chat_id == expected_chat_id
    jsonl_file = tmp_path / DEFAULT_FACTS_JSONL_FILENAME
    assert jsonl_file.is_file()
    assert not (tmp_path / DEFAULT_FACTS_FILENAME).exists()
    raw_lines = [line for line in jsonl_file.read_text(encoding="utf-8").splitlines() if line]
    assert len(raw_lines) == 1
    parsed_line = json.loads(raw_lines[0])
    assert parsed_line["chat_id"] == expected_chat_id
    assert parsed_line["provenance"]["chat_id"] == expected_chat_id
    assert parsed_line["statement"] == "Use UTC everywhere."

    # Custom JSONL path via facts_file.
    custom_jsonl = tmp_path / "alt_jsonl" / "custom.jsonl"
    with open_project_fact_store(
        tmp_path, enabled=True, facts_file=custom_jsonl
    ) as custom_jsonl_store:
        assert custom_jsonl_store.active
        assert custom_jsonl_store.backend == "jsonl"
        assert custom_jsonl_store.storage_path == custom_jsonl.resolve()
        custom_jsonl_store.record_fact("Custom JSONL fact.", source_paths=["app.py"])
    assert custom_jsonl.is_file()

    # Plain SQLite path via facts_db.
    custom_path = tmp_path / "alt_dir" / "custom_facts.sqlite3"
    with open_project_fact_store(tmp_path, enabled=True, facts_db=custom_path) as custom_store:
        assert custom_store.active
        assert custom_store.backend == "sqlite"
        assert custom_store.storage_path == custom_path.resolve()
        assert custom_store.db_path == custom_path.resolve()
        fact = custom_store.record_fact(
            "Always validate inputs at boundary.", source_paths=["app.py"]
        )
        assert fact.provenance.chat_id == custom_store.chat_id
    assert custom_path.is_file()

    # sqlite:// URL routing (absolute and relative).
    abs_url = f"sqlite:///{abs_url_target.as_posix().lstrip('/')}"
    with open_project_fact_store(tmp_path, enabled=True, facts_db=abs_url) as url_store:
        assert url_store.active
        assert url_store.backend == "sqlite"
        assert url_store.storage_path == abs_url_target
        url_store.record_fact("Opened via sqlite:/// URL.", source_paths=["app.py"])
    assert abs_url_target.is_file()

    with open_project_fact_store(
        tmp_path, enabled=True, facts_db="sqlite://rel_url/facts.sqlite3"
    ) as rel_url_store:
        assert rel_url_store.active
        assert rel_url_store.backend == "sqlite"
        assert rel_url_store.storage_path == (tmp_path / "rel_url" / "facts.sqlite3").resolve()
    assert (tmp_path / "rel_url" / "facts.sqlite3").is_file()

    # Unsupported URL scheme or :memory: returns inactive store and creates no file.
    isolated = tmp_path / "isolated_bad_url"
    isolated.mkdir()
    for bad_db in ("postgresql://localhost/aider", "mysql://localhost/aider", ":memory:"):
        with open_project_fact_store(isolated, enabled=True, facts_db=bad_db) as bad_store:
            assert bad_store.enabled
            assert not bad_store.available
            assert not bad_store.active
            assert bad_store.storage_path is None
            assert bad_store.diagnostic
            assert bad_store.search("anything") == ()
    assert list(isolated.iterdir()) == []


def test_chat_scope_id_determinism_and_provenance_serialization(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    link = tmp_path / "repo_link"
    link.symlink_to(repo)

    id_default = chat_scope_id(repo)
    id_explicit = chat_scope_id(repo, repo / ".aider.chat.history.md")
    id_relative = chat_scope_id(repo, ".aider.chat.history.md")
    id_symlink = chat_scope_id(link)
    id_other_history = chat_scope_id(repo, "sessions/chat-b.md")
    other_repo = tmp_path / "other_repo"
    other_repo.mkdir()
    id_other_repo = chat_scope_id(other_repo)

    assert id_default == id_explicit == id_relative == id_symlink
    assert id_default != id_other_history
    assert id_default != id_other_repo
    assert id_default.startswith("chat:") and len(id_default) == 21

    (repo / "mod.py").write_text("X = 1\n", encoding="utf-8")
    snaps = capture_source_snapshots(repo, ["mod.py"])
    fact = ProjectFact.confirmed(
        worktree_id=worktree_scope_id(repo),
        statement="Round-trip test.",
        sources=snaps,
        chat_id=id_default,
    )
    payload = fact.to_dict()
    assert payload["chat_id"] == id_default
    assert payload["provenance"]["chat_id"] == id_default
    assert ProjectFact.from_dict(payload) == fact

    # Backwards-compatible provenance dict without chat_id defaults to "".
    legacy_prov = FactProvenance.from_dict(
        {
            "origin": "user:/fact",
            "worktree_id": worktree_scope_id(repo),
            "recorded_at": "2026-01-01T00:00:00Z",
            "sources": [],
        }
    )
    assert legacy_prov.chat_id == ""

    rendered = format_facts_context([fact])
    assert f"chat={id_default}" in rendered


def test_default_jsonl_works_without_python_sqlite_and_sqlite_works_without_cli(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", "")
    assert shutil.which("sqlite3") is None

    (tmp_path / "service.py").write_text("def handle():\n    return 'ok'\n", encoding="utf-8")

    # Default JSONL works even when Python's sqlite3 module is completely unavailable.
    with monkeypatch.context() as mp:
        mp.setattr("aider.project_facts.sqlite3", None)
        with open_project_fact_store(tmp_path, enabled=True) as jsonl_store:
            assert jsonl_store.active
            assert jsonl_store.backend == "jsonl"
            fact = jsonl_store.record_fact(
                "Service handlers return normalized ISO timestamps.",
                source_paths=["service.py"],
                tags=["convention"],
            )
            assert fact.provenance.chat_id == jsonl_store.chat_id
            found = jsonl_store.search("normalized ISO timestamps")
            assert [f.fact_id for f in found] == [fact.fact_id]
            by_path = jsonl_store.list_for_paths(["service.py"])
            assert [f.fact_id for f in by_path] == [fact.fact_id]
        assert (tmp_path / DEFAULT_FACTS_JSONL_FILENAME).is_file()
        assert not (tmp_path / DEFAULT_FACTS_FILENAME).exists()

    # Explicit --facts-db uses embedded sqlite3 without needing the standalone CLI executable.
    db_path = tmp_path / DEFAULT_FACTS_FILENAME
    with open_project_fact_store(tmp_path, enabled=True, facts_db=db_path) as sqlite_store:
        assert sqlite_store.active
        assert sqlite_store.backend == "sqlite"
        fact = sqlite_store.record_fact(
            "Service handlers return normalized ISO timestamps.",
            source_paths=["service.py"],
            tags=["convention"],
        )
        found = sqlite_store.search("normalized ISO timestamps")
        assert [f.fact_id for f in found] == [fact.fact_id]
        by_path = sqlite_store.list_for_paths(["service.py"])
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

    with open_project_fact_store(tmp_path, enabled=True, facts_db=existing_db) as store:
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
    assert not (tmp_path / DEFAULT_FACTS_JSONL_FILENAME).exists()
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

    with open_project_fact_store(
        tmp_path, enabled=True, facts_db=existing_db, sqlite_module=no_fts5
    ) as store:
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
    assert not (tmp_path / DEFAULT_FACTS_JSONL_FILENAME).exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before_files


@pytest.mark.parametrize("backend", ["fake", "jsonl", "sqlite"])
def test_repository_add_search_list_for_paths_and_provenance(backend, tmp_path):
    wt_root = tmp_path / "repo"
    wt_root.mkdir()
    (wt_root / "pkg").mkdir()
    (wt_root / "pkg" / "pricing.py").write_text("def cents(v): return int(v * 100)\n")
    (wt_root / "pkg" / "auth.py").write_text("def verify(tok): return bool(tok)\n")
    wt_id = worktree_scope_id(wt_root)
    cid = chat_scope_id(wt_root)

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
            chat_id=cid,
        )
        f_auth = ProjectFact.confirmed(
            worktree_id=wt_id,
            statement="Session tokens are validated strictly before route dispatch.",
            sources=auth_snaps,
            origin="user:/fact",
            tags=("security",),
            chat_id=cid,
        )
        f_candidate = ProjectFact.model_candidate(
            worktree_id=wt_id,
            statement="Monetary rounding uses banker's rounding in pricing.",
            sources=pricing_snaps,
            chat_id=cid,
        )

        repo.add(f_pricing)
        repo.add(f_auth)
        repo.add(f_candidate)

        fetched = repo.get(f_pricing.fact_id, worktree_id=wt_id)
        assert fetched == f_pricing
        assert fetched.provenance.commit_sha == "abc1234"
        assert fetched.provenance.note == "Confirmed in billing review"
        assert fetched.provenance.chat_id == cid
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


@pytest.mark.parametrize("backend", ["fake", "jsonl", "sqlite"])
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


@pytest.mark.parametrize("backend", ["jsonl", "sqlite"])
def test_compaction_restart_provenance_survival_and_source_invalidation(backend, tmp_path):
    (
        "Record a decision, drop it during ChatSummary compaction, "
        "restart, then edit/delete sources."
    )
    src_edited = tmp_path / "ledger.py"
    src_deleted = tmp_path / "legacy_adapter.py"
    src_edited.write_text("def balance_cents(entries): return sum(entries)\n", encoding="utf-8")
    src_deleted.write_text("def legacy_export(): return []\n", encoding="utf-8")

    orig_edited_hash = hashlib.sha256(src_edited.read_bytes()).hexdigest()
    orig_deleted_hash = hashlib.sha256(src_deleted.read_bytes()).hexdigest()
    assert hash_worktree_file(tmp_path, "ledger.py") == orig_edited_hash

    open_kwargs = (
        {"facts_db": tmp_path / DEFAULT_FACTS_FILENAME} if backend == "sqlite" else {}
    )
    with open_project_fact_store(tmp_path, enabled=True, **open_kwargs) as store:
        assert store.backend == backend
        expected_chat_id = store.chat_id
        cmd_edited = store.handle_fact_command(
            "--file ledger.py --tag billing --symbol balance_cents "
            "Ledger balances must always be computed in integer cents.",
            commit_sha="d2f5965",
        )
        assert cmd_edited.ok and cmd_edited.fact is not None
        fact_edited = cmd_edited.fact
        assert fact_edited.provenance.chat_id == expected_chat_id

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
            {
                "role": "user",
                "content": f"Turn {turn}: refactor unrelated UI widgets " + ("word " * 12),
            }
        )
        history.append(
            {
                "role": "assistant",
                "content": f"Turn {turn}: updated unrelated UI widgets " + ("done " * 12),
            }
        )

    summarizer = ChatSummary([_LossyFakeModel()], max_tokens=60)
    assert summarizer.too_big(history)
    compacted = summarizer.summarize(history)
    compacted_text = "\n".join(m["content"] for m in compacted)
    assert "integer cents" not in compacted_text

    # Restart: open a fresh store instance pointing at the same worktree store.
    with open_project_fact_store(tmp_path, enabled=True, **open_kwargs) as restarted:
        assert restarted.backend == backend
        recalled = restarted.search("integer cents")
        assert len(recalled) == 1
        survived = recalled[0]
        assert survived.fact_id == fact_edited.fact_id
        assert survived.is_confirmed
        assert survived.is_active
        assert survived.source_hashes == {"ledger.py": orig_edited_hash}
        assert survived.provenance.origin == "user:/fact"
        assert survived.provenance.chat_id == expected_chat_id
        assert survived.provenance.recorded_at == fact_edited.provenance.recorded_at
        assert survived.provenance.commit_sha == "d2f5965"
        assert survived.provenance.sources[0].symbol == "balance_cents"

        # Editing the bound source file must mark the fact stale while keeping
        # original hash/provenance.
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
        assert stale_edited.provenance.chat_id == expected_chat_id
        assert stale_edited.provenance.recorded_at == fact_edited.provenance.recorded_at

        # Deleting a bound source file must also mark its fact stale while keeping
        # original hash/provenance.
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


def test_atomic_rollback_and_read_only_mode(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("VAL = 10\n", encoding="utf-8")
    db_path = tmp_path / DEFAULT_FACTS_FILENAME

    with open_project_fact_store(tmp_path, enabled=True, facts_db=db_path) as store:
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
    with open_project_fact_store(
        tmp_path, enabled=True, facts_db=db_path, read_only=True
    ) as ro_store:
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

    # JSONL atomic write preserves existing file on replace error and cleans temp file.
    jsonl_dir = tmp_path / "jsonl_atomic"
    jsonl_dir.mkdir()
    (jsonl_dir / "mod.py").write_text("VAL = 10\n", encoding="utf-8")
    jsonl_path = jsonl_dir / DEFAULT_FACTS_JSONL_FILENAME
    with open_project_fact_store(jsonl_dir, enabled=True) as jstore:
        jfact = jstore.record_fact("Original JSONL fact.", source_paths=["mod.py"])
        original_jsonl_bytes = jsonl_path.read_bytes()

        def _fail_replace(src, dst):
            raise OSError("simulated atomic replace failure")

        monkeypatch.setattr(os, "replace", _fail_replace)
        with pytest.raises(FactStoreError):
            jstore.record_fact("Should fail atomically.", source_paths=["mod.py"])

    assert jsonl_path.read_bytes() == original_jsonl_bytes
    assert [p.name for p in jsonl_dir.iterdir() if p.name.endswith(".tmp")] == []
    with open_project_fact_store(jsonl_dir, enabled=True, read_only=True) as ro_jsonl:
        assert ro_jsonl.active
        assert [f.fact_id for f in ro_jsonl.search("Original JSONL")] == [jfact.fact_id]
        with pytest.raises(FactStoreReadOnlyError):
            ro_jsonl.record_fact("Blocked in read-only.", source_paths=["mod.py"])
    assert jsonl_path.read_bytes() == original_jsonl_bytes


def test_jsonl_malformed_and_missing_read_only_semantics(tmp_path):
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(FactStoreError):
        JsonlFactRepository(missing, read_only=True)
    assert not missing.exists()

    with open_project_fact_store(
        tmp_path, enabled=True, facts_file=missing, read_only=True
    ) as ro_missing:
        assert not ro_missing.active
        assert ro_missing.backend == "jsonl"
        assert "read-only" in ro_missing.diagnostic.lower()
    assert not missing.exists()

    bad_json = tmp_path / "bad.jsonl"
    bad_json.write_text("{not valid json\n", encoding="utf-8")
    with pytest.raises(FactStoreError):
        JsonlFactRepository(bad_json)

    non_obj = tmp_path / "non_obj.jsonl"
    non_obj.write_text('["not", "an", "object"]\n', encoding="utf-8")
    with pytest.raises(FactStoreError):
        JsonlFactRepository(non_obj)


def test_sqlite_legacy_schema_migration_and_read_only_missing_chat_id(tmp_path):
    legacy_db = tmp_path / "legacy.sqlite3"
    wt_id = worktree_scope_id(tmp_path)
    conn = real_sqlite3.connect(str(legacy_db))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE facts (
            worktree_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            statement TEXT NOT NULL,
            verification TEXT NOT NULL,
            status TEXT NOT NULL,
            origin TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            commit_sha TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            stale_reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (worktree_id, fact_id)
        );
        CREATE TABLE fact_sources (
            worktree_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            symbol TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (worktree_id, fact_id, seq)
        );
        CREATE VIRTUAL TABLE facts_fts USING fts5(
            worktree_id UNINDEXED,
            fact_id UNINDEXED,
            statement,
            tags,
            paths,
            symbols,
            tokenize = 'unicode61'
        );
        """
    )
    conn.execute(
        """
        INSERT INTO facts (
            worktree_id, fact_id, statement, verification, status,
            origin, recorded_at, updated_at, commit_sha, note, tags_json, stale_reason
        ) VALUES (?, ?, ?, 'confirmed', 'active', 'user:/fact',
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '', '', '[]', '')
        """,
        (wt_id, "legacy_1", "Legacy SQLite fact without chat_id column."),
    )
    conn.execute(
        "INSERT INTO facts_fts VALUES (?, ?, ?, '', '', '')",
        (wt_id, "legacy_1", "Legacy SQLite fact without chat_id column."),
    )
    conn.commit()
    conn.close()

    # Read-only open tolerates missing chat_id column as "" without modifying DB bytes.
    before_bytes = legacy_db.read_bytes()
    with open_project_fact_store(
        tmp_path, enabled=True, facts_db=legacy_db, read_only=True
    ) as ro_store:
        assert ro_store.active
        got = ro_store.get("legacy_1")
        assert got is not None
        assert got.provenance.chat_id == ""
        found = ro_store.search("Legacy SQLite")
        assert [f.fact_id for f in found] == ["legacy_1"]
    assert legacy_db.read_bytes() == before_bytes

    # Writable open runs safe idempotent ALTER TABLE migration and stores new chat_id.
    with open_project_fact_store(tmp_path, enabled=True, facts_db=legacy_db) as rw_store:
        assert rw_store.active
        got = rw_store.get("legacy_1")
        assert got is not None and got.provenance.chat_id == ""
        added = rw_store.record_fact("Post-migration fact with chat_id.")
        assert added.provenance.chat_id == rw_store.chat_id

    # Re-opening again is idempotent.
    with open_project_fact_store(tmp_path, enabled=True, facts_db=legacy_db) as reopened:
        assert reopened.get("legacy_1") is not None
        got_new = reopened.get(added.fact_id)
        assert got_new is not None and got_new.provenance.chat_id == reopened.chat_id


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


@pytest.mark.parametrize("backend", ["jsonl", "sqlite"])
def test_shared_custom_store_isolates_revalidation_between_worktrees(backend, tmp_path):
    wt_a = tmp_path / "worktree_a"
    wt_b = tmp_path / "worktree_b"
    wt_a.mkdir()
    wt_b.mkdir()
    shared_path = tmp_path / (f"shared.{backend}")

    (wt_a / "api.py").write_text("VERSION = 1\n", encoding="utf-8")
    (wt_b / "api.py").write_text("VERSION = 1\n", encoding="utf-8")

    open_kwargs = (
        {"facts_db": shared_path} if backend == "sqlite" else {"facts_file": shared_path}
    )
    with (
        open_project_fact_store(wt_a, enabled=True, **open_kwargs) as store_a,
        open_project_fact_store(wt_b, enabled=True, **open_kwargs) as store_b,
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


def test_jsonl_readonly_operations_do_not_create_lock_file(tmp_path):
    (tmp_path / "app.py").write_text("X = 1\n", encoding="utf-8")
    jsonl_path = tmp_path / DEFAULT_FACTS_JSONL_FILENAME
    lock_path = Path(str(jsonl_path) + ".lock")
    wt_id = worktree_scope_id(tmp_path)
    snaps = capture_source_snapshots(tmp_path, ["app.py"])

    seed_fact = ProjectFact.confirmed(
        worktree_id=wt_id,
        statement="Read-only operations must not create lock files.",
        sources=snaps,
    )
    jsonl_path.write_text(
        json.dumps(seed_fact.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    assert not lock_path.exists()

    for read_only in (True, False):
        repo = JsonlFactRepository(jsonl_path, read_only=read_only)
        assert repo.get(seed_fact.fact_id, worktree_id=wt_id) == seed_fact
        assert [f.fact_id for f in repo.search("lock files", worktree_id=wt_id)] == [
            seed_fact.fact_id
        ]
        assert [f.fact_id for f in repo.list_for_paths(["app.py"], worktree_id=wt_id)] == [
            seed_fact.fact_id
        ]
        assert [f.fact_id for f in repo.list_all(worktree_id=wt_id)] == [seed_fact.fact_id]
        assert not lock_path.exists()


def test_jsonl_add_and_update_status_create_missing_parent_directory(tmp_path):
    (tmp_path / "app.py").write_text("X = 1\n", encoding="utf-8")
    wt_id = worktree_scope_id(tmp_path)
    snaps = capture_source_snapshots(tmp_path, ["app.py"])

    nested_jsonl = tmp_path / "new_subdir" / "nested" / DEFAULT_FACTS_JSONL_FILENAME
    lock_path = Path(str(nested_jsonl) + ".lock")
    assert not nested_jsonl.parent.exists()

    repo = JsonlFactRepository(nested_jsonl)
    fact_a = ProjectFact.confirmed(
        worktree_id=wt_id,
        statement="Nested JSONL store creates parent directory cleanly.",
        sources=snaps,
    )
    repo.add(fact_a)
    assert nested_jsonl.parent.is_dir()
    assert nested_jsonl.is_file()
    assert lock_path.is_file()
    assert JsonlFactRepository(nested_jsonl).get(fact_a.fact_id, worktree_id=wt_id) == fact_a

    # Even if the parent directory is removed before add(), add() recreates it before locking.
    shutil.rmtree(tmp_path / "new_subdir")
    assert not nested_jsonl.parent.exists()
    fact_b = ProjectFact.confirmed(
        worktree_id=wt_id,
        statement="Recreated directory before acquiring write lock.",
        sources=snaps,
    )
    repo.add(fact_b)
    assert nested_jsonl.parent.is_dir()
    assert nested_jsonl.is_file()
    assert lock_path.is_file()
    assert JsonlFactRepository(nested_jsonl).get(fact_b.fact_id, worktree_id=wt_id) == fact_b

    # Same guarantee for update_status() when parent directory does not yet exist.
    shutil.rmtree(tmp_path / "new_subdir")
    assert not nested_jsonl.parent.exists()
    result = repo.update_status(
        "nonexistent_fact",
        worktree_id=wt_id,
        status=FactStatus.STALE,
        stale_reason="missing dir check",
    )
    assert result is None
    assert nested_jsonl.parent.is_dir()
    assert lock_path.is_file()


def test_jsonl_concurrent_repository_and_store_writes_prevent_lost_update(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    wt_id = worktree_scope_id(tmp_path)
    snaps = capture_source_snapshots(tmp_path, ["app.py"])

    # 1. Two separate JsonlFactRepository instances writing concurrently to the same file.
    jsonl_path = tmp_path / DEFAULT_FACTS_JSONL_FILENAME
    repo_a = JsonlFactRepository(jsonl_path)
    repo_b = JsonlFactRepository(jsonl_path)

    fact_a = ProjectFact.confirmed(
        fact_id="fact_concurrent_a",
        worktree_id=wt_id,
        statement="Alpha writer confirmed invariant.",
        sources=snaps,
    )
    fact_b = ProjectFact.confirmed(
        fact_id="fact_concurrent_b",
        worktree_id=wt_id,
        statement="Beta writer confirmed invariant.",
        sources=snaps,
    )

    start_barrier = threading.Barrier(2)
    a_in_load = threading.Event()
    b_in_load = threading.Event()
    events = []
    events_lock = threading.Lock()
    overlap_detected = False

    orig_load_a = repo_a._load_facts
    orig_write_a = repo_a._write_facts
    orig_load_b = repo_b._load_facts
    orig_write_b = repo_b._write_facts

    def wrapped_load_a():
        nonlocal overlap_detected
        loaded = orig_load_a()
        with events_lock:
            events.append("a_load")
        a_in_load.set()
        # Without FileLock serialization, repo_b would enter _load_facts immediately here
        # and read the same empty snapshot, causing a lost update.
        if b_in_load.wait(timeout=0.15):
            overlap_detected = True
        return loaded

    def wrapped_write_a(facts):
        orig_write_a(facts)
        with events_lock:
            events.append("a_write")

    def wrapped_load_b():
        loaded = orig_load_b()
        with events_lock:
            events.append("b_load")
        b_in_load.set()
        return loaded

    def wrapped_write_b(facts):
        orig_write_b(facts)
        with events_lock:
            events.append("b_write")

    repo_a._load_facts = wrapped_load_a
    repo_a._write_facts = wrapped_write_a
    repo_b._load_facts = wrapped_load_b
    repo_b._write_facts = wrapped_write_b

    errors = []

    def run_a():
        try:
            start_barrier.wait(timeout=5.0)
            repo_a.add(fact_a)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def run_b():
        try:
            start_barrier.wait(timeout=5.0)
            assert a_in_load.wait(timeout=5.0)
            repo_b.add(fact_b)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t_a = threading.Thread(target=run_a)
    t_b = threading.Thread(target=run_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10.0)
    t_b.join(timeout=10.0)

    assert not t_a.is_alive() and not t_b.is_alive()
    assert errors == []
    assert overlap_detected is False
    assert events == ["a_load", "a_write", "b_load", "b_write"]

    reopened_repo = JsonlFactRepository(jsonl_path)
    assert reopened_repo.get(fact_a.fact_id, worktree_id=wt_id) == fact_a
    assert reopened_repo.get(fact_b.fact_id, worktree_id=wt_id) == fact_b

    # 2. Two separate ProjectFactStore instances writing distinct confirmed facts concurrently.
    store_root = tmp_path / "concurrent_stores"
    store_root.mkdir()
    (store_root / "svc.py").write_text("MODE = 'prod'\n", encoding="utf-8")

    with (
        open_project_fact_store(store_root, enabled=True) as store_a,
        open_project_fact_store(store_root, enabled=True) as store_b,
    ):
        s_barrier = threading.Barrier(2)
        s_a_in_load = threading.Event()
        s_b_in_load = threading.Event()
        s_overlap = False

        s_orig_load_a = store_a.repository._load_facts
        s_orig_load_b = store_b.repository._load_facts

        def s_wrapped_load_a():
            nonlocal s_overlap
            loaded = s_orig_load_a()
            s_a_in_load.set()
            if s_b_in_load.wait(timeout=0.15):
                s_overlap = True
            return loaded

        def s_wrapped_load_b():
            loaded = s_orig_load_b()
            s_b_in_load.set()
            return loaded

        store_a.repository._load_facts = s_wrapped_load_a
        store_b.repository._load_facts = s_wrapped_load_b

        store_errors = []
        recorded_ids = {}

        def write_store_a():
            try:
                s_barrier.wait(timeout=5.0)
                fact = store_a.record_fact(
                    "Store A confirmed fact for concurrent durability.",
                    source_paths=["svc.py"],
                )
                recorded_ids["a"] = fact.fact_id
            except Exception as exc:  # pragma: no cover
                store_errors.append(exc)

        def write_store_b():
            try:
                s_barrier.wait(timeout=5.0)
                assert s_a_in_load.wait(timeout=5.0)
                fact = store_b.record_fact(
                    "Store B confirmed fact for concurrent durability.",
                    source_paths=["svc.py"],
                )
                recorded_ids["b"] = fact.fact_id
            except Exception as exc:  # pragma: no cover
                store_errors.append(exc)

        st_a = threading.Thread(target=write_store_a)
        st_b = threading.Thread(target=write_store_b)
        st_a.start()
        st_b.start()
        st_a.join(timeout=10.0)
        st_b.join(timeout=10.0)

        assert not st_a.is_alive() and not st_b.is_alive()
        assert store_errors == []
        assert s_overlap is False

    with open_project_fact_store(store_root, enabled=True) as reopened_store:
        found = reopened_store.search("concurrent durability")
        assert {f.fact_id for f in found} == {recorded_ids["a"], recorded_ids["b"]}
        assert reopened_store.get(recorded_ids["a"]).is_confirmed
        assert reopened_store.get(recorded_ids["b"]).is_confirmed


def test_jsonl_concurrent_add_and_update_status_preserves_both(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    jsonl_path = tmp_path / DEFAULT_FACTS_JSONL_FILENAME
    wt_id = worktree_scope_id(tmp_path)
    snaps = capture_source_snapshots(tmp_path, ["app.py"])

    fact_existing = ProjectFact.confirmed(
        fact_id="fact_existing",
        worktree_id=wt_id,
        statement="Existing fact whose status will transition.",
        sources=snaps,
    )
    fact_new = ProjectFact.confirmed(
        fact_id="fact_added_concurrently",
        worktree_id=wt_id,
        statement="Newly added fact during concurrent status update.",
        sources=snaps,
    )
    JsonlFactRepository(jsonl_path).add(fact_existing)

    repo_status = JsonlFactRepository(jsonl_path)
    repo_add = JsonlFactRepository(jsonl_path)

    barrier = threading.Barrier(2)
    status_in_load = threading.Event()
    add_in_load = threading.Event()
    overlap = False

    orig_status_load = repo_status._load_facts
    orig_add_load = repo_add._load_facts

    def wrapped_status_load():
        nonlocal overlap
        loaded = orig_status_load()
        status_in_load.set()
        if add_in_load.wait(timeout=0.15):
            overlap = True
        return loaded

    def wrapped_add_load():
        loaded = orig_add_load()
        add_in_load.set()
        return loaded

    repo_status._load_facts = wrapped_status_load
    repo_add._load_facts = wrapped_add_load

    errors = []

    def run_status():
        try:
            barrier.wait(timeout=5.0)
            updated = repo_status.update_status(
                fact_existing.fact_id,
                worktree_id=wt_id,
                status=FactStatus.STALE,
                stale_reason="source hash changed",
            )
            assert updated is not None and updated.status == FactStatus.STALE
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def run_add():
        try:
            barrier.wait(timeout=5.0)
            assert status_in_load.wait(timeout=5.0)
            repo_add.add(fact_new)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t_status = threading.Thread(target=run_status)
    t_add = threading.Thread(target=run_add)
    t_status.start()
    t_add.start()
    t_status.join(timeout=10.0)
    t_add.join(timeout=10.0)

    assert not t_status.is_alive() and not t_add.is_alive()
    assert errors == []
    assert overlap is False

    reopened = JsonlFactRepository(jsonl_path)
    recovered_existing = reopened.get(fact_existing.fact_id, worktree_id=wt_id)
    recovered_new = reopened.get(fact_new.fact_id, worktree_id=wt_id)
    assert recovered_existing is not None
    assert recovered_existing.status == FactStatus.STALE
    assert recovered_existing.stale_reason == "source hash changed"
    assert recovered_new == fact_new


def test_jsonl_write_lock_errors_chain_to_fact_store_error(tmp_path, monkeypatch):
    from filelock import Timeout as FileLockTimeout

    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    jsonl_path = tmp_path / DEFAULT_FACTS_JSONL_FILENAME
    wt_id = worktree_scope_id(tmp_path)
    snaps = capture_source_snapshots(tmp_path, ["app.py"])
    fact = ProjectFact.confirmed(
        worktree_id=wt_id,
        statement="Lock failures must raise chained FactStoreError.",
        sources=snaps,
    )

    repo = JsonlFactRepository(jsonl_path)

    def _raise_timeout(*args, **kwargs):
        raise FileLockTimeout(str(jsonl_path) + ".lock")

    monkeypatch.setattr(repo._write_lock, "acquire", _raise_timeout)
    with pytest.raises(FactStoreError, match="Failed to acquire write lock") as exc_info:
        repo.add(fact)
    assert isinstance(exc_info.value.__cause__, FileLockTimeout)

    def _raise_oserror(*args, **kwargs):
        raise PermissionError("lock directory read-only")

    monkeypatch.setattr(repo._write_lock, "acquire", _raise_oserror)
    with pytest.raises(FactStoreError, match="Failed to acquire write lock") as status_exc:
        repo.update_status(
            fact.fact_id,
            worktree_id=wt_id,
            status=FactStatus.STALE,
            stale_reason="changed",
        )
    assert isinstance(status_exc.value.__cause__, PermissionError)
