"""Worktree-scoped persistent project facts with JSONL default and SQLite FTS5 routing.

Architecture (Repository Pattern / Clean Architecture):
- Pure domain value objects (`SourceSnapshot`, `FactProvenance`, `ProjectFact`) and
  validation/formatting helpers have zero dependency on SQLite, Coder, or git CLI.
- `FactRepository` defines the persistence port (`add`, `search`, `list_for_paths`,
  `get`, `list_all`, `update_status`) with three interchangeable implementations:
  `FakeFactRepository` (in-memory), `JsonlFactRepository` (UTF-8
  JSONL with chat hash ID and atomic rewrites), and `SqliteFtsFactRepository`
  (embedded Python `sqlite3` + FTS5 with atomic transactions and initial DB creation).
- `ProjectFactStore` / `open_project_fact_store` orchestrates worktree isolation,
  chat hash ID provenance, source-hash capture and revalidation, explicit `/fact`
  confirmation, unverified model-candidate separation, read-only enforcement, and
  graceful degradation when disabled, given an unsupported URL, or when Python
  `sqlite3` / FTS5 is unavailable for `--facts-db`.
"""

from __future__ import annotations

import abc
import errno
import hashlib
import json
import os
import posixpath
import re
import shlex
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

from filelock import FileLock, Timeout as FileLockTimeout

try:
    import sqlite3  # type: ignore[assignment]
except ImportError:  # pragma: no cover - exercised via monkeypatching in tests
    sqlite3 = None  # type: ignore[assignment]

DEFAULT_FACTS_JSONL_FILENAME = ".aider.facts.jsonl"
DEFAULT_FACTS_FILENAME = ".aider.facts.sqlite3"
DEFAULT_TOP_K = 5
MAX_TOP_K = 20
MAX_FACT_STATEMENT_CHARS = 2000
MAX_PROMPT_FACTS_CHARS = 4000
SCHEMA_VERSION = 2

_HASH_CHUNK_BYTES = 65536
_MAX_QUERY_TOKENS = 32
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_UNSET = object()

_MODEL_ORIGIN_PREFIXES = ("model", "llm", "assistant", "candidate")

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS facts (
        worktree_id TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        statement TEXT NOT NULL,
        verification TEXT NOT NULL CHECK (verification IN ('confirmed', 'unverified')),
        status TEXT NOT NULL CHECK (status IN ('active', 'stale', 'invalidated')),
        origin TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        commit_sha TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        tags_json TEXT NOT NULL DEFAULT '[]',
        stale_reason TEXT NOT NULL DEFAULT '',
        chat_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (worktree_id, fact_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_sources (
        worktree_id TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        start_line INTEGER,
        end_line INTEGER,
        symbol TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (worktree_id, fact_id, seq),
        FOREIGN KEY (worktree_id, fact_id)
            REFERENCES facts (worktree_id, fact_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fact_sources_lookup
        ON fact_sources (worktree_id, path, fact_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_facts_worktree_status
        ON facts (worktree_id, status, verification, updated_at)
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
        worktree_id UNINDEXED,
        fact_id UNINDEXED,
        statement,
        tags,
        paths,
        symbols,
        tokenize = 'unicode61'
    )
    """,
)


class FactStoreError(RuntimeError):
    """Base error for project fact persistence failures."""


class FactValidationError(ValueError):
    """Raised when a fact, provenance field, or path violates domain invariants."""


class FactStoreReadOnlyError(FactStoreError):
    """Raised when attempting to mutate a read-only fact store or repository."""


class SqliteUnavailableError(FactStoreError):
    """Raised when Python's embedded sqlite3 driver is unavailable."""


class Fts5UnavailableError(FactStoreError):
    """Raised when SQLite's FTS5 extension is unavailable."""


class FactStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    INVALIDATED = "invalidated"


class VerificationState(str, Enum):
    CONFIRMED = "confirmed"
    UNVERIFIED = "unverified"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_fact_id() -> str:
    return f"fact_{uuid.uuid4().hex[:16]}"


def _is_model_origin(origin: str) -> bool:
    normalized = origin.strip().lower()
    if not normalized:
        return False
    head = re.split(r"[:/\s_-]+", normalized, maxsplit=1)[0]
    return head in _MODEL_ORIGIN_PREFIXES


def _bound_top_k(top_k: int, *, max_top_k: int = MAX_TOP_K) -> int:
    try:
        requested = int(top_k)
    except (TypeError, ValueError) as exc:
        raise FactValidationError(f"top_k must be an integer, got {top_k!r}") from exc
    if requested <= 0:
        return 0
    cap = max(1, min(int(max_top_k), MAX_TOP_K))
    return min(requested, cap)


def _tokenize_text(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _tokenize_query(query: str) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _tokenize_text(query):
        if token not in seen:
            seen.add(token)
            tokens.append(token)
            if len(tokens) >= _MAX_QUERY_TOKENS:
                break
    return tuple(tokens)


def _build_fts5_match_expr(tokens: Sequence[str]) -> str:
    return " AND ".join(f'"{token}"' for token in tokens)


def normalize_rel_path(path: str) -> str:
    """Validate and normalize a worktree-relative POSIX path."""
    if not isinstance(path, str):
        raise FactValidationError(f"Path must be a string, got {type(path).__name__}")
    raw = path.strip()
    if not raw or "\x00" in raw:
        raise FactValidationError("Source path must be a non-empty string without NUL bytes.")
    raw = raw.replace("\\", "/")
    if raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
        raise FactValidationError(f"Source path must be worktree-relative: {path!r}")
    normalized = posixpath.normpath(raw)
    if normalized in (".", "..") or normalized.startswith("../"):
        raise FactValidationError(f"Source path escapes worktree root: {path!r}")
    return normalized


def worktree_scope_id(root: Path | str) -> str:
    """Return a deterministic canonical identifier for a local worktree root."""
    resolved = os.path.realpath(str(Path(root).expanduser()))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]
    return f"wt:{digest}"


def chat_scope_id(
    root: Path | str,
    chat_history_file: Path | str | None = None,
) -> str:
    """Return a deterministic canonical chat hash ID ('chat:<16-char sha256 hex>')."""
    root_real = os.path.realpath(str(Path(root).expanduser()))
    if chat_history_file is None or (
        isinstance(chat_history_file, str) and not chat_history_file.strip()
    ):
        history_candidate = Path(root_real) / ".aider.chat.history.md"
    else:
        raw_history = (
            chat_history_file.strip()
            if isinstance(chat_history_file, str)
            else chat_history_file
        )
        history_path = Path(raw_history).expanduser()
        if not history_path.is_absolute():
            history_candidate = Path(root_real) / history_path
        else:
            history_candidate = history_path
    history_real = os.path.realpath(str(history_candidate))
    payload = f"{root_real}\x00{history_real}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"chat:{digest}"


def resolve_facts_jsonl_path(
    root: Path | str,
    facts_file: Path | str | None = None,
) -> Path:
    """Resolve the JSONL facts path for a worktree (defaulting to `.aider.facts.jsonl`)."""
    root_path = Path(root).expanduser().resolve()
    if facts_file is None or (isinstance(facts_file, str) and not facts_file.strip()):
        return root_path / DEFAULT_FACTS_JSONL_FILENAME
    raw = facts_file.strip() if isinstance(facts_file, str) else facts_file
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    return candidate.resolve()


def parse_facts_db_url(root: Path | str, facts_db: Path | str) -> Path:
    """Parse a local SQLite file path or sqlite:// URL into a resolved filesystem Path."""
    if isinstance(facts_db, Path):
        raw = str(facts_db).strip()
    elif isinstance(facts_db, str):
        raw = facts_db.strip()
    else:
        raise FactValidationError(
            f"Database path/URL must be a string or Path, got {type(facts_db).__name__}"
        )
    if not raw or "\x00" in raw:
        raise FactValidationError("Database path/URL must be a non-empty string.")
    if raw.lower() == ":memory:":
        raise FactValidationError(
            "In-memory SQLite databases (':memory:') are not supported for project facts."
        )

    root_path = Path(root).expanduser().resolve()

    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme_lower = scheme.strip().lower()
        if scheme_lower != "sqlite":
            raise FactValidationError(
                f"Unsupported database URL scheme {scheme!r}; "
                "only local file paths and 'sqlite://' URLs are supported."
            )
        path_part, _, query_and_frag = rest.partition("?")
        path_part = path_part.split("#", 1)[0]
        if "mode=memory" in query_and_frag.lower():
            raise FactValidationError(
                "In-memory SQLite URLs are not supported for project facts."
            )
        path_part = unquote(path_part)
        if not path_part or path_part == "/" or path_part.lstrip("/").lower() == ":memory:":
            raise FactValidationError(
                f"Invalid SQLite database URL {facts_db!r}: expected a filesystem path."
            )
        if path_part.startswith("//"):
            stripped = path_part.lstrip("/")
            if not stripped or stripped.lower() == ":memory:":
                raise FactValidationError(
                    f"Invalid SQLite database URL {facts_db!r}: expected a filesystem path."
                )
            path_part = "/" + stripped
        if path_part.startswith("/") and _WINDOWS_DRIVE_RE.match(path_part[1:]):
            path_part = path_part[1:]
        candidate = Path(path_part).expanduser()
        if not candidate.is_absolute():
            candidate = root_path / candidate
        return candidate.resolve()

    if raw.lower().startswith(("sqlite:", "file:")):
        raise FactValidationError(
            f"Unsupported database URL format {facts_db!r}; "
            "use a file path, 'sqlite:///abs/path', or 'sqlite://rel/path'."
        )

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    return candidate.resolve()


def resolve_facts_db_path(root: Path | str, facts_db: Path | str | None = None) -> Path:
    """Resolve the database path for a worktree (defaulting to `.aider.facts.sqlite3`)."""
    root_path = Path(root).expanduser().resolve()
    if facts_db is None or (isinstance(facts_db, str) and not facts_db.strip()):
        return root_path / DEFAULT_FACTS_FILENAME
    return parse_facts_db_url(root_path, facts_db)


def _resolve_worktree_file(root: Path | str, rel_path: str) -> tuple[Path, Path]:
    normalized = normalize_rel_path(rel_path)
    root_resolved = Path(root).expanduser().resolve()
    target = root_resolved.joinpath(*normalized.split("/"))
    # Reject symlink components inside the worktree to prevent escaping root.
    current = root_resolved
    for part in normalized.split("/"):
        current = current / part
        if current.is_symlink():
            raise FactValidationError(f"Symlinked source path is not allowed: {normalized!r}")
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise FactValidationError(f"Source path escapes worktree root: {normalized!r}") from exc
    return root_resolved, target_resolved


def hash_worktree_file(root: Path | str, rel_path: str) -> str | None:
    """Return the SHA-256 hex digest of a worktree-relative regular file, or None if missing."""
    _, target = _resolve_worktree_file(root, rel_path)
    if not target.exists() or not target.is_file():
        return None
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceSnapshot:
    """Exact content-hash provenance for a single worktree-relative source file."""

    path: str
    sha256: str
    start_line: int | None = None
    end_line: int | None = None
    symbol: str = ""

    def __post_init__(self) -> None:
        normalized_path = normalize_rel_path(self.path)
        object.__setattr__(self, "path", normalized_path)
        sha = (self.sha256 or "").strip().lower()
        if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise FactValidationError(f"Invalid SHA-256 source hash: {self.sha256!r}")
        object.__setattr__(self, "sha256", sha)
        if self.start_line is not None and self.start_line < 1:
            raise FactValidationError("start_line must be >= 1")
        if self.end_line is not None:
            if self.end_line < 1:
                raise FactValidationError("end_line must be >= 1")
            if self.start_line is not None and self.end_line < self.start_line:
                raise FactValidationError("end_line must be >= start_line")
        object.__setattr__(self, "symbol", (self.symbol or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceSnapshot:
        if not isinstance(data, Mapping):
            raise FactValidationError("Source snapshot record must be a mapping.")
        path = data.get("path")
        if not isinstance(path, str):
            raise FactValidationError("Source snapshot 'path' must be a string.")
        sha256 = data.get("sha256", "")
        if sha256 is None:
            sha256 = ""
        if not isinstance(sha256, str):
            raise FactValidationError("Source snapshot 'sha256' must be a string.")
        start_line = data.get("start_line")
        if start_line is not None and (
            isinstance(start_line, bool) or not isinstance(start_line, int)
        ):
            raise FactValidationError("Source snapshot 'start_line' must be an integer or null.")
        end_line = data.get("end_line")
        if end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int)):
            raise FactValidationError("Source snapshot 'end_line' must be an integer or null.")
        symbol = data.get("symbol", "")
        if symbol is None:
            symbol = ""
        if not isinstance(symbol, str):
            raise FactValidationError("Source snapshot 'symbol' must be a string.")
        return cls(
            path=path,
            sha256=sha256,
            start_line=start_line,
            end_line=end_line,
            symbol=symbol,
        )


def capture_source_snapshots(
    root: Path | str,
    paths: Iterable[str],
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    symbol: str = "",
    allow_missing: bool = False,
) -> tuple[SourceSnapshot, ...]:
    """Capture SHA-256 source snapshots for worktree-relative files."""
    snapshots: list[SourceSnapshot] = []
    seen: set[str] = set()
    for raw_path in paths:
        rel_path = normalize_rel_path(raw_path)
        if rel_path in seen:
            continue
        seen.add(rel_path)
        digest = hash_worktree_file(root, rel_path)
        if digest is None:
            if not allow_missing:
                raise FileNotFoundError(f"Source file not found in worktree: {rel_path}")
            digest = ""
        snapshots.append(
            SourceSnapshot(
                path=rel_path,
                sha256=digest,
                start_line=start_line,
                end_line=end_line,
                symbol=symbol,
            )
        )
    return tuple(snapshots)


@dataclass(frozen=True)
class FactProvenance:
    """Origin and source-file provenance attached to a persisted fact."""

    origin: str
    worktree_id: str
    recorded_at: str
    sources: tuple[SourceSnapshot, ...] = ()
    commit_sha: str = ""
    note: str = ""
    chat_id: str = ""

    def __post_init__(self) -> None:
        origin = (self.origin or "").strip()
        if not origin:
            raise FactValidationError("Fact provenance origin must be non-empty.")
        worktree_id = (self.worktree_id or "").strip()
        if not worktree_id:
            raise FactValidationError("Fact provenance worktree_id must be non-empty.")
        recorded_at = (self.recorded_at or "").strip()
        if not recorded_at:
            raise FactValidationError("Fact provenance recorded_at must be non-empty.")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "worktree_id", worktree_id)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "commit_sha", (self.commit_sha or "").strip())
        object.__setattr__(self, "note", (self.note or "").strip())
        object.__setattr__(self, "chat_id", (self.chat_id or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "worktree_id": self.worktree_id,
            "recorded_at": self.recorded_at,
            "sources": [src.to_dict() for src in self.sources],
            "commit_sha": self.commit_sha,
            "note": self.note,
            "chat_id": self.chat_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FactProvenance:
        if not isinstance(data, Mapping):
            raise FactValidationError("Fact provenance record must be a mapping.")
        origin = data.get("origin")
        worktree_id = data.get("worktree_id")
        recorded_at = data.get("recorded_at")
        if not isinstance(origin, str):
            raise FactValidationError("Fact provenance 'origin' must be a string.")
        if not isinstance(worktree_id, str):
            raise FactValidationError("Fact provenance 'worktree_id' must be a string.")
        if not isinstance(recorded_at, str):
            raise FactValidationError("Fact provenance 'recorded_at' must be a string.")
        raw_sources = data.get("sources", ())
        if raw_sources is None:
            raw_sources = ()
        if not isinstance(raw_sources, (list, tuple)):
            raise FactValidationError("Fact provenance 'sources' must be a list.")
        sources = tuple(SourceSnapshot.from_dict(item) for item in raw_sources)
        commit_sha = data.get("commit_sha", "")
        if commit_sha is None:
            commit_sha = ""
        if not isinstance(commit_sha, str):
            raise FactValidationError("Fact provenance 'commit_sha' must be a string.")
        note = data.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise FactValidationError("Fact provenance 'note' must be a string.")
        chat_id = data.get("chat_id", "")
        if chat_id is None:
            chat_id = ""
        if not isinstance(chat_id, str):
            raise FactValidationError("Fact provenance 'chat_id' must be a string.")
        return cls(
            origin=origin,
            worktree_id=worktree_id,
            recorded_at=recorded_at,
            sources=sources,
            commit_sha=commit_sha,
            note=note,
            chat_id=chat_id,
        )


@dataclass(frozen=True)
class ProjectFact:
    """Immutable domain record representing a worktree-scoped project fact."""

    fact_id: str
    worktree_id: str
    statement: str
    provenance: FactProvenance
    verification: VerificationState = VerificationState.UNVERIFIED
    status: FactStatus = FactStatus.ACTIVE
    tags: tuple[str, ...] = ()
    stale_reason: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        fact_id = (self.fact_id or "").strip()
        if not fact_id:
            raise FactValidationError("fact_id must be non-empty.")
        worktree_id = (self.worktree_id or "").strip()
        if not worktree_id:
            raise FactValidationError("worktree_id must be non-empty.")
        if self.provenance.worktree_id != worktree_id:
            raise FactValidationError(
                "ProjectFact.worktree_id must match provenance.worktree_id."
            )
        statement = (self.statement or "").strip()
        if not statement:
            raise FactValidationError("Fact statement must be non-empty.")
        if len(statement) > MAX_FACT_STATEMENT_CHARS:
            raise FactValidationError(
                f"Fact statement exceeds {MAX_FACT_STATEMENT_CHARS} characters."
            )
        verification = (
            self.verification
            if isinstance(self.verification, VerificationState)
            else VerificationState(str(self.verification))
        )
        status = (
            self.status
            if isinstance(self.status, FactStatus)
            else FactStatus(str(self.status))
        )
        if verification == VerificationState.CONFIRMED and _is_model_origin(
            self.provenance.origin
        ):
            raise FactValidationError(
                "Model-generated candidates must remain unverified until explicitly confirmed."
            )
        clean_tags = tuple(
            dict.fromkeys(
                tag.strip().lower() for tag in self.tags if isinstance(tag, str) and tag.strip()
            )
        )
        updated_at = (self.updated_at or "").strip() or self.provenance.recorded_at

        object.__setattr__(self, "fact_id", fact_id)
        object.__setattr__(self, "worktree_id", worktree_id)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "verification", verification)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "tags", clean_tags)
        object.__setattr__(self, "stale_reason", (self.stale_reason or "").strip())
        object.__setattr__(self, "updated_at", updated_at)

    @classmethod
    def confirmed(
        cls,
        *,
        worktree_id: str,
        statement: str,
        sources: Sequence[SourceSnapshot] = (),
        origin: str = "user:/fact",
        tags: Sequence[str] = (),
        commit_sha: str = "",
        note: str = "",
        chat_id: str = "",
        fact_id: str | None = None,
        recorded_at: str | None = None,
    ) -> ProjectFact:
        """Construct an explicitly confirmed project fact."""
        ts = recorded_at or _utc_now_iso()
        return cls(
            fact_id=fact_id or _new_fact_id(),
            worktree_id=worktree_id,
            statement=statement,
            verification=VerificationState.CONFIRMED,
            status=FactStatus.ACTIVE,
            provenance=FactProvenance(
                origin=origin,
                worktree_id=worktree_id,
                recorded_at=ts,
                sources=tuple(sources),
                commit_sha=commit_sha,
                note=note,
                chat_id=chat_id,
            ),
            tags=tuple(tags),
            updated_at=ts,
        )

    @classmethod
    def model_candidate(
        cls,
        *,
        worktree_id: str,
        statement: str,
        sources: Sequence[SourceSnapshot] = (),
        origin: str = "model_candidate",
        tags: Sequence[str] = (),
        commit_sha: str = "",
        note: str = "",
        chat_id: str = "",
        fact_id: str | None = None,
        recorded_at: str | None = None,
    ) -> ProjectFact:
        """Construct an unverified model-proposed candidate fact."""
        ts = recorded_at or _utc_now_iso()
        return cls(
            fact_id=fact_id or _new_fact_id(),
            worktree_id=worktree_id,
            statement=statement,
            verification=VerificationState.UNVERIFIED,
            status=FactStatus.ACTIVE,
            provenance=FactProvenance(
                origin=origin,
                worktree_id=worktree_id,
                recorded_at=ts,
                sources=tuple(sources),
                commit_sha=commit_sha,
                note=note,
                chat_id=chat_id,
            ),
            tags=tuple(tags),
            updated_at=ts,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "worktree_id": self.worktree_id,
            "chat_id": self.provenance.chat_id,
            "statement": self.statement,
            "verification": self.verification.value,
            "status": self.status.value,
            "provenance": self.provenance.to_dict(),
            "tags": list(self.tags),
            "stale_reason": self.stale_reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProjectFact:
        if not isinstance(data, Mapping):
            raise FactValidationError("ProjectFact record must be a mapping.")
        fact_id = data.get("fact_id")
        worktree_id = data.get("worktree_id")
        statement = data.get("statement")
        if not isinstance(fact_id, str):
            raise FactValidationError("ProjectFact 'fact_id' must be a string.")
        if not isinstance(worktree_id, str):
            raise FactValidationError("ProjectFact 'worktree_id' must be a string.")
        if not isinstance(statement, str):
            raise FactValidationError("ProjectFact 'statement' must be a string.")
        prov_raw = data.get("provenance")
        if not isinstance(prov_raw, Mapping):
            raise FactValidationError("ProjectFact 'provenance' must be a mapping.")
        provenance = FactProvenance.from_dict(prov_raw)
        top_chat_id = data.get("chat_id", "")
        if top_chat_id is None:
            top_chat_id = ""
        if not isinstance(top_chat_id, str):
            raise FactValidationError("ProjectFact 'chat_id' must be a string.")
        top_chat_id = top_chat_id.strip()
        if top_chat_id and not provenance.chat_id:
            provenance = replace(provenance, chat_id=top_chat_id)
        verification = data.get("verification", VerificationState.UNVERIFIED.value)
        status = data.get("status", FactStatus.ACTIVE.value)
        raw_tags = data.get("tags", ())
        if raw_tags is None:
            raw_tags = ()
        if not isinstance(raw_tags, (list, tuple)) or any(
            not isinstance(tag, str) for tag in raw_tags
        ):
            raise FactValidationError("ProjectFact 'tags' must be a list of strings.")
        stale_reason = data.get("stale_reason", "")
        if stale_reason is None:
            stale_reason = ""
        if not isinstance(stale_reason, str):
            raise FactValidationError("ProjectFact 'stale_reason' must be a string.")
        updated_at = data.get("updated_at", "")
        if updated_at is None:
            updated_at = ""
        if not isinstance(updated_at, str):
            raise FactValidationError("ProjectFact 'updated_at' must be a string.")
        return cls(
            fact_id=fact_id,
            worktree_id=worktree_id,
            statement=statement,
            provenance=provenance,
            verification=VerificationState(str(verification)),
            status=FactStatus(str(status)),
            tags=tuple(raw_tags),
            stale_reason=stale_reason,
            updated_at=updated_at,
        )

    @property
    def chat_id(self) -> str:
        return self.provenance.chat_id

    @property
    def is_confirmed(self) -> bool:
        return self.verification == VerificationState.CONFIRMED

    @property
    def is_unverified(self) -> bool:
        return self.verification == VerificationState.UNVERIFIED

    @property
    def is_active(self) -> bool:
        return self.status == FactStatus.ACTIVE

    @property
    def is_stale(self) -> bool:
        return self.status == FactStatus.STALE

    @property
    def source_paths(self) -> tuple[str, ...]:
        return tuple(source.path for source in self.provenance.sources)

    @property
    def source_hashes(self) -> dict[str, str]:
        return {source.path: source.sha256 for source in self.provenance.sources}


@dataclass(frozen=True)
class SqliteFts5Status:
    """Result of probing embedded Python sqlite3 and FTS5 support."""

    available: bool
    reason: str
    message: str


@dataclass(frozen=True)
class RevalidationChange:
    """Record of a single fact status transition discovered during revalidation."""

    fact_id: str
    previous_status: FactStatus
    new_status: FactStatus
    reason: str
    changed_paths: tuple[str, ...] = ()
    missing_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RevalidationReport:
    """Summary of source-hash revalidation across a worktree's facts."""

    checked: int
    active: int
    stale: int
    invalidated: int
    changes: tuple[RevalidationChange, ...] = ()


@dataclass(frozen=True)
class ParsedFactInput:
    """Structured result of parsing `/fact` command arguments."""

    statement: str
    source_paths: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    symbol: str = ""
    start_line: int | None = None
    end_line: int | None = None
    note: str = ""


@dataclass(frozen=True)
class FactCommandResult:
    """Outcome of handling an explicit `/fact` command."""

    ok: bool
    message: str
    fact: ProjectFact | None = None


def _resolve_sqlite_module(sqlite_module: Any = _UNSET) -> Any:
    if sqlite_module is _UNSET:
        return sqlite3
    return sqlite_module


def check_sqlite_fts5_support(*, sqlite_module: Any = _UNSET) -> SqliteFts5Status:
    """Probe embedded Python sqlite3 + FTS5 using an in-memory DB without touching disk."""
    mod = _resolve_sqlite_module(sqlite_module)
    if mod is None or not callable(getattr(mod, "connect", None)):
        return SqliteFts5Status(
            available=False,
            reason="missing_sqlite3",
            message=(
                "Project facts disabled: Python's embedded sqlite3 driver is unavailable. "
                "Continuing session without a fact store."
            ),
        )

    conn = None
    try:
        conn = mod.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _aider_fts5_probe USING fts5(content)")
        conn.execute("DROP TABLE _aider_fts5_probe")
    except Exception as exc:
        detail = str(exc).strip()
        suffix = f" ({detail})" if detail else ""
        return SqliteFts5Status(
            available=False,
            reason="missing_fts5",
            message=(
                f"Project facts disabled: SQLite FTS5 support is unavailable{suffix}. "
                "Continuing session without a fact store."
            ),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return SqliteFts5Status(available=True, reason="ok", message="")


class FactRepository(abc.ABC):
    """Repository Port for persisting, searching, and listing project facts."""

    @abc.abstractmethod
    def add(self, fact: ProjectFact) -> ProjectFact:
        """Atomically persist a fact."""

    @abc.abstractmethod
    def get(self, fact_id: str, *, worktree_id: str) -> ProjectFact | None:
        """Fetch a single fact by ID within ``worktree_id``."""

    @abc.abstractmethod
    def search(
        self,
        query: str,
        *,
        worktree_id: str,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
    ) -> tuple[ProjectFact, ...]:
        """Search facts within ``worktree_id`` and return at most ``top_k`` matches."""

    @abc.abstractmethod
    def list_for_paths(
        self,
        paths: Sequence[str],
        *,
        worktree_id: str,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
    ) -> tuple[ProjectFact, ...]:
        """Return facts associated with ``paths`` in ``worktree_id``, bounded to ``top_k``."""

    @abc.abstractmethod
    def list_all(
        self,
        *,
        worktree_id: str,
        include_unverified: bool = True,
        include_stale: bool = True,
    ) -> tuple[ProjectFact, ...]:
        """Return all facts stored for ``worktree_id``."""

    @abc.abstractmethod
    def update_status(
        self,
        fact_id: str,
        *,
        worktree_id: str,
        status: FactStatus,
        stale_reason: str = "",
        updated_at: str | None = None,
    ) -> ProjectFact | None:
        """Atomically update the status of a fact while preserving provenance."""

    def close(self) -> None:
        """Release underlying resources."""
        return None

    def __enter__(self) -> FactRepository:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _matches_fact_filters(
    fact: ProjectFact,
    *,
    worktree_id: str,
    include_unverified: bool,
    include_stale: bool,
) -> bool:
    if fact.worktree_id != worktree_id:
        return False
    if not include_unverified and fact.verification != VerificationState.CONFIRMED:
        return False
    if not include_stale and fact.status != FactStatus.ACTIVE:
        return False
    return True


def _search_facts_collection(
    facts: Iterable[ProjectFact],
    query: str,
    *,
    worktree_id: str,
    limit: int,
    include_unverified: bool,
    include_stale: bool,
) -> tuple[ProjectFact, ...]:
    tokens = _tokenize_query(query)
    if limit == 0 or not tokens:
        return ()

    scored: list[tuple[int, str, str, ProjectFact]] = []
    for fact in facts:
        if not _matches_fact_filters(
            fact,
            worktree_id=worktree_id,
            include_unverified=include_unverified,
            include_stale=include_stale,
        ):
            continue
        corpus = " ".join(
            (
                fact.statement,
                " ".join(fact.tags),
                " ".join(s.path for s in fact.provenance.sources),
                " ".join(s.symbol for s in fact.provenance.sources if s.symbol),
            )
        )
        doc_tokens = _tokenize_text(corpus)
        if not doc_tokens:
            continue
        doc_token_set = set(doc_tokens)
        if not all(tok in doc_token_set for tok in tokens):
            continue
        hits = sum(doc_tokens.count(tok) for tok in tokens)
        scored.append((hits, fact.updated_at, fact.fact_id, fact))

    # Sort primary by hits desc, then updated_at desc, then fact_id asc.
    scored.sort(key=lambda item: item[2])
    scored.sort(key=lambda item: item[1], reverse=True)
    scored.sort(key=lambda item: item[0], reverse=True)
    return tuple(item[3] for item in scored[:limit])


def _list_facts_collection_for_paths(
    facts: Iterable[ProjectFact],
    paths: Sequence[str],
    *,
    worktree_id: str,
    limit: int,
    include_unverified: bool,
    include_stale: bool,
) -> tuple[ProjectFact, ...]:
    normalized_paths = {normalize_rel_path(p) for p in paths}
    if limit == 0 or not normalized_paths:
        return ()

    matched: list[tuple[int, str, str, ProjectFact]] = []
    for fact in facts:
        if not _matches_fact_filters(
            fact,
            worktree_id=worktree_id,
            include_unverified=include_unverified,
            include_stale=include_stale,
        ):
            continue
        overlap = len(normalized_paths.intersection(fact.source_paths))
        if overlap <= 0:
            continue
        matched.append((overlap, fact.updated_at, fact.fact_id, fact))

    matched.sort(key=lambda item: item[2])
    matched.sort(key=lambda item: item[1], reverse=True)
    matched.sort(key=lambda item: item[0], reverse=True)
    return tuple(item[3] for item in matched[:limit])


def _list_all_facts_collection(
    facts: Iterable[ProjectFact],
    *,
    worktree_id: str,
    include_unverified: bool,
    include_stale: bool,
) -> tuple[ProjectFact, ...]:
    items = [
        fact
        for fact in facts
        if _matches_fact_filters(
            fact,
            worktree_id=worktree_id,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )
    ]
    items.sort(key=lambda f: f.fact_id)
    items.sort(key=lambda f: f.updated_at, reverse=True)
    return tuple(items)


class FakeFactRepository(FactRepository):
    """In-memory Repository Pattern implementation with worktree isolation and bounded top-K."""

    def __init__(self, *, max_top_k: int = MAX_TOP_K, read_only: bool = False) -> None:
        self._max_top_k = max(1, min(int(max_top_k), MAX_TOP_K))
        self._read_only = bool(read_only)
        self._facts: dict[tuple[str, str], ProjectFact] = {}

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise FactStoreReadOnlyError("Project fact repository is read-only.")

    def add(self, fact: ProjectFact) -> ProjectFact:
        self._ensure_writable()
        if not isinstance(fact, ProjectFact):
            raise FactValidationError("Expected a ProjectFact instance.")
        self._facts[(fact.worktree_id, fact.fact_id)] = fact
        return fact

    def get(self, fact_id: str, *, worktree_id: str) -> ProjectFact | None:
        return self._facts.get((worktree_id, fact_id))

    def search(
        self,
        query: str,
        *,
        worktree_id: str,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
    ) -> tuple[ProjectFact, ...]:
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        return _search_facts_collection(
            self._facts.values(),
            query,
            worktree_id=worktree_id,
            limit=limit,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )

    def list_for_paths(
        self,
        paths: Sequence[str],
        *,
        worktree_id: str,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
    ) -> tuple[ProjectFact, ...]:
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        return _list_facts_collection_for_paths(
            self._facts.values(),
            paths,
            worktree_id=worktree_id,
            limit=limit,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )

    def list_all(
        self,
        *,
        worktree_id: str,
        include_unverified: bool = True,
        include_stale: bool = True,
    ) -> tuple[ProjectFact, ...]:
        return _list_all_facts_collection(
            self._facts.values(),
            worktree_id=worktree_id,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )

    def update_status(
        self,
        fact_id: str,
        *,
        worktree_id: str,
        status: FactStatus,
        stale_reason: str = "",
        updated_at: str | None = None,
    ) -> ProjectFact | None:
        self._ensure_writable()
        existing = self._facts.get((worktree_id, fact_id))
        if existing is None:
            return None
        new_status = status if isinstance(status, FactStatus) else FactStatus(str(status))
        updated = replace(
            existing,
            status=new_status,
            stale_reason=stale_reason.strip(),
            updated_at=updated_at or _utc_now_iso(),
        )
        self._facts[(worktree_id, fact_id)] = updated
        return updated


class JsonlFactRepository(FactRepository):
    """UTF-8 JSONL Repository Pattern implementation with atomic rewrites."""

    def __init__(
        self,
        file_path: Path | str,
        *,
        max_top_k: int = MAX_TOP_K,
        read_only: bool = False,
    ) -> None:
        self._file_path = Path(file_path).expanduser().resolve()
        self._write_lock = FileLock(str(self._file_path) + ".lock")
        self._max_top_k = max(1, min(int(max_top_k), MAX_TOP_K))
        self._read_only = bool(read_only)

        if self._read_only and not self._file_path.exists():
            raise FactStoreError(
                f"Project facts file does not exist in read-only mode: {self._file_path}"
            )

        if not self._read_only and not self._file_path.exists():
            self._ensure_file_initialized()

        if self._file_path.exists():
            self._load_facts()

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise FactStoreReadOnlyError("Project fact repository is read-only.")

    @contextmanager
    def _locked_write(self) -> Iterator[None]:
        """Ensure the parent directory exists and hold the inter-process write lock."""
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_lock.acquire()
        except (OSError, FileLockTimeout) as exc:
            raise FactStoreError(
                f"Failed to acquire write lock for project facts file at {self._file_path}: {exc}"
            ) from exc
        try:
            yield
        finally:
            self._write_lock.release()

    def _ensure_file_initialized(self) -> None:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                str(self._file_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o666,
            )
        except FileExistsError:
            return
        except OSError as exc:
            raise FactStoreError(
                f"Failed to create project facts file at {self._file_path}: {exc}"
            ) from exc
        else:
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _load_facts(self) -> dict[tuple[str, str], ProjectFact]:
        if not self._file_path.exists():
            if self._read_only:
                raise FactStoreError(
                    f"Project facts file does not exist in read-only mode: {self._file_path}"
                )
            return {}
        facts: dict[tuple[str, str], ProjectFact] = {}
        try:
            with self._file_path.open("r", encoding="utf-8") as handle:
                for lineno, raw_line in enumerate(handle, start=1):
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    try:
                        payload = json.loads(stripped)
                    except ValueError as exc:
                        raise FactStoreError(
                            f"Malformed JSONL in {self._file_path} at line {lineno}: {exc}"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise FactStoreError(
                            f"Malformed JSONL record in {self._file_path} at line {lineno}: "
                            "expected a JSON object."
                        )
                    try:
                        fact = ProjectFact.from_dict(payload)
                    except (FactValidationError, KeyError, TypeError, ValueError) as exc:
                        raise FactStoreError(
                            f"Invalid project fact record in {self._file_path} "
                            f"at line {lineno}: {exc}"
                        ) from exc
                    facts[(fact.worktree_id, fact.fact_id)] = fact
        except FactStoreError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise FactStoreError(
                f"Failed to read project facts file at {self._file_path}: {exc}"
            ) from exc
        return facts

    def _write_facts(self, facts: dict[tuple[str, str], ProjectFact]) -> None:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(facts.values(), key=lambda f: (f.worktree_id, f.fact_id))
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._file_path.name}.",
            suffix=".tmp",
            dir=str(self._file_path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                for fact in ordered:
                    line = json.dumps(
                        fact.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._file_path)
        except Exception as exc:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            if isinstance(exc, FactStoreError):
                raise
            raise FactStoreError(
                f"Failed to write project facts file at {self._file_path}: {exc}"
            ) from exc

    def add(self, fact: ProjectFact) -> ProjectFact:
        self._ensure_writable()
        if not isinstance(fact, ProjectFact):
            raise FactValidationError("Expected a ProjectFact instance.")
        with self._locked_write():
            facts = self._load_facts()
            facts[(fact.worktree_id, fact.fact_id)] = fact
            self._write_facts(facts)
            return fact

    def get(self, fact_id: str, *, worktree_id: str) -> ProjectFact | None:
        return self._load_facts().get((worktree_id, fact_id))

    def search(
        self,
        query: str,
        *,
        worktree_id: str,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
    ) -> tuple[ProjectFact, ...]:
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        return _search_facts_collection(
            self._load_facts().values(),
            query,
            worktree_id=worktree_id,
            limit=limit,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )

    def list_for_paths(
        self,
        paths: Sequence[str],
        *,
        worktree_id: str,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
    ) -> tuple[ProjectFact, ...]:
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        return _list_facts_collection_for_paths(
            self._load_facts().values(),
            paths,
            worktree_id=worktree_id,
            limit=limit,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )

    def list_all(
        self,
        *,
        worktree_id: str,
        include_unverified: bool = True,
        include_stale: bool = True,
    ) -> tuple[ProjectFact, ...]:
        return _list_all_facts_collection(
            self._load_facts().values(),
            worktree_id=worktree_id,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )

    def update_status(
        self,
        fact_id: str,
        *,
        worktree_id: str,
        status: FactStatus,
        stale_reason: str = "",
        updated_at: str | None = None,
    ) -> ProjectFact | None:
        self._ensure_writable()
        with self._locked_write():
            facts = self._load_facts()
            existing = facts.get((worktree_id, fact_id))
            if existing is None:
                return None
            new_status = status if isinstance(status, FactStatus) else FactStatus(str(status))
            updated = replace(
                existing,
                status=new_status,
                stale_reason=stale_reason.strip(),
                updated_at=updated_at or _utc_now_iso(),
            )
            facts[(worktree_id, fact_id)] = updated
            self._write_facts(facts)
            return updated


class SqliteFtsFactRepository(FactRepository):
    """Embedded SQLite + FTS5 Repository Pattern implementation with atomic persistence."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        max_top_k: int = MAX_TOP_K,
        read_only: bool = False,
        sqlite_module: Any = _UNSET,
    ) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._max_top_k = max(1, min(int(max_top_k), MAX_TOP_K))
        self._read_only = bool(read_only)
        self._sqlite = _resolve_sqlite_module(sqlite_module)

        probe = check_sqlite_fts5_support(sqlite_module=self._sqlite)
        if not probe.available:
            if probe.reason == "missing_sqlite3":
                raise SqliteUnavailableError(probe.message)
            raise Fts5UnavailableError(probe.message)

        if self._read_only and not self._db_path.exists():
            raise FactStoreError(
                f"Project facts database does not exist in read-only mode: {self._db_path}"
            )

        self._conn: Any = None
        if not self._read_only:
            self._ensure_database_initialized()
        self._conn = self._open_connection()
        self._has_chat_id_column = self._detect_chat_id_column(self._conn)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise FactStoreReadOnlyError("Project fact repository is read-only.")

    def _detect_chat_id_column(self, conn: Any) -> bool:
        rows = conn.execute("PRAGMA table_info(facts)").fetchall()
        return any(row["name"] == "chat_id" for row in rows)

    def _configure_connection(self, conn: Any) -> None:
        conn.row_factory = self._sqlite.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if self._read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA synchronous = FULL")

    def _apply_schema(self, conn: Any) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            if not self._detect_chat_id_column(conn):
                conn.execute("ALTER TABLE facts ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def _ensure_database_initialized(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._db_path.exists():
            self._create_database_atomically()
            return

        # Existing file: verify integrity and ensure tables exist inside a transaction.
        conn = self._sqlite.connect(str(self._db_path), isolation_level=None)
        try:
            self._configure_connection(conn)
            self._apply_schema(conn)
        except Exception as exc:
            raise FactStoreError(
                f"Failed to initialize project facts database at {self._db_path}: {exc}"
            ) from exc
        finally:
            conn.close()

    def _create_database_atomically(self) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".aider.facts.init.",
            suffix=".sqlite3.tmp",
            dir=str(self._db_path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        conn = None
        try:
            conn = self._sqlite.connect(str(tmp_path), isolation_level=None)
            self._configure_connection(conn)
            self._apply_schema(conn)
            conn.close()
            conn = None

            try:
                os.link(tmp_path, self._db_path)
            except FileExistsError:
                # Another process created the DB concurrently; leave it intact.
                pass
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    pass
                else:
                    os.replace(tmp_path, self._db_path)
                    return
        except Exception as exc:
            raise FactStoreError(
                f"Failed to create project facts database at {self._db_path}: {exc}"
            ) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _open_connection(self) -> Any:
        try:
            if self._read_only:
                uri = f"{self._db_path.as_uri()}?mode=ro"
                conn = self._sqlite.connect(uri, uri=True, isolation_level=None)
            else:
                conn = self._sqlite.connect(str(self._db_path), isolation_level=None)
            self._configure_connection(conn)
            return conn
        except Exception as exc:
            raise FactStoreError(
                f"Failed to open project facts database at {self._db_path}: {exc}"
            ) from exc

    def _require_conn(self) -> Any:
        if self._conn is None:
            raise FactStoreError("Project facts repository connection is closed.")
        return self._conn

    def _hydrate_facts(self, rows: Sequence[Any]) -> tuple[ProjectFact, ...]:
        if not rows:
            return ()
        conn = self._require_conn()
        worktree_id = rows[0]["worktree_id"]
        fact_ids = [row["fact_id"] for row in rows]
        placeholders = ", ".join("?" for _ in fact_ids)
        src_rows = conn.execute(
            f"""
            SELECT fact_id, path, sha256, start_line, end_line, symbol
            FROM fact_sources
            WHERE worktree_id = ? AND fact_id IN ({placeholders})
            ORDER BY fact_id ASC, seq ASC
            """,
            (worktree_id, *fact_ids),
        ).fetchall()

        sources_by_fact: dict[str, list[SourceSnapshot]] = {}
        for src in src_rows:
            sources_by_fact.setdefault(src["fact_id"], []).append(
                SourceSnapshot(
                    path=src["path"],
                    sha256=src["sha256"],
                    start_line=src["start_line"],
                    end_line=src["end_line"],
                    symbol=src["symbol"],
                )
            )

        hydrated: list[ProjectFact] = []
        for row in rows:
            try:
                tags = tuple(json.loads(row["tags_json"] or "[]"))
            except (TypeError, ValueError):
                tags = ()
            fact_sources = tuple(sources_by_fact.get(row["fact_id"], ()))
            chat_id = row["chat_id"] if "chat_id" in row.keys() else ""
            hydrated.append(
                ProjectFact(
                    fact_id=row["fact_id"],
                    worktree_id=row["worktree_id"],
                    statement=row["statement"],
                    verification=VerificationState(row["verification"]),
                    status=FactStatus(row["status"]),
                    provenance=FactProvenance(
                        origin=row["origin"],
                        worktree_id=row["worktree_id"],
                        recorded_at=row["recorded_at"],
                        sources=fact_sources,
                        commit_sha=row["commit_sha"],
                        note=row["note"],
                        chat_id=chat_id or "",
                    ),
                    tags=tags,
                    stale_reason=row["stale_reason"],
                    updated_at=row["updated_at"],
                )
            )
        return tuple(hydrated)

    def add(self, fact: ProjectFact) -> ProjectFact:
        self._ensure_writable()
        if not isinstance(fact, ProjectFact):
            raise FactValidationError("Expected a ProjectFact instance.")
        conn = self._require_conn()
        tags_json = json.dumps(list(fact.tags), ensure_ascii=False)
        fts_tags = " ".join(fact.tags)
        fts_paths = " ".join(source.path for source in fact.provenance.sources)
        fts_symbols = " ".join(
            source.symbol for source in fact.provenance.sources if source.symbol
        )

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM facts_fts WHERE worktree_id = ? AND fact_id = ?",
                (fact.worktree_id, fact.fact_id),
            )
            conn.execute(
                "DELETE FROM fact_sources WHERE worktree_id = ? AND fact_id = ?",
                (fact.worktree_id, fact.fact_id),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO facts (
                    worktree_id,
                    fact_id,
                    statement,
                    verification,
                    status,
                    origin,
                    recorded_at,
                    updated_at,
                    commit_sha,
                    note,
                    tags_json,
                    stale_reason,
                    chat_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact.worktree_id,
                    fact.fact_id,
                    fact.statement,
                    fact.verification.value,
                    fact.status.value,
                    fact.provenance.origin,
                    fact.provenance.recorded_at,
                    fact.updated_at,
                    fact.provenance.commit_sha,
                    fact.provenance.note,
                    tags_json,
                    fact.stale_reason,
                    fact.provenance.chat_id,
                ),
            )
            for seq, source in enumerate(fact.provenance.sources):
                conn.execute(
                    """
                    INSERT INTO fact_sources (
                        worktree_id,
                        fact_id,
                        seq,
                        path,
                        sha256,
                        start_line,
                        end_line,
                        symbol
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact.worktree_id,
                        fact.fact_id,
                        seq,
                        source.path,
                        source.sha256,
                        source.start_line,
                        source.end_line,
                        source.symbol,
                    ),
                )
            conn.execute(
                """
                INSERT INTO facts_fts (
                    worktree_id,
                    fact_id,
                    statement,
                    tags,
                    paths,
                    symbols
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    fact.worktree_id,
                    fact.fact_id,
                    fact.statement,
                    fts_tags,
                    fts_paths,
                    fts_symbols,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return fact

    def get(self, fact_id: str, *, worktree_id: str) -> ProjectFact | None:
        conn = self._require_conn()
        chat_id_expr = "chat_id" if self._has_chat_id_column else "'' AS chat_id"
        rows = conn.execute(
            f"""
            SELECT
                worktree_id,
                fact_id,
                statement,
                verification,
                status,
                origin,
                recorded_at,
                updated_at,
                commit_sha,
                note,
                tags_json,
                stale_reason,
                {chat_id_expr}
            FROM facts
            WHERE worktree_id = ? AND fact_id = ?
            """,
            (worktree_id, fact_id),
        ).fetchall()
        facts = self._hydrate_facts(rows)
        return facts[0] if facts else None

    def search(
        self,
        query: str,
        *,
        worktree_id: str,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
    ) -> tuple[ProjectFact, ...]:
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        tokens = _tokenize_query(query)
        if limit == 0 or not tokens:
            return ()

        match_expr = _build_fts5_match_expr(tokens)
        clauses = ["facts_fts MATCH ?", "f.worktree_id = ?"]
        params: list[Any] = [match_expr, worktree_id]
        if not include_unverified:
            clauses.append("f.verification = ?")
            params.append(VerificationState.CONFIRMED.value)
        if not include_stale:
            clauses.append("f.status = ?")
            params.append(FactStatus.ACTIVE.value)
        params.append(limit)

        conn = self._require_conn()
        chat_id_expr = "f.chat_id" if self._has_chat_id_column else "'' AS chat_id"
        rows = conn.execute(
            f"""
            SELECT
                f.worktree_id,
                f.fact_id,
                f.statement,
                f.verification,
                f.status,
                f.origin,
                f.recorded_at,
                f.updated_at,
                f.commit_sha,
                f.note,
                f.tags_json,
                f.stale_reason,
                {chat_id_expr}
            FROM facts_fts
            CROSS JOIN facts AS f
              ON f.worktree_id = facts_fts.worktree_id
             AND f.fact_id = facts_fts.fact_id
            WHERE {" AND ".join(clauses)}
            ORDER BY bm25(facts_fts) ASC, f.updated_at DESC, f.fact_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return self._hydrate_facts(rows)

    def list_for_paths(
        self,
        paths: Sequence[str],
        *,
        worktree_id: str,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
    ) -> tuple[ProjectFact, ...]:
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        normalized = sorted({normalize_rel_path(p) for p in paths})
        if limit == 0 or not normalized:
            return ()

        placeholders = ", ".join("?" for _ in normalized)
        clauses = ["f.worktree_id = ?", f"fs.path IN ({placeholders})"]
        params: list[Any] = [worktree_id, *normalized]
        if not include_unverified:
            clauses.append("f.verification = ?")
            params.append(VerificationState.CONFIRMED.value)
        if not include_stale:
            clauses.append("f.status = ?")
            params.append(FactStatus.ACTIVE.value)
        params.append(limit)

        conn = self._require_conn()
        chat_id_expr = "f.chat_id" if self._has_chat_id_column else "'' AS chat_id"
        rows = conn.execute(
            f"""
            SELECT
                f.worktree_id,
                f.fact_id,
                f.statement,
                f.verification,
                f.status,
                f.origin,
                f.recorded_at,
                f.updated_at,
                f.commit_sha,
                f.note,
                f.tags_json,
                f.stale_reason,
                {chat_id_expr},
                COUNT(DISTINCT fs.path) AS match_count
            FROM facts AS f
            JOIN fact_sources AS fs
              ON fs.worktree_id = f.worktree_id
             AND fs.fact_id = f.fact_id
            WHERE {" AND ".join(clauses)}
            GROUP BY f.worktree_id, f.fact_id
            ORDER BY match_count DESC, f.updated_at DESC, f.fact_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return self._hydrate_facts(rows)

    def list_all(
        self,
        *,
        worktree_id: str,
        include_unverified: bool = True,
        include_stale: bool = True,
    ) -> tuple[ProjectFact, ...]:
        clauses = ["worktree_id = ?"]
        params: list[Any] = [worktree_id]
        if not include_unverified:
            clauses.append("verification = ?")
            params.append(VerificationState.CONFIRMED.value)
        if not include_stale:
            clauses.append("status = ?")
            params.append(FactStatus.ACTIVE.value)

        conn = self._require_conn()
        chat_id_expr = "chat_id" if self._has_chat_id_column else "'' AS chat_id"
        rows = conn.execute(
            f"""
            SELECT
                worktree_id,
                fact_id,
                statement,
                verification,
                status,
                origin,
                recorded_at,
                updated_at,
                commit_sha,
                note,
                tags_json,
                stale_reason,
                {chat_id_expr}
            FROM facts
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at DESC, fact_id ASC
            """,
            tuple(params),
        ).fetchall()
        return self._hydrate_facts(rows)

    def update_status(
        self,
        fact_id: str,
        *,
        worktree_id: str,
        status: FactStatus,
        stale_reason: str = "",
        updated_at: str | None = None,
    ) -> ProjectFact | None:
        self._ensure_writable()
        conn = self._require_conn()
        new_status = status if isinstance(status, FactStatus) else FactStatus(str(status))
        ts = updated_at or _utc_now_iso()

        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE facts
                SET status = ?, stale_reason = ?, updated_at = ?
                WHERE worktree_id = ? AND fact_id = ?
                """,
                (new_status.value, stale_reason.strip(), ts, worktree_id, fact_id),
            )
            if cursor.rowcount == 0:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return self.get(fact_id, worktree_id=worktree_id)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def evaluate_fact_freshness(
    root: Path | str, fact: ProjectFact
) -> tuple[FactStatus, str, tuple[str, ...], tuple[str, ...]]:
    """Evaluate a fact's source snapshots against files currently in ``root``."""
    if fact.status == FactStatus.INVALIDATED:
        return FactStatus.INVALIDATED, fact.stale_reason, (), ()
    if not fact.provenance.sources:
        return fact.status, fact.stale_reason, (), ()

    changed_paths: list[str] = []
    missing_paths: list[str] = []
    for snapshot in fact.provenance.sources:
        current_hash = hash_worktree_file(root, snapshot.path)
        if current_hash is None:
            missing_paths.append(snapshot.path)
        elif current_hash != snapshot.sha256:
            changed_paths.append(snapshot.path)

    if not changed_paths and not missing_paths:
        return fact.status, fact.stale_reason, (), ()

    reasons: list[str] = []
    if changed_paths:
        reasons.append("source hash changed: " + ", ".join(changed_paths))
    if missing_paths:
        reasons.append("missing source: " + ", ".join(missing_paths))
    return (
        FactStatus.STALE,
        "; ".join(reasons),
        tuple(changed_paths),
        tuple(missing_paths),
    )


def parse_fact_command_args(raw: str) -> ParsedFactInput:
    """Parse `/fact` arguments into structured source paths, tags, line range, and statement.

    Supported leading options:
      -f / --file PATH      (repeatable)
      -t / --tag TAG        (repeatable)
      --symbol NAME
      --line START[:END]
      --note TEXT
      --                    (end of options)
    """
    text = (raw or "").strip()
    if not text:
        raise FactValidationError("Fact statement cannot be empty.")

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise FactValidationError(f"Invalid /fact quoting: {exc}") from exc

    source_paths: list[str] = []
    tags: list[str] = []
    symbol = ""
    start_line: int | None = None
    end_line: int | None = None
    note = ""
    statement_tokens: list[str] = []

    idx = 0
    parsing_flags = True
    while idx < len(tokens):
        tok = tokens[idx]
        if parsing_flags and tok == "--":
            parsing_flags = False
            idx += 1
            continue
        if parsing_flags and tok in ("-f", "--file"):
            if idx + 1 >= len(tokens):
                raise FactValidationError(f"Option {tok} requires a file path.")
            source_paths.append(normalize_rel_path(tokens[idx + 1]))
            idx += 2
            continue
        if parsing_flags and tok in ("-t", "--tag"):
            if idx + 1 >= len(tokens):
                raise FactValidationError(f"Option {tok} requires a tag value.")
            tags.append(tokens[idx + 1].strip())
            idx += 2
            continue
        if parsing_flags and tok == "--symbol":
            if idx + 1 >= len(tokens):
                raise FactValidationError("Option --symbol requires a symbol name.")
            symbol = tokens[idx + 1].strip()
            idx += 2
            continue
        if parsing_flags and tok == "--note":
            if idx + 1 >= len(tokens):
                raise FactValidationError("Option --note requires a value.")
            note = tokens[idx + 1].strip()
            idx += 2
            continue
        if parsing_flags and tok == "--line":
            if idx + 1 >= len(tokens):
                raise FactValidationError("Option --line requires START or START:END.")
            raw_line = tokens[idx + 1].strip()
            parts = raw_line.split(":", 1)
            try:
                start_line = int(parts[0])
                end_line = int(parts[1]) if len(parts) == 2 else start_line
            except ValueError as exc:
                raise FactValidationError(f"Invalid --line specification: {raw_line!r}") from exc
            idx += 2
            continue

        parsing_flags = False
        statement_tokens.append(tok)
        idx += 1

    statement = " ".join(statement_tokens).strip()
    if not statement:
        raise FactValidationError("Fact statement cannot be empty.")

    return ParsedFactInput(
        statement=statement,
        source_paths=tuple(dict.fromkeys(source_paths)),
        tags=tuple(dict.fromkeys(t for t in tags if t)),
        symbol=symbol,
        start_line=start_line,
        end_line=end_line,
        note=note,
    )


def format_facts_context(
    facts: Sequence[ProjectFact],
    *,
    max_chars: int = MAX_PROMPT_FACTS_CHARS,
) -> str:
    """Render project facts into a bounded, read-only prompt context section."""
    if not facts or max_chars <= 0:
        return ""

    header = "Read-only project facts for this worktree:"
    lines: list[str] = [header]
    total_len = len(header)

    for fact in facts:
        markers: list[str] = []
        if fact.is_unverified:
            markers.append("UNVERIFIED")
        if fact.is_stale:
            markers.append("STALE")
        elif fact.status == FactStatus.INVALIDATED:
            markers.append("INVALIDATED")
        marker_prefix = f"[{' '.join(markers)}] " if markers else ""

        src_parts: list[str] = []
        for src in fact.provenance.sources:
            loc = src.path
            if src.start_line is not None:
                if src.end_line is not None and src.end_line != src.start_line:
                    loc = f"{loc}:{src.start_line}-{src.end_line}"
                else:
                    loc = f"{loc}:{src.start_line}"
            if src.symbol:
                loc = f"{loc}({src.symbol})"
            if src.sha256:
                loc = f"{loc}@{src.sha256[:8]}"
            src_parts.append(loc)

        provenance_bits = [f"origin={fact.provenance.origin}"]
        if fact.provenance.chat_id:
            provenance_bits.append(f"chat={fact.provenance.chat_id}")
        if src_parts:
            provenance_bits.append("sources=" + ", ".join(src_parts))
        if fact.stale_reason:
            provenance_bits.append(f"stale={fact.stale_reason}")

        entry = f"- {marker_prefix}{fact.statement} ({'; '.join(provenance_bits)})"
        candidate_len = total_len + 1 + len(entry)
        if candidate_len > max_chars:
            if len(lines) == 1:
                budget = max_chars - total_len - 4
                if budget > 0:
                    lines.append(entry[:budget] + "...")
                else:
                    return header[:max_chars]
            break
        lines.append(entry)
        total_len = candidate_len

    return "\n".join(lines)


class ProjectFactStore:
    """Worktree-scoped service facade over a FactRepository with graceful degradation."""

    def __init__(
        self,
        root: Path | str,
        *,
        enabled: bool,
        available: bool,
        repository: FactRepository | None = None,
        storage_path: Path | None = None,
        db_path: Path | None = None,
        backend: str = "",
        chat_id: str | None = None,
        chat_history_file: Path | str | None = None,
        diagnostic: str = "",
        read_only: bool = False,
        max_top_k: int = MAX_TOP_K,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._worktree_id = worktree_scope_id(self._root)
        resolved_chat = (chat_id or "").strip()
        self._chat_id = (
            resolved_chat if resolved_chat else chat_scope_id(self._root, chat_history_file)
        )
        self._enabled = bool(enabled)
        self._available = bool(available and repository is not None)
        self._repository = repository
        self._storage_path = storage_path if storage_path is not None else db_path
        if backend:
            self._backend = backend
        elif not self._enabled:
            self._backend = "disabled"
        elif isinstance(repository, JsonlFactRepository):
            self._backend = "jsonl"
        elif isinstance(repository, SqliteFtsFactRepository):
            self._backend = "sqlite"
        elif repository is not None:
            self._backend = "custom"
        else:
            self._backend = "disabled"
        self._diagnostic = diagnostic.strip()
        self._read_only = bool(read_only)
        self._max_top_k = max(1, min(int(max_top_k), MAX_TOP_K))

    @property
    def root(self) -> Path:
        return self._root

    @property
    def worktree_id(self) -> str:
        return self._worktree_id

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def available(self) -> bool:
        return self._available

    @property
    def active(self) -> bool:
        return self._enabled and self._available and self._repository is not None

    @property
    def storage_path(self) -> Path | None:
        return self._storage_path

    @property
    def db_path(self) -> Path | None:
        return self._storage_path

    @property
    def diagnostic(self) -> str:
        return self._diagnostic

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def repository(self) -> FactRepository | None:
        return self._repository

    def _require_active_repo(self) -> FactRepository:
        if not self.active or self._repository is None:
            raise FactStoreError(self._diagnostic or "Project facts store is unavailable.")
        return self._repository

    def _require_writable_repo(self) -> FactRepository:
        if self._read_only:
            raise FactStoreReadOnlyError("Project facts store is read-only.")
        return self._require_active_repo()

    def record_fact(
        self,
        statement: str,
        *,
        source_paths: Sequence[str] = (),
        tags: Sequence[str] = (),
        symbol: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        commit_sha: str = "",
        note: str = "",
        chat_id: str = "",
        origin: str = "user:/fact",
        fact_id: str | None = None,
        allow_missing_sources: bool = False,
    ) -> ProjectFact:
        """Record an explicitly confirmed project fact with captured source hashes."""
        repo = self._require_writable_repo()
        snapshots = capture_source_snapshots(
            self._root,
            source_paths,
            start_line=start_line,
            end_line=end_line,
            symbol=symbol,
            allow_missing=allow_missing_sources,
        )
        effective_chat_id = (chat_id or "").strip() or self._chat_id
        fact = ProjectFact.confirmed(
            worktree_id=self._worktree_id,
            statement=statement,
            sources=snapshots,
            origin=origin,
            tags=tags,
            commit_sha=commit_sha,
            note=note,
            chat_id=effective_chat_id,
            fact_id=fact_id,
        )
        return repo.add(fact)

    def record_candidate(
        self,
        statement: str,
        *,
        source_paths: Sequence[str] = (),
        tags: Sequence[str] = (),
        symbol: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        commit_sha: str = "",
        note: str = "",
        chat_id: str = "",
        origin: str = "model_candidate",
        fact_id: str | None = None,
        allow_missing_sources: bool = False,
    ) -> ProjectFact:
        """Record a model-proposed candidate fact, always marked unverified."""
        repo = self._require_writable_repo()
        snapshots = capture_source_snapshots(
            self._root,
            source_paths,
            start_line=start_line,
            end_line=end_line,
            symbol=symbol,
            allow_missing=allow_missing_sources,
        )
        effective_chat_id = (chat_id or "").strip() or self._chat_id
        fact = ProjectFact.model_candidate(
            worktree_id=self._worktree_id,
            statement=statement,
            sources=snapshots,
            origin=origin,
            tags=tags,
            commit_sha=commit_sha,
            note=note,
            chat_id=effective_chat_id,
            fact_id=fact_id,
        )
        return repo.add(fact)

    def confirm_fact(
        self,
        fact_id: str,
        *,
        origin: str = "user:/fact",
    ) -> ProjectFact | None:
        """Explicitly confirm an existing unverified candidate fact."""
        repo = self._require_writable_repo()
        existing = repo.get(fact_id, worktree_id=self._worktree_id)
        if existing is None:
            return None
        effective_chat_id = existing.provenance.chat_id or self._chat_id
        new_provenance = replace(
            existing.provenance,
            origin=origin,
            chat_id=effective_chat_id,
        )
        confirmed = replace(
            existing,
            verification=VerificationState.CONFIRMED,
            provenance=new_provenance,
            updated_at=_utc_now_iso(),
        )
        return repo.add(confirmed)

    def invalidate_fact(
        self,
        fact_id: str,
        *,
        reason: str = "invalidated",
    ) -> ProjectFact | None:
        """Explicitly invalidate a fact while preserving its provenance."""
        repo = self._require_writable_repo()
        return repo.update_status(
            fact_id,
            worktree_id=self._worktree_id,
            status=FactStatus.INVALIDATED,
            stale_reason=reason,
        )

    def handle_fact_command(
        self,
        raw_args: str,
        *,
        default_source_paths: Sequence[str] = (),
        commit_sha: str = "",
    ) -> FactCommandResult:
        """Handle an explicit `/fact` command without falling back to another store."""
        if not self.active:
            return FactCommandResult(
                ok=False,
                message=self._diagnostic or "Project facts are disabled.",
                fact=None,
            )
        if self._read_only:
            return FactCommandResult(
                ok=False,
                message="Project facts store is read-only.",
                fact=None,
            )
        try:
            parsed = parse_fact_command_args(raw_args)
            source_paths = parsed.source_paths or tuple(default_source_paths)
            fact = self.record_fact(
                parsed.statement,
                source_paths=source_paths,
                tags=parsed.tags,
                symbol=parsed.symbol,
                start_line=parsed.start_line,
                end_line=parsed.end_line,
                commit_sha=commit_sha,
                note=parsed.note,
                origin="user:/fact",
            )
        except (FactValidationError, FileNotFoundError, FactStoreError) as exc:
            return FactCommandResult(ok=False, message=str(exc), fact=None)

        return FactCommandResult(
            ok=True,
            message=f"Recorded confirmed project fact {fact.fact_id}.",
            fact=fact,
        )

    def get(self, fact_id: str) -> ProjectFact | None:
        if not self.active or self._repository is None:
            return None
        return self._repository.get(fact_id, worktree_id=self._worktree_id)

    def revalidate(
        self,
        *,
        paths: Sequence[str] | None = None,
        persist: bool = True,
    ) -> RevalidationReport:
        """Check source hashes for facts in this worktree and mark drifted entries stale."""
        if not self.active or self._repository is None:
            return RevalidationReport(checked=0, active=0, stale=0, invalidated=0)

        facts = self._repository.list_all(
            worktree_id=self._worktree_id,
            include_unverified=True,
            include_stale=True,
        )
        path_filter = (
            {normalize_rel_path(p) for p in paths} if paths is not None else None
        )

        checked = 0
        active_count = 0
        stale_count = 0
        invalidated_count = 0
        changes: list[RevalidationChange] = []
        can_persist = persist and not self._read_only

        for fact in facts:
            if path_filter is not None and not path_filter.intersection(fact.source_paths):
                continue
            checked += 1
            new_status, reason, changed_paths, missing_paths = evaluate_fact_freshness(
                self._root, fact
            )
            if new_status != fact.status or (
                new_status == FactStatus.STALE and reason and reason != fact.stale_reason
            ):
                if can_persist:
                    self._repository.update_status(
                        fact.fact_id,
                        worktree_id=self._worktree_id,
                        status=new_status,
                        stale_reason=reason,
                    )
                if new_status != fact.status:
                    changes.append(
                        RevalidationChange(
                            fact_id=fact.fact_id,
                            previous_status=fact.status,
                            new_status=new_status,
                            reason=reason,
                            changed_paths=changed_paths,
                            missing_paths=missing_paths,
                        )
                    )
            if new_status == FactStatus.ACTIVE:
                active_count += 1
            elif new_status == FactStatus.STALE:
                stale_count += 1
            else:
                invalidated_count += 1

        return RevalidationReport(
            checked=checked,
            active=active_count,
            stale=stale_count,
            invalidated=invalidated_count,
            changes=tuple(changes),
        )

    def _apply_freshness_filter(
        self,
        facts: Sequence[ProjectFact],
        *,
        include_stale: bool,
        revalidate: bool,
    ) -> tuple[ProjectFact, ...]:
        if not revalidate or not facts:
            return tuple(facts)

        can_persist = not self._read_only and self._repository is not None
        filtered: list[ProjectFact] = []
        for fact in facts:
            new_status, reason, _, _ = evaluate_fact_freshness(self._root, fact)
            effective = fact
            if new_status != fact.status or (
                new_status == FactStatus.STALE and reason and reason != fact.stale_reason
            ):
                if can_persist and self._repository is not None:
                    updated = self._repository.update_status(
                        fact.fact_id,
                        worktree_id=self._worktree_id,
                        status=new_status,
                        stale_reason=reason,
                    )
                    effective = updated or replace(
                        fact, status=new_status, stale_reason=reason
                    )
                else:
                    effective = replace(fact, status=new_status, stale_reason=reason)
            if include_stale or effective.status == FactStatus.ACTIVE:
                filtered.append(effective)
        return tuple(filtered)

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
        revalidate: bool = True,
    ) -> tuple[ProjectFact, ...]:
        if not self.active or self._repository is None:
            return ()
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        if limit == 0:
            return ()
        if revalidate:
            self.revalidate(persist=not self._read_only)
        fetch_limit = (
            self._max_top_k if (self._read_only and revalidate and not include_stale) else limit
        )
        raw_results = self._repository.search(
            query,
            worktree_id=self._worktree_id,
            top_k=fetch_limit,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )
        return self._apply_freshness_filter(
            raw_results, include_stale=include_stale, revalidate=revalidate
        )[:limit]

    def list_for_paths(
        self,
        paths: Sequence[str],
        *,
        top_k: int = DEFAULT_TOP_K,
        include_unverified: bool = False,
        include_stale: bool = False,
        revalidate: bool = True,
    ) -> tuple[ProjectFact, ...]:
        if not self.active or self._repository is None:
            return ()
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        if limit == 0 or not paths:
            return ()
        if revalidate:
            self.revalidate(paths=paths, persist=not self._read_only)
        fetch_limit = (
            self._max_top_k if (self._read_only and revalidate and not include_stale) else limit
        )
        raw_results = self._repository.list_for_paths(
            paths,
            worktree_id=self._worktree_id,
            top_k=fetch_limit,
            include_unverified=include_unverified,
            include_stale=include_stale,
        )
        return self._apply_freshness_filter(
            raw_results, include_stale=include_stale, revalidate=revalidate
        )[:limit]

    def format_prompt_context(
        self,
        *,
        query: str = "",
        paths: Sequence[str] = (),
        top_k: int = DEFAULT_TOP_K,
        max_chars: int = MAX_PROMPT_FACTS_CHARS,
    ) -> str:
        """Retrieve confirmed active facts for query/paths and render read-only context."""
        if not self.active:
            return ""
        limit = _bound_top_k(top_k, max_top_k=self._max_top_k)
        if limit == 0:
            return ""
        collected: list[ProjectFact] = []
        seen: set[str] = set()
        if paths:
            for fact in self.list_for_paths(paths, top_k=limit):
                if fact.fact_id not in seen:
                    seen.add(fact.fact_id)
                    collected.append(fact)
        if query and len(collected) < limit:
            for fact in self.search(query, top_k=limit):
                if fact.fact_id not in seen:
                    seen.add(fact.fact_id)
                    collected.append(fact)
                    if len(collected) >= limit:
                        break
        return format_facts_context(collected[:limit], max_chars=max_chars)

    def close(self) -> None:
        if self._repository is not None:
            self._repository.close()

    def __enter__(self) -> ProjectFactStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def open_project_fact_store(
    root: Path | str,
    *,
    enabled: bool = False,
    facts_db: Path | str | None = None,
    facts_file: Path | str | None = None,
    chat_id: str | None = None,
    chat_history_file: Path | str | None = None,
    read_only: bool = False,
    max_top_k: int = MAX_TOP_K,
    repository: FactRepository | None = None,
    sqlite_module: Any = _UNSET,
) -> ProjectFactStore:
    """Open or degrade a worktree-scoped ProjectFactStore.

    - When ``enabled`` is False, skips probing, opening, or creating any store.
    - When ``facts_db`` is omitted/blank, opens the JSONL store at
      ``resolve_facts_jsonl_path(root, facts_file)`` with deterministic ``chat_id``.
    - When ``facts_db`` is provided, parses it via ``parse_facts_db_url`` and opens
      the embedded SQLite FTS5 store. On invalid URL/scheme or missing ``sqlite3`` /
      FTS5 support, reports a clear diagnostic, leaves existing files untouched,
      writes no fallback store, and returns an inactive store.
    """
    root_path = Path(root).expanduser().resolve()
    resolved_chat_id = (chat_id or "").strip() or chat_scope_id(root_path, chat_history_file)

    if not enabled:
        return ProjectFactStore(
            root_path,
            enabled=False,
            available=False,
            repository=None,
            storage_path=None,
            backend="disabled",
            chat_id=resolved_chat_id,
            diagnostic="Project facts are disabled.",
            read_only=read_only,
            max_top_k=max_top_k,
        )

    if repository is not None:
        if isinstance(repository, JsonlFactRepository):
            repo_backend = "jsonl"
            repo_path: Path | None = repository.file_path
        elif isinstance(repository, SqliteFtsFactRepository):
            repo_backend = "sqlite"
            repo_path = repository.db_path
        else:
            repo_backend = "custom"
            repo_path = None
        return ProjectFactStore(
            root_path,
            enabled=True,
            available=True,
            repository=repository,
            storage_path=repo_path,
            backend=repo_backend,
            chat_id=resolved_chat_id,
            diagnostic="",
            read_only=read_only,
            max_top_k=max_top_k,
        )

    has_facts_db = facts_db is not None and (
        not isinstance(facts_db, str) or bool(facts_db.strip())
    )
    if has_facts_db:
        try:
            resolved_db = parse_facts_db_url(root_path, facts_db)
        except FactValidationError as exc:
            return ProjectFactStore(
                root_path,
                enabled=True,
                available=False,
                repository=None,
                storage_path=None,
                backend="sqlite",
                chat_id=resolved_chat_id,
                diagnostic=f"Project facts disabled: {exc}",
                read_only=read_only,
                max_top_k=max_top_k,
            )

        probe = check_sqlite_fts5_support(sqlite_module=sqlite_module)
        if not probe.available:
            return ProjectFactStore(
                root_path,
                enabled=True,
                available=False,
                repository=None,
                storage_path=resolved_db,
                backend="sqlite",
                chat_id=resolved_chat_id,
                diagnostic=probe.message,
                read_only=read_only,
                max_top_k=max_top_k,
            )

        if read_only and not resolved_db.exists():
            return ProjectFactStore(
                root_path,
                enabled=True,
                available=False,
                repository=None,
                storage_path=resolved_db,
                backend="sqlite",
                chat_id=resolved_chat_id,
                diagnostic=(
                    f"Project facts database does not exist at {resolved_db} (read-only mode)."
                ),
                read_only=True,
                max_top_k=max_top_k,
            )

        repo = SqliteFtsFactRepository(
            resolved_db,
            max_top_k=max_top_k,
            read_only=read_only,
            sqlite_module=sqlite_module,
        )
        return ProjectFactStore(
            root_path,
            enabled=True,
            available=True,
            repository=repo,
            storage_path=resolved_db,
            backend="sqlite",
            chat_id=resolved_chat_id,
            diagnostic="",
            read_only=read_only,
            max_top_k=max_top_k,
        )

    resolved_jsonl = resolve_facts_jsonl_path(root_path, facts_file)
    if read_only and not resolved_jsonl.exists():
        return ProjectFactStore(
            root_path,
            enabled=True,
            available=False,
            repository=None,
            storage_path=resolved_jsonl,
            backend="jsonl",
            chat_id=resolved_chat_id,
            diagnostic=(
                f"Project facts file does not exist at {resolved_jsonl} (read-only mode)."
            ),
            read_only=True,
            max_top_k=max_top_k,
        )

    jsonl_repo = JsonlFactRepository(
        resolved_jsonl,
        max_top_k=max_top_k,
        read_only=read_only,
    )
    return ProjectFactStore(
        root_path,
        enabled=True,
        available=True,
        repository=jsonl_repo,
        storage_path=resolved_jsonl,
        backend="jsonl",
        chat_id=resolved_chat_id,
        diagnostic="",
        read_only=read_only,
        max_top_k=max_top_k,
    )
