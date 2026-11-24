"""Runnable replay corpus loader, validator, and CaseSpec adapter.

Materializes version-pinned tasks from local pinned git checkouts, wires external
hidden verification commands and files without leaking them into candidate workspaces,
validates fixture integrity and fail-to-pass behavior, and enforces documented
preregistration metadata.

No corpus tasks or external downloads are bundled. Callers must supply a local JSON
manifest via ``manifest_path`` or the ``REPLAY_CORPUS_MANIFEST`` environment variable.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
from dataclasses import dataclass, field, replace
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
import zlib

SCHEMA_VERSION = "1.0"
MANIFEST_ENV_VAR = "REPLAY_CORPUS_MANIFEST"
ALLOWED_DIFFICULTIES = ("easy", "medium", "hard")
ALLOWED_FILE_SCOPES = ("single_file", "multi_file")
ALLOWED_PLACEMENTS = ("workspace", "hidden_dir")
ALLOWED_CUTOFF_DIRECTIONS = ("on_or_after", "on_or_before")
INLINE_HIDDEN_BYTES_LIMIT = 32_768

PREREGISTRATION_FIELD_DOCS: dict[str, str] = {
    "protocol_version": "Version string identifying the trial evaluation protocol.",
    "preregistration_id": "Unique slug identifying this preregistered corpus freeze.",
    "registered_at": "ISO-8601 timestamp with timezone recording when the corpus was registered.",
    "frozen": "Boolean indicating whether the manifest is frozen for evaluation runs.",
    "hypothesis": "Primary experimental hypothesis tested by this replay corpus.",
    "primary_metric": "Primary outcome metric used to adjudicate the hypothesis.",
    "secondary_metrics": (
        "Ordered tuple of secondary diagnostic metrics recorded alongside primary_metric."
    ),
    "inclusion_criteria": (
        "Non-empty tuple of documented criteria required for a commit/task to enter the corpus."
    ),
    "exclusion_criteria": (
        "Non-empty tuple of documented criteria that disqualify candidate commits/tasks."
    ),
    "stopping_rule": "Documented rule specifying when corpus construction or evaluation stops.",
    "leakage_controls": (
        "Non-empty tuple of documented controls preventing target/test leakage"
        " into prompts or snapshots."
    ),
    "cutoff_date": "Optional ISO-8601 date (YYYY-MM-DD) governing temporal contamination checks.",
    "cutoff_direction": (
        "How case commit_date is checked against cutoff_date:"
        " 'on_or_after' or 'on_or_before'."
    ),
    "manifest_sha256": (
        "Optional SHA-256 hex digest of the canonical manifest"
        " (with manifest_sha256 set to null) for tamper detection."
    ),
}

CASE_PREREGISTRATION_FIELD_DOCS: dict[str, str] = {
    "selection_rationale": (
        "Documented rationale explaining why this case satisfies corpus inclusion_criteria."
    ),
    "source_origin": (
        "Human-readable provenance label (e.g. repo and issue/PR identifier);"
        " metadata only, never fetched."
    ),
    "commit_date": (
        "Optional ISO-8601 date (YYYY-MM-DD) of the historical commit"
        " for cutoff_date verification."
    ),
    "expected_difficulty": "Preregistered difficulty tier: 'easy', 'medium', or 'hard'.",
    "file_scope_category": (
        "Preregistered edit scope: 'single_file' (1 editable file)"
        " or 'multi_file' (2+ editable files)."
    ),
    "leakage_checked": (
        "Boolean attestation that task_prompt and explicit_guidance"
        " do not leak hidden tests or fix diffs."
    ),
    "preregistered_notes": (
        "Optional pre-run notes or caveats documented prior to trial execution."
    ),
}

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REMOTE_SCHEME_RE = re.compile(r"^(?:https?|ssh|git|ftp|ftps|file)://", re.IGNORECASE)
_SCP_REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:.+")


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    code: str
    message: str
    case_id: str | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "case_id": self.case_id,
            "field": self.field,
        }


class FixtureValidationError(ValueError):
    """Raised when a replay manifest or fixture fails validation."""

    def __init__(self, message: str, issues: Sequence[ValidationIssue] = ()) -> None:
        super().__init__(message)
        self.issues = tuple(issues)


@dataclass(frozen=True)
class CaseValidationResult:
    case_id: str
    file_count: int
    files_sha256: str
    hidden_file_digests: dict[str, str]
    base_smoke_passed: bool | None = None
    base_hidden_failed: bool | None = None
    fix_hidden_passed: bool | None = None
    fix_smoke_passed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "file_count": self.file_count,
            "files_sha256": self.files_sha256,
            "hidden_file_digests": dict(self.hidden_file_digests),
            "base_smoke_passed": self.base_smoke_passed,
            "base_hidden_failed": self.base_hidden_failed,
            "fix_hidden_passed": self.fix_hidden_passed,
            "fix_smoke_passed": self.fix_smoke_passed,
        }


@dataclass(frozen=True)
class ValidationReport:
    manifest_sha256: str
    errors: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]
    case_results: dict[str, CaseValidationResult]

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "manifest_sha256": self.manifest_sha256,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "cases": {
                cid: res.to_dict() for cid, res in sorted(self.case_results.items())
            },
        }


def _reject_unknown_keys(
    data: Mapping[str, Any],
    allowed: set[str],
    context: str,
) -> None:
    if not isinstance(data, Mapping):
        raise FixtureValidationError(f"{context} must be a JSON object")
    unknown = sorted(set(data.keys()) - allowed)
    if unknown:
        raise FixtureValidationError(
            f"Unknown keys in {context}: {', '.join(unknown)}"
        )


def _require_str(data: Mapping[str, Any], key: str, context: str) -> str:
    if key not in data or not isinstance(data[key], str):
        raise FixtureValidationError(f"{context}.{key} must be a string")
    return data[key]


def _optional_str(data: Mapping[str, Any], key: str, context: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise FixtureValidationError(f"{context}.{key} must be a string or null")
    return value


def _require_bool(data: Mapping[str, Any], key: str, context: str) -> bool:
    if key not in data or not isinstance(data[key], bool):
        raise FixtureValidationError(f"{context}.{key} must be a boolean")
    return data[key]


def _str_tuple(
    data: Mapping[str, Any],
    key: str,
    context: str,
    *,
    required: bool = True,
) -> tuple[str, ...]:
    if key not in data or data[key] is None:
        if required:
            raise FixtureValidationError(f"{context}.{key} must be a list of strings")
        return ()
    raw = data[key]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise FixtureValidationError(f"{context}.{key} must be a list of strings")
    return tuple(raw)


def _str_mapping(data: Mapping[str, Any], key: str, context: str) -> dict[str, str]:
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise FixtureValidationError(
            f"{context}.{key} must be an object mapping strings to strings"
        )
    return dict(raw)


@dataclass(frozen=True)
class PreregistrationSpec:
    protocol_version: str
    preregistration_id: str
    registered_at: str
    frozen: bool
    hypothesis: str
    primary_metric: str
    inclusion_criteria: tuple[str, ...]
    exclusion_criteria: tuple[str, ...]
    stopping_rule: str
    leakage_controls: tuple[str, ...]
    secondary_metrics: tuple[str, ...] = ()
    cutoff_date: str | None = None
    cutoff_direction: str = "on_or_after"
    manifest_sha256: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreregistrationSpec:
        allowed = set(PREREGISTRATION_FIELD_DOCS.keys())
        _reject_unknown_keys(data, allowed, "preregistration")
        return cls(
            protocol_version=_require_str(data, "protocol_version", "preregistration"),
            preregistration_id=_require_str(data, "preregistration_id", "preregistration"),
            registered_at=_require_str(data, "registered_at", "preregistration"),
            frozen=_require_bool(data, "frozen", "preregistration"),
            hypothesis=_require_str(data, "hypothesis", "preregistration"),
            primary_metric=_require_str(data, "primary_metric", "preregistration"),
            secondary_metrics=_str_tuple(
                data, "secondary_metrics", "preregistration", required=False
            ),
            inclusion_criteria=_str_tuple(data, "inclusion_criteria", "preregistration"),
            exclusion_criteria=_str_tuple(data, "exclusion_criteria", "preregistration"),
            stopping_rule=_require_str(data, "stopping_rule", "preregistration"),
            leakage_controls=_str_tuple(data, "leakage_controls", "preregistration"),
            cutoff_date=_optional_str(data, "cutoff_date", "preregistration"),
            cutoff_direction=(
                _optional_str(data, "cutoff_direction", "preregistration")
                or "on_or_after"
            ),
            manifest_sha256=_optional_str(data, "manifest_sha256", "preregistration"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "preregistration_id": self.preregistration_id,
            "registered_at": self.registered_at,
            "frozen": self.frozen,
            "hypothesis": self.hypothesis,
            "primary_metric": self.primary_metric,
            "secondary_metrics": list(self.secondary_metrics),
            "inclusion_criteria": list(self.inclusion_criteria),
            "exclusion_criteria": list(self.exclusion_criteria),
            "stopping_rule": self.stopping_rule,
            "leakage_controls": list(self.leakage_controls),
            "cutoff_date": self.cutoff_date,
            "cutoff_direction": self.cutoff_direction,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True)
class CasePreregistration:
    selection_rationale: str
    source_origin: str
    expected_difficulty: str
    file_scope_category: str
    leakage_checked: bool
    commit_date: str | None = None
    preregistered_notes: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], context: str) -> CasePreregistration:
        allowed = set(CASE_PREREGISTRATION_FIELD_DOCS.keys())
        _reject_unknown_keys(data, allowed, context)
        return cls(
            selection_rationale=_require_str(data, "selection_rationale", context),
            source_origin=_require_str(data, "source_origin", context),
            commit_date=_optional_str(data, "commit_date", context),
            expected_difficulty=_require_str(data, "expected_difficulty", context),
            file_scope_category=_require_str(data, "file_scope_category", context),
            leakage_checked=_require_bool(data, "leakage_checked", context),
            preregistered_notes=_optional_str(data, "preregistered_notes", context) or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_rationale": self.selection_rationale,
            "source_origin": self.source_origin,
            "commit_date": self.commit_date,
            "expected_difficulty": self.expected_difficulty,
            "file_scope_category": self.file_scope_category,
            "leakage_checked": self.leakage_checked,
            "preregistered_notes": self.preregistered_notes,
        }


@dataclass(frozen=True)
class SourcePin:
    repo_path: str
    base_commit: str
    fix_commit: str | None = None
    expected_tree_sha: str | None = None
    expected_files_sha256: str | None = None
    subdir: str = ""
    include_globs: tuple[str, ...] = ()
    exclude_globs: tuple[str, ...] = ()
    max_file_bytes: int = 524_288
    skip_binary: bool = True
    version_label: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], context: str) -> SourcePin:
        allowed = {
            "repo_path",
            "base_commit",
            "fix_commit",
            "expected_tree_sha",
            "expected_files_sha256",
            "subdir",
            "include_globs",
            "exclude_globs",
            "max_file_bytes",
            "skip_binary",
            "version_label",
        }
        _reject_unknown_keys(data, allowed, context)
        max_file_bytes = data.get("max_file_bytes", 524_288)
        if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool):
            raise FixtureValidationError(f"{context}.max_file_bytes must be an integer")
        skip_binary = data.get("skip_binary", True)
        if not isinstance(skip_binary, bool):
            raise FixtureValidationError(f"{context}.skip_binary must be a boolean")
        return cls(
            repo_path=_require_str(data, "repo_path", context),
            base_commit=_require_str(data, "base_commit", context),
            fix_commit=_optional_str(data, "fix_commit", context),
            expected_tree_sha=_optional_str(data, "expected_tree_sha", context),
            expected_files_sha256=_optional_str(data, "expected_files_sha256", context),
            subdir=_optional_str(data, "subdir", context) or "",
            include_globs=_str_tuple(data, "include_globs", context, required=False),
            exclude_globs=_str_tuple(data, "exclude_globs", context, required=False),
            max_file_bytes=max_file_bytes,
            skip_binary=skip_binary,
            version_label=_optional_str(data, "version_label", context),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "base_commit": self.base_commit,
            "fix_commit": self.fix_commit,
            "expected_tree_sha": self.expected_tree_sha,
            "expected_files_sha256": self.expected_files_sha256,
            "subdir": self.subdir,
            "include_globs": list(self.include_globs),
            "exclude_globs": list(self.exclude_globs),
            "max_file_bytes": self.max_file_bytes,
            "skip_binary": self.skip_binary,
            "version_label": self.version_label,
        }


@dataclass(frozen=True)
class HiddenFileSpec:
    target_path: str
    source_path: str | None = None
    git_commit: str | None = None
    git_path: str | None = None
    placement: str = "workspace"
    sha256: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], context: str) -> HiddenFileSpec:
        allowed = {
            "target_path",
            "source_path",
            "git_commit",
            "git_path",
            "placement",
            "sha256",
        }
        _reject_unknown_keys(data, allowed, context)
        return cls(
            target_path=_require_str(data, "target_path", context),
            source_path=_optional_str(data, "source_path", context),
            git_commit=_optional_str(data, "git_commit", context),
            git_path=_optional_str(data, "git_path", context),
            placement=_optional_str(data, "placement", context) or "workspace",
            sha256=_optional_str(data, "sha256", context),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_path": self.target_path,
            "source_path": self.source_path,
            "git_commit": self.git_commit,
            "git_path": self.git_path,
            "placement": self.placement,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class HiddenSpec:
    command: tuple[str, ...]
    files: tuple[HiddenFileSpec, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    expected_exit_code: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], context: str) -> HiddenSpec:
        allowed = {
            "command",
            "files",
            "cwd",
            "env",
            "timeout_seconds",
            "expected_exit_code",
        }
        _reject_unknown_keys(data, allowed, context)
        raw_files = data.get("files") or []
        if not isinstance(raw_files, list):
            raise FixtureValidationError(f"{context}.files must be a list")
        parsed_files = tuple(
            HiddenFileSpec.from_dict(item, f"{context}.files[{idx}]")
            for idx, item in enumerate(raw_files)
        )
        timeout = data.get("timeout_seconds", 120.0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise FixtureValidationError(f"{context}.timeout_seconds must be a number")
        exit_code = data.get("expected_exit_code", 0)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise FixtureValidationError(
                f"{context}.expected_exit_code must be an integer"
            )
        return cls(
            command=_str_tuple(data, "command", context),
            files=parsed_files,
            cwd=_optional_str(data, "cwd", context),
            env=_str_mapping(data, "env", context),
            timeout_seconds=float(timeout),
            expected_exit_code=exit_code,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "files": [hf.to_dict() for hf in self.files],
            "cwd": self.cwd,
            "env": dict(sorted(self.env.items())),
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_code": self.expected_exit_code,
        }


@dataclass(frozen=True)
class SmokeSpec:
    command: tuple[str, ...] = ()
    script: str | None = None
    timeout_seconds: float = 30.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], context: str) -> SmokeSpec:
        allowed = {"command", "script", "timeout_seconds"}
        _reject_unknown_keys(data, allowed, context)
        timeout = data.get("timeout_seconds", 30.0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise FixtureValidationError(f"{context}.timeout_seconds must be a number")
        return cls(
            command=_str_tuple(data, "command", context, required=False),
            script=_optional_str(data, "script", context),
            timeout_seconds=float(timeout),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "script": self.script,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class ReplayCaseEntry:
    case_id: str
    task_prompt: str
    explicit_guidance: str
    editable_files: tuple[str, ...]
    source: SourcePin
    hidden: HiddenSpec
    preregistration: CasePreregistration
    smoke: SmokeSpec | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], context: str) -> ReplayCaseEntry:
        allowed = {
            "case_id",
            "task_prompt",
            "explicit_guidance",
            "editable_files",
            "source",
            "hidden",
            "smoke",
            "preregistration",
        }
        _reject_unknown_keys(data, allowed, context)
        case_id = _require_str(data, "case_id", context)
        smoke_raw = data.get("smoke")
        smoke = (
            SmokeSpec.from_dict(smoke_raw, f"{context}.smoke")
            if smoke_raw is not None
            else None
        )
        return cls(
            case_id=case_id,
            task_prompt=_require_str(data, "task_prompt", context),
            explicit_guidance=_require_str(data, "explicit_guidance", context),
            editable_files=_str_tuple(data, "editable_files", context),
            source=SourcePin.from_dict(data.get("source"), f"{context}.source"),
            hidden=HiddenSpec.from_dict(data.get("hidden"), f"{context}.hidden"),
            smoke=smoke,
            preregistration=CasePreregistration.from_dict(
                data.get("preregistration"), f"{context}.preregistration"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "task_prompt": self.task_prompt,
            "explicit_guidance": self.explicit_guidance,
            "editable_files": list(self.editable_files),
            "source": self.source.to_dict(),
            "hidden": self.hidden.to_dict(),
            "smoke": self.smoke.to_dict() if self.smoke is not None else None,
            "preregistration": self.preregistration.to_dict(),
        }


@dataclass(frozen=True)
class ReplayManifest:
    schema_version: str
    preregistration: PreregistrationSpec
    cases: tuple[ReplayCaseEntry, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplayManifest:
        _reject_unknown_keys(
            data, {"schema_version", "preregistration", "cases"}, "manifest"
        )
        schema_version = _require_str(data, "schema_version", "manifest")
        prereg = PreregistrationSpec.from_dict(data.get("preregistration"))
        raw_cases = data.get("cases")
        if not isinstance(raw_cases, list):
            raise FixtureValidationError("manifest.cases must be a list")
        parsed_cases = tuple(
            ReplayCaseEntry.from_dict(item, f"cases[{idx}]")
            for idx, item in enumerate(raw_cases)
        )
        return cls(
            schema_version=schema_version,
            preregistration=prereg,
            cases=parsed_cases,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "preregistration": self.preregistration.to_dict(),
            "cases": [case.to_dict() for case in self.cases],
        }


@dataclass(frozen=True)
class ResolvedHiddenFile:
    spec: HiddenFileSpec
    content_bytes: bytes
    sha256: str
    resolved_source_path: Path | None = None
    resolved_repo_path: Path | None = None


@dataclass(frozen=True)
class MaterializedCase:
    entry: ReplayCaseEntry
    files: dict[str, str]
    files_sha256: str
    hidden_files: tuple[ResolvedHiddenFile, ...]
    resolved_repo_path: Path


def _is_remote_location(value: str) -> bool:
    stripped = value.strip()
    return bool(_REMOTE_SCHEME_RE.match(stripped) or _SCP_REMOTE_RE.match(stripped))


def _is_full_hex_oid(value: str) -> bool:
    return len(value) in (40, 64) and bool(_HEX_RE.match(value))


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and bool(_HEX_RE.match(value))


def _validate_iso_timestamp(value: str) -> bool:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.tzinfo is not None


def _validate_iso_date(value: str) -> date | None:
    if not _DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _validate_rel_posix_path(path: str, *, allow_empty: bool = False) -> str | None:
    if path == "":
        return None if allow_empty else "must be a non-empty relative POSIX path"
    if "\x00" in path:
        return "must not contain NUL bytes"
    if "\\" in path:
        return "must use forward slashes ('/')"
    if path.startswith("/") or path.endswith("/"):
        return "must be a normalized relative path without leading or trailing '/'"
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return f"contains invalid path segment {part!r}"
        if part == ".git":
            return "must not reference '.git'"
    return None


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    i = 0
    n = len(pattern)
    out = ["^"]
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(ch))
                i += 1
            else:
                inner = pattern[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append(f"[{inner}]")
                i = j + 1
        else:
            out.append(re.escape(ch))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _matches_any_glob(path: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.match(path) is not None for pattern in patterns)


def compute_files_digest(files: Mapping[str, str]) -> str:
    """Compute a canonical SHA-256 digest over a materialized path -> text map."""
    hasher = hashlib.sha256()
    for relpath in sorted(files):
        path_bytes = relpath.encode("utf-8")
        content_bytes = files[relpath].encode("utf-8")
        hasher.update(path_bytes)
        hasher.update(b"\x00")
        hasher.update(str(len(content_bytes)).encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(content_bytes)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def compute_manifest_digest(manifest: ReplayManifest) -> str:
    """Compute the canonical SHA-256 preregistration digest of a manifest.

    ``preregistration.manifest_sha256`` is normalized to ``null`` prior to hashing
    so the digest can be stored inside the manifest without circularity.
    """
    payload = manifest.to_dict()
    payload["preregistration"]["manifest_sha256"] = None
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _resolve_local_path(raw_path: str, base_dir: Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    return env


def _run_git(
    repo_path: Path,
    args: Sequence[str],
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
        env=_git_env(),
    )


def _verify_local_git_repo(repo_path: Path) -> None:
    if not repo_path.exists():
        raise FixtureValidationError(f"Local git checkout does not exist: {repo_path}")
    proc = _run_git(repo_path, ["rev-parse", "--git-dir"])
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise FixtureValidationError(
            f"Path is not a valid local git repository: {repo_path} ({detail})"
        )


def _resolve_git_commit(repo_path: Path, commit: str) -> str:
    if not _is_full_hex_oid(commit):
        raise FixtureValidationError(
            f"Git commit must be a full 40- or 64-char lowercase hex SHA, got {commit!r}"
        )
    proc = _run_git(repo_path, ["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"])
    if proc.returncode != 0:
        raise FixtureValidationError(
            f"Pinned git commit {commit} not found in local checkout {repo_path}"
        )
    resolved = proc.stdout.decode("ascii").strip()
    if resolved != commit:
        raise FixtureValidationError(
            f"Git commit pin mismatch: expected {commit}, resolved {resolved}"
        )
    return resolved


def _resolve_git_tree(repo_path: Path, rev: str) -> str:
    proc = _run_git(repo_path, ["rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}"])
    if proc.returncode != 0:
        raise FixtureValidationError(
            f"Git tree {rev!r} not found in local checkout {repo_path}"
        )
    return proc.stdout.decode("ascii").strip()


def _list_tree_blobs(
    repo_path: Path, commit: str, subdir: str = ""
) -> list[tuple[str, str]]:
    tree_spec = f"{commit}:{subdir}" if subdir else commit
    if subdir:
        _resolve_git_tree(repo_path, tree_spec)
    proc = _run_git(repo_path, ["ls-tree", "-r", "-z", tree_spec])
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise FixtureValidationError(
            f"Failed to list git tree {tree_spec} in {repo_path}: {detail}"
        )
    entries: list[tuple[str, str]] = []
    for raw_entry in proc.stdout.split(b"\x00"):
        if not raw_entry:
            continue
        meta_bytes, sep, path_bytes = raw_entry.partition(b"\t")
        if not sep:
            raise FixtureValidationError(
                f"Malformed git ls-tree entry in {repo_path} at {tree_spec}"
            )
        meta_parts = meta_bytes.decode("ascii").split(" ")
        if len(meta_parts) != 3:
            raise FixtureValidationError(
                f"Malformed git ls-tree metadata in {repo_path}: {meta_parts!r}"
            )
        mode, obj_type, oid = meta_parts
        if obj_type != "blob" or mode not in ("100644", "100755"):
            continue
        try:
            relpath = path_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FixtureValidationError(
                f"Non-UTF-8 path in git tree {tree_spec}: {path_bytes!r}"
            ) from exc
        path_err = _validate_rel_posix_path(relpath)
        if path_err is not None:
            raise FixtureValidationError(
                f"Unsafe path {relpath!r} in git tree {tree_spec}: {path_err}"
            )
        entries.append((relpath, oid))
    return entries


def _batch_read_blobs(
    repo_path: Path, items: Sequence[tuple[str, str]]
) -> dict[str, bytes]:
    if not items:
        return {}
    stdin_bytes = "".join(f"{oid}\n" for _, oid in items).encode("ascii")
    proc = _run_git(repo_path, ["cat-file", "--batch"], input_bytes=stdin_bytes)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise FixtureValidationError(
            f"git cat-file --batch failed in {repo_path}: {detail}"
        )
    buf = proc.stdout
    pos = 0
    result: dict[str, bytes] = {}
    for relpath, expected_oid in items:
        nl = buf.find(b"\n", pos)
        if nl == -1:
            raise FixtureValidationError(
                f"Unexpected EOF from git cat-file --batch reading {relpath}"
            )
        header = buf[pos:nl].decode("ascii")
        pos = nl + 1
        parts = header.split(" ")
        if len(parts) == 2 and parts[1] == "missing":
            raise FixtureValidationError(
                f"Missing git blob {expected_oid} for {relpath} in {repo_path}"
            )
        if len(parts) != 3 or parts[1] != "blob":
            raise FixtureValidationError(
                f"Unexpected git cat-file header {header!r} for {relpath}"
            )
        size = int(parts[2])
        content = buf[pos : pos + size]
        if len(content) != size:
            raise FixtureValidationError(
                f"Truncated git blob for {relpath} in {repo_path}"
            )
        pos += size
        if buf[pos : pos + 1] != b"\n":
            raise FixtureValidationError(
                f"Missing record delimiter after git blob {relpath}"
            )
        pos += 1
        result[relpath] = content
    if pos != len(buf):
        raise FixtureValidationError(
            f"Trailing unread bytes ({len(buf) - pos}) from git cat-file --batch"
        )
    return result


def _read_single_git_blob(repo_path: Path, commit: str, git_path: str) -> bytes:
    _resolve_git_commit(repo_path, commit)
    proc = _run_git(repo_path, ["cat-file", "-t", f"{commit}:{git_path}"])
    if proc.returncode != 0 or proc.stdout.decode("ascii", "replace").strip() != "blob":
        raise FixtureValidationError(
            f"Git blob {commit}:{git_path} not found in {repo_path}"
        )
    show_proc = _run_git(repo_path, ["show", f"{commit}:{git_path}"])
    if show_proc.returncode != 0:
        detail = show_proc.stderr.decode("utf-8", "replace").strip()
        raise FixtureValidationError(
            f"Failed to read git blob {commit}:{git_path}: {detail}"
        )
    return show_proc.stdout


def materialize_source(
    source: SourcePin,
    *,
    base_dir: Path,
    editable_files: Sequence[str] = (),
    commit_override: str | None = None,
) -> tuple[dict[str, str], str]:
    """Materialize UTF-8 source files directly from a pinned local git commit.

    Reads solely from the local git object database without touching or depending
    on the checkout's working tree state.
    """
    if _is_remote_location(source.repo_path):
        raise FixtureValidationError(
            "Remote repository URLs are forbidden; "
            f"use a local pinned git checkout: {source.repo_path!r}"
        )
    subdir_err = _validate_rel_posix_path(source.subdir, allow_empty=True)
    if subdir_err is not None:
        raise FixtureValidationError(f"Invalid source.subdir {source.subdir!r}: {subdir_err}")
    if source.max_file_bytes <= 0:
        raise FixtureValidationError("source.max_file_bytes must be positive")

    repo_path = _resolve_local_path(source.repo_path, base_dir)
    _verify_local_git_repo(repo_path)

    target_commit = commit_override or source.base_commit
    _resolve_git_commit(repo_path, target_commit)

    if commit_override is None and source.expected_tree_sha is not None:
        if not _is_full_hex_oid(source.expected_tree_sha):
            raise FixtureValidationError(
                "source.expected_tree_sha must be a full hex OID, "
                f"got {source.expected_tree_sha!r}"
            )
        actual_tree = _resolve_git_tree(repo_path, target_commit)
        if actual_tree != source.expected_tree_sha:
            raise FixtureValidationError(
                f"Git tree pin mismatch at {target_commit}: "
                f"expected {source.expected_tree_sha}, got {actual_tree}"
            )

    include_res = [_glob_to_regex(pat) for pat in source.include_globs]
    exclude_res = [_glob_to_regex(pat) for pat in source.exclude_globs]
    all_entries = _list_tree_blobs(repo_path, target_commit, subdir=source.subdir)

    selected: list[tuple[str, str]] = []
    for relpath, oid in all_entries:
        if include_res and not _matches_any_glob(relpath, include_res):
            continue
        if exclude_res and _matches_any_glob(relpath, exclude_res):
            continue
        selected.append((relpath, oid))

    raw_blobs = _batch_read_blobs(repo_path, selected)
    editable_set = set(editable_files)
    files: dict[str, str] = {}

    for relpath in sorted(raw_blobs):
        raw = raw_blobs[relpath]
        is_binary = b"\x00" in raw
        decoded: str | None = None
        if not is_binary:
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                is_binary = True
        if is_binary:
            if relpath in editable_set or not source.skip_binary:
                raise FixtureValidationError(
                    f"Materialized file {relpath!r} is binary or non-UTF-8"
                )
            continue
        if len(raw) > source.max_file_bytes:
            raise FixtureValidationError(
                f"Materialized file {relpath!r} ({len(raw)} bytes) "
                f"exceeds max_file_bytes ({source.max_file_bytes})"
            )
        assert decoded is not None
        files[relpath] = decoded

    missing_editable = [path for path in editable_files if path not in files]
    if missing_editable:
        raise FixtureValidationError(
            f"Editable file(s) missing from materialized commit {target_commit}: "
            f"{', '.join(missing_editable)}"
        )

    digest = compute_files_digest(files)
    if commit_override is None and source.expected_files_sha256 is not None:
        if not _is_sha256_hex(source.expected_files_sha256):
            raise FixtureValidationError(
                "source.expected_files_sha256 must be 64 lowercase hex chars, "
                f"got {source.expected_files_sha256!r}"
            )
        if digest != source.expected_files_sha256:
            raise FixtureValidationError(
                "Materialized files digest mismatch: "
                f"expected {source.expected_files_sha256}, got {digest}"
            )

    return files, digest


def resolve_hidden_files(
    entry: ReplayCaseEntry,
    *,
    base_dir: Path,
    materialized_files: Mapping[str, str],
) -> tuple[ResolvedHiddenFile, ...]:
    """Load and validate external hidden files without adding them to workspace files."""
    repo_path = _resolve_local_path(entry.source.repo_path, base_dir)
    editable_set = set(entry.editable_files)
    seen_targets: set[tuple[str, str]] = set()
    resolved: list[ResolvedHiddenFile] = []

    for idx, hf in enumerate(entry.hidden.files):
        ctx = f"case {entry.case_id!r} hidden.files[{idx}]"
        path_err = _validate_rel_posix_path(hf.target_path)
        if path_err is not None:
            raise FixtureValidationError(f"{ctx}.target_path {hf.target_path!r}: {path_err}")
        if hf.placement not in ALLOWED_PLACEMENTS:
            raise FixtureValidationError(
                f"{ctx}.placement must be one of {ALLOWED_PLACEMENTS}, got {hf.placement!r}"
            )
        placement_key = (hf.placement, hf.target_path)
        if placement_key in seen_targets:
            raise FixtureValidationError(
                f"Duplicate hidden target path {hf.target_path!r} in placement {hf.placement!r}"
            )
        seen_targets.add(placement_key)

        if hf.placement == "workspace":
            if hf.target_path in materialized_files or hf.target_path in editable_set:
                raise FixtureValidationError(
                    f"{ctx}.target_path {hf.target_path!r} "
                    "collides with materialized workspace files"
                )

        has_fs = hf.source_path is not None
        has_git = hf.git_commit is not None or hf.git_path is not None
        if has_fs == has_git:
            raise FixtureValidationError(
                f"{ctx} must specify either source_path or "
                "(git_commit and git_path), not both or neither"
            )

        resolved_source_path: Path | None = None
        resolved_repo_path: Path | None = None
        if has_fs:
            assert hf.source_path is not None
            if _is_remote_location(hf.source_path):
                raise FixtureValidationError(
                    f"{ctx}.source_path must be a local file path, not a URL: {hf.source_path!r}"
                )
            resolved_source_path = _resolve_local_path(hf.source_path, base_dir)
            if not resolved_source_path.is_file():
                raise FixtureValidationError(
                    f"{ctx}.source_path does not exist or is not a regular file: "
                    f"{resolved_source_path}"
                )
            if _is_within(resolved_source_path, repo_path):
                raise FixtureValidationError(
                    f"{ctx}.source_path ({resolved_source_path}) "
                    f"must be external to repo_path ({repo_path})"
                )
            content_bytes = resolved_source_path.read_bytes()
        else:
            if not hf.git_commit or not hf.git_path:
                raise FixtureValidationError(
                    f"{ctx} requires both git_commit and git_path when using git source"
                )
            git_path_err = _validate_rel_posix_path(hf.git_path)
            if git_path_err is not None:
                raise FixtureValidationError(f"{ctx}.git_path {hf.git_path!r}: {git_path_err}")
            if hf.git_commit == entry.source.base_commit:
                rel_in_subdir = hf.git_path
                if entry.source.subdir and rel_in_subdir.startswith(entry.source.subdir + "/"):
                    rel_in_subdir = rel_in_subdir[len(entry.source.subdir) + 1 :]
                if rel_in_subdir in materialized_files:
                    raise FixtureValidationError(
                        f"{ctx} references {hf.git_path!r} at base_commit "
                        "which is already exposed in materialized files"
                    )
            resolved_repo_path = repo_path
            content_bytes = _read_single_git_blob(repo_path, hf.git_commit, hf.git_path)

        actual_sha = hashlib.sha256(content_bytes).hexdigest()
        if hf.sha256 is not None:
            if not _is_sha256_hex(hf.sha256):
                raise FixtureValidationError(
                    f"{ctx}.sha256 must be 64 lowercase hex chars, got {hf.sha256!r}"
                )
            if actual_sha != hf.sha256:
                raise FixtureValidationError(
                    f"{ctx} SHA-256 mismatch for {hf.target_path!r}: "
                    f"expected {hf.sha256}, got {actual_sha}"
                )

        resolved.append(
            ResolvedHiddenFile(
                spec=hf,
                content_bytes=content_bytes,
                sha256=actual_sha,
                resolved_source_path=resolved_source_path,
                resolved_repo_path=resolved_repo_path,
            )
        )

    return tuple(resolved)


def build_hidden_runner_script(
    hidden: HiddenSpec,
    resolved_files: Sequence[ResolvedHiddenFile] = (),
) -> str:
    """Build a standalone Python check script compatible with ``run.py_case``.

    Stages external hidden files with SHA-256 verification, runs the external
    hidden command (without shell interpolation), cleans up any workspace overlays
    in ``finally``, and raises ``AssertionError`` on non-matching exit code.
    """
    serialized_files: list[dict[str, Any]] = []
    for rf in resolved_files:
        item: dict[str, Any] = {
            "target_path": rf.spec.target_path,
            "placement": rf.spec.placement,
            "sha256": rf.sha256,
        }
        compressed = zlib.compress(rf.content_bytes, level=9)
        if len(compressed) <= INLINE_HIDDEN_BYTES_LIMIT:
            item["inline_z64"] = base64.b64encode(compressed).decode("ascii")
        elif rf.resolved_source_path is not None:
            item["source_path"] = str(rf.resolved_source_path)
        elif rf.resolved_repo_path is not None and rf.spec.git_commit and rf.spec.git_path:
            item["git_repo"] = str(rf.resolved_repo_path)
            item["git_commit"] = rf.spec.git_commit
            item["git_path"] = rf.spec.git_path
        else:
            item["inline_z64"] = base64.b64encode(compressed).decode("ascii")
        serialized_files.append(item)

    config = {
        "command": list(hidden.command),
        "cwd": hidden.cwd,
        "env": dict(hidden.env),
        "timeout_seconds": hidden.timeout_seconds,
        "expected_exit_code": hidden.expected_exit_code,
        "files": serialized_files,
    }
    config_json = json.dumps(config, sort_keys=True)
    return (
        "def _replay_run_hidden():\n"
        "    import base64, hashlib, json, os, pathlib\n"
        "    import shutil, subprocess, sys, tempfile, zlib\n"
        f"    cfg = json.loads({config_json!r})\n"
        "    workspace = pathlib.Path.cwd().resolve()\n"
        "    created_files = []\n"
        "    created_dirs = []\n"
        "    with tempfile.TemporaryDirectory(prefix='replay_hidden_') as hidden_dir_str:\n"
        "        hidden_dir = pathlib.Path(hidden_dir_str).resolve()\n"
        "        try:\n"
        "            for item in cfg['files']:\n"
        "                if 'inline_z64' in item:\n"
        "                    data = zlib.decompress(base64.b64decode(item['inline_z64']))\n"
        "                elif 'source_path' in item:\n"
        "                    data = pathlib.Path(item['source_path']).read_bytes()\n"
        "                else:\n"
        "                    git_env = os.environ.copy()\n"
        "                    git_env['GIT_TERMINAL_PROMPT'] = '0'\n"
        "                    git_env['GIT_ASKPASS'] = 'true'\n"
        "                    git_spec = item['git_commit'] + ':' + item['git_path']\n"
        "                    gproc = subprocess.run(\n"
        "                        ['git', '-C', item['git_repo'], 'show', git_spec],\n"
        "                        capture_output=True,\n"
        "                        check=False,\n"
        "                        env=git_env,\n"
        "                    )\n"
        "                    if gproc.returncode != 0:\n"
        "                        err_msg = gproc.stderr.decode('utf-8', 'replace')\n"
        "                        raise AssertionError(\n"
        "                            'Failed to read git hidden file: ' + err_msg\n"
        "                        )\n"
        "                    data = gproc.stdout\n"
        "                actual_sha = hashlib.sha256(data).hexdigest()\n"
        "                if actual_sha != item['sha256']:\n"
        "                    raise AssertionError(\n"
        "                        'Hidden file digest mismatch for ' + item['target_path']\n"
        "                        + ': expected ' + item['sha256'] + ', got ' + actual_sha\n"
        "                    )\n"
        "                base = workspace if item['placement'] == 'workspace' else hidden_dir\n"
        "                dest = (base / item['target_path']).resolve()\n"
        "                dest.relative_to(base)\n"
        "                if item['placement'] == 'workspace':\n"
        "                    if dest.exists():\n"
        "                        raise AssertionError(\n"
        "                            'Hidden workspace file collides with existing path: '\n"
        "                            + item['target_path']\n"
        "                        )\n"
        "                    missing_parents = []\n"
        "                    parent = dest.parent\n"
        "                    while parent != workspace and not parent.exists():\n"
        "                        missing_parents.append(parent)\n"
        "                        parent = parent.parent\n"
        "                    for p in reversed(missing_parents):\n"
        "                        p.mkdir()\n"
        "                        created_dirs.append(p)\n"
        "                    dest.write_bytes(data)\n"
        "                    created_files.append(dest)\n"
        "                else:\n"
        "                    dest.parent.mkdir(parents=True, exist_ok=True)\n"
        "                    dest.write_bytes(data)\n"
        "            subs = {\n"
        "                '{python}': sys.executable,\n"
        "                '{workspace}': str(workspace),\n"
        "                '{hidden_dir}': str(hidden_dir),\n"
        "            }\n"
        "            def _sub(text):\n"
        "                for k, v in subs.items():\n"
        "                    text = text.replace(k, v)\n"
        "                return text\n"
        "            argv = [_sub(tok) for tok in cfg['command']]\n"
        "            env = os.environ.copy()\n"
        "            env['PYTHONDONTWRITEBYTECODE'] = '1'\n"
        "            env['REPLAY_WORKSPACE'] = str(workspace)\n"
        "            env['REPLAY_HIDDEN_DIR'] = str(hidden_dir)\n"
        "            existing_pp = env.get('PYTHONPATH', '')\n"
        "            env['PYTHONPATH'] = (\n"
        "                str(workspace)\n"
        "                if not existing_pp\n"
        "                else str(workspace) + os.pathsep + existing_pp\n"
        "            )\n"
        "            for k, v in cfg['env'].items():\n"
        "                env[k] = _sub(v)\n"
        "            run_cwd = (\n"
        "                workspace\n"
        "                if not cfg['cwd']\n"
        "                else (workspace / cfg['cwd']).resolve()\n"
        "            )\n"
        "            run_cwd.relative_to(workspace)\n"
        "            proc = subprocess.run(\n"
        "                argv,\n"
        "                cwd=str(run_cwd),\n"
        "                env=env,\n"
        "                capture_output=True,\n"
        "                text=True,\n"
        "                timeout=cfg['timeout_seconds'],\n"
        "            )\n"
        "            if proc.returncode != cfg['expected_exit_code']:\n"
        "                raise AssertionError(\n"
        "                    'Hidden command ' + repr(argv)\n"
        "                    + ' exited with ' + str(proc.returncode)\n"
        "                    + ' (expected ' + str(cfg['expected_exit_code']) + ')\\n'\n"
        "                    + 'STDOUT:\\n' + proc.stdout + '\\nSTDERR:\\n' + proc.stderr\n"
        "                )\n"
        "        finally:\n"
        "            for path in reversed(created_files):\n"
        "                try:\n"
        "                    path.unlink()\n"
        "                except FileNotFoundError:\n"
        "                    pass\n"
        "            for directory in reversed(created_dirs):\n"
        "                shutil.rmtree(directory, ignore_errors=True)\n"
        "_replay_run_hidden()\n"
        "del _replay_run_hidden\n"
    )


def build_smoke_wrapper_script(smoke: SmokeSpec) -> str:
    """Build a ``smoke.py`` wrapper script delegating to ``smoke.command``."""
    config = {
        "command": list(smoke.command),
        "timeout_seconds": smoke.timeout_seconds,
    }
    config_json = json.dumps(config, sort_keys=True)
    return (
        "import json, os, pathlib, subprocess, sys\n"
        f"_cfg = json.loads({config_json!r})\n"
        "_workspace = pathlib.Path.cwd().resolve()\n"
        "_argv = [\n"
        "    tok.replace('{python}', sys.executable).replace('{workspace}', str(_workspace))\n"
        "    for tok in _cfg['command']\n"
        "]\n"
        "_env = os.environ.copy()\n"
        "_pp = _env.get('PYTHONPATH', '')\n"
        "_env['PYTHONPATH'] = str(_workspace) if not _pp else str(_workspace) + os.pathsep + _pp\n"
        "_proc = subprocess.run(\n"
        "    _argv,\n"
        "    cwd=str(_workspace),\n"
        "    env=_env,\n"
        "    capture_output=True,\n"
        "    text=True,\n"
        "    timeout=_cfg['timeout_seconds'],\n"
        ")\n"
        "if _proc.returncode != 0:\n"
        "    raise AssertionError(\n"
        "        'Smoke command ' + repr(_argv) + ' exited with ' + str(_proc.returncode)\n"
        "        + '\\nSTDOUT:\\n' + _proc.stdout + '\\nSTDERR:\\n' + _proc.stderr\n"
        "    )\n"
    )


def _write_workspace_files(workspace: Path, files: Mapping[str, str]) -> None:
    root = workspace.resolve()
    for relpath, content in files.items():
        dest = (root / relpath).resolve()
        dest.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _run_python_script_in_workspace(
    script: str, workspace: Path, *, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    ws_str = str(workspace.resolve())
    env["PYTHONPATH"] = ws_str if not existing_pp else ws_str + os.pathsep + existing_pp
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ws_str,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _run_smoke_in_workspace(
    smoke: SmokeSpec, workspace: Path
) -> subprocess.CompletedProcess[str]:
    if smoke.script is not None:
        return _run_python_script_in_workspace(
            smoke.script, workspace, timeout_seconds=smoke.timeout_seconds
        )
    wrapper = build_smoke_wrapper_script(smoke)
    return _run_python_script_in_workspace(
        wrapper, workspace, timeout_seconds=smoke.timeout_seconds + 5.0
    )


def load_manifest(manifest_path: str | Path) -> tuple[ReplayManifest, Path]:
    """Load and parse a JSON replay corpus manifest from disk."""
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Replay manifest not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FixtureValidationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise FixtureValidationError(f"Replay manifest root must be a JSON object: {path}")
    return ReplayManifest.from_dict(raw), path.parent


def materialize_case(
    entry: ReplayCaseEntry,
    *,
    base_dir: Path,
) -> MaterializedCase:
    """Materialize a single replay case from its pinned local git checkout."""
    files, files_sha256 = materialize_source(
        entry.source, base_dir=base_dir, editable_files=entry.editable_files
    )
    hidden_files = resolve_hidden_files(
        entry, base_dir=base_dir, materialized_files=files
    )
    repo_path = _resolve_local_path(entry.source.repo_path, base_dir)
    return MaterializedCase(
        entry=entry,
        files=files,
        files_sha256=files_sha256,
        hidden_files=hidden_files,
        resolved_repo_path=repo_path,
    )


def validate_manifest(
    manifest: ReplayManifest,
    *,
    base_dir: Path,
    run_checks: bool = False,
    require_frozen: bool = False,
) -> ValidationReport:
    """Validate preregistration metadata, git pins, external hidden fixtures, and checks."""
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    case_results: dict[str, CaseValidationResult] = {}

    def add_error(
        code: str,
        message: str,
        *,
        case_id: str | None = None,
        field_name: str | None = None,
    ) -> None:
        errors.append(
            ValidationIssue("error", code, message, case_id=case_id, field=field_name)
        )

    def add_warning(
        code: str,
        message: str,
        *,
        case_id: str | None = None,
        field_name: str | None = None,
    ) -> None:
        warnings.append(
            ValidationIssue("warning", code, message, case_id=case_id, field=field_name)
        )

    computed_manifest_sha = compute_manifest_digest(manifest)

    if manifest.schema_version != SCHEMA_VERSION:
        add_error(
            "invalid_schema_version",
            f"Unsupported schema_version {manifest.schema_version!r}; expected {SCHEMA_VERSION!r}",
            field_name="schema_version",
        )

    prereg = manifest.preregistration
    if not prereg.protocol_version.strip():
        add_error(
            "missing_preregistration_field",
            "preregistration.protocol_version must be non-empty",
            field_name="preregistration.protocol_version",
        )
    if not prereg.preregistration_id.strip() or not _SLUG_RE.match(prereg.preregistration_id):
        add_error(
            "invalid_preregistration_id",
            "preregistration.preregistration_id must be a non-empty slug, "
            f"got {prereg.preregistration_id!r}",
            field_name="preregistration.preregistration_id",
        )
    if not _validate_iso_timestamp(prereg.registered_at):
        add_error(
            "invalid_registered_at",
            "preregistration.registered_at must be an ISO-8601 timestamp with timezone, "
            f"got {prereg.registered_at!r}",
            field_name="preregistration.registered_at",
        )
    if not prereg.frozen:
        if require_frozen:
            add_error(
                "manifest_not_frozen",
                "preregistration.frozen must be true when require_frozen=True",
                field_name="preregistration.frozen",
            )
        else:
            add_warning(
                "manifest_not_frozen",
                "preregistration.frozen is false; freeze and pin manifest_sha256"
                " before running formal trials",
                field_name="preregistration.frozen",
            )
    for attr in ("hypothesis", "primary_metric", "stopping_rule"):
        if not getattr(prereg, attr).strip():
            add_error(
                "missing_preregistration_field",
                f"preregistration.{attr} must be non-empty",
                field_name=f"preregistration.{attr}",
            )
    for attr in ("inclusion_criteria", "exclusion_criteria", "leakage_controls"):
        items = getattr(prereg, attr)
        if not items or any(not item.strip() for item in items):
            add_error(
                "missing_preregistration_field",
                f"preregistration.{attr} must contain at least one non-empty entry",
                field_name=f"preregistration.{attr}",
            )

    if prereg.cutoff_direction not in ALLOWED_CUTOFF_DIRECTIONS:
        add_error(
            "invalid_cutoff_direction",
            f"preregistration.cutoff_direction must be one of {ALLOWED_CUTOFF_DIRECTIONS}, "
            f"got {prereg.cutoff_direction!r}",
            field_name="preregistration.cutoff_direction",
        )
    cutoff_date_parsed: date | None = None
    if prereg.cutoff_date is not None:
        cutoff_date_parsed = _validate_iso_date(prereg.cutoff_date)
        if cutoff_date_parsed is None:
            add_error(
                "invalid_cutoff_date",
                f"preregistration.cutoff_date must be YYYY-MM-DD, got {prereg.cutoff_date!r}",
                field_name="preregistration.cutoff_date",
            )

    if prereg.manifest_sha256 is not None:
        if not _is_sha256_hex(prereg.manifest_sha256):
            add_error(
                "invalid_manifest_sha256",
                "preregistration.manifest_sha256 must be 64 lowercase hex chars, "
                f"got {prereg.manifest_sha256!r}",
                field_name="preregistration.manifest_sha256",
            )
        elif prereg.manifest_sha256 != computed_manifest_sha:
            add_error(
                "manifest_digest_mismatch",
                "preregistration.manifest_sha256 mismatch: "
                f"expected {prereg.manifest_sha256}, computed {computed_manifest_sha}",
                field_name="preregistration.manifest_sha256",
            )
    elif prereg.frozen:
        add_warning(
            "unpinned_frozen_manifest",
            "preregistration.frozen is true but manifest_sha256 is not set",
            field_name="preregistration.manifest_sha256",
        )

    if not manifest.cases:
        add_error(
            "empty_corpus",
            "manifest.cases must contain at least one case",
            field_name="cases",
        )

    seen_ids: set[str] = set()
    for entry in manifest.cases:
        cid = entry.case_id
        if not cid.strip() or not _SLUG_RE.match(cid):
            add_error(
                "invalid_case_id",
                f"case_id must be a non-empty slug, got {cid!r}",
                case_id=cid,
                field_name="case_id",
            )
        if cid in seen_ids:
            add_error(
                "duplicate_case_id",
                f"Duplicate case_id {cid!r}",
                case_id=cid,
                field_name="case_id",
            )
        seen_ids.add(cid)

        if not entry.task_prompt.strip():
            add_error(
                "empty_task_prompt",
                "task_prompt must be non-empty",
                case_id=cid,
                field_name="task_prompt",
            )
        if not entry.explicit_guidance.strip():
            add_error(
                "empty_explicit_guidance",
                "explicit_guidance must be non-empty",
                case_id=cid,
                field_name="explicit_guidance",
            )
        if not entry.editable_files:
            add_error(
                "empty_editable_files",
                "editable_files must contain at least one relative path",
                case_id=cid,
                field_name="editable_files",
            )
        else:
            seen_editable: set[str] = set()
            for ef in entry.editable_files:
                err = _validate_rel_posix_path(ef)
                if err is not None:
                    add_error(
                        "invalid_editable_file",
                        f"editable_files entry {ef!r}: {err}",
                        case_id=cid,
                        field_name="editable_files",
                    )
                if ef in seen_editable:
                    add_error(
                        "duplicate_editable_file",
                        f"Duplicate editable file {ef!r}",
                        case_id=cid,
                        field_name="editable_files",
                    )
                seen_editable.add(ef)

        # Case-level preregistration checks
        cpr = entry.preregistration
        if not cpr.selection_rationale.strip():
            add_error(
                "missing_case_preregistration",
                "case preregistration.selection_rationale must be non-empty",
                case_id=cid,
                field_name="preregistration.selection_rationale",
            )
        if not cpr.source_origin.strip():
            add_error(
                "missing_case_preregistration",
                "case preregistration.source_origin must be non-empty",
                case_id=cid,
                field_name="preregistration.source_origin",
            )
        if cpr.expected_difficulty not in ALLOWED_DIFFICULTIES:
            add_error(
                "invalid_expected_difficulty",
                f"expected_difficulty must be one of {ALLOWED_DIFFICULTIES}, "
                f"got {cpr.expected_difficulty!r}",
                case_id=cid,
                field_name="preregistration.expected_difficulty",
            )
        if cpr.file_scope_category not in ALLOWED_FILE_SCOPES:
            add_error(
                "invalid_file_scope_category",
                f"file_scope_category must be one of {ALLOWED_FILE_SCOPES}, "
                f"got {cpr.file_scope_category!r}",
                case_id=cid,
                field_name="preregistration.file_scope_category",
            )
        elif entry.editable_files:
            expected_scope = "single_file" if len(entry.editable_files) == 1 else "multi_file"
            if cpr.file_scope_category != expected_scope:
                add_error(
                    "file_scope_mismatch",
                    f"file_scope_category is {cpr.file_scope_category!r} "
                    f"but editable_files has {len(entry.editable_files)} file(s)",
                    case_id=cid,
                    field_name="preregistration.file_scope_category",
                )
        if not cpr.leakage_checked:
            add_error(
                "leakage_not_checked",
                "case preregistration.leakage_checked must be true",
                case_id=cid,
                field_name="preregistration.leakage_checked",
            )
        if cpr.commit_date is not None:
            parsed_commit_date = _validate_iso_date(cpr.commit_date)
            if parsed_commit_date is None:
                add_error(
                    "invalid_commit_date",
                    "case preregistration.commit_date must be YYYY-MM-DD, "
                    f"got {cpr.commit_date!r}",
                    case_id=cid,
                    field_name="preregistration.commit_date",
                )
            elif cutoff_date_parsed is not None:
                if (
                    prereg.cutoff_direction == "on_or_after"
                    and parsed_commit_date < cutoff_date_parsed
                ):
                    add_error(
                        "cutoff_date_violation",
                        f"commit_date {cpr.commit_date} precedes "
                        f"preregistered cutoff_date {prereg.cutoff_date}",
                        case_id=cid,
                        field_name="preregistration.commit_date",
                    )
                elif (
                    prereg.cutoff_direction == "on_or_before"
                    and parsed_commit_date > cutoff_date_parsed
                ):
                    add_error(
                        "cutoff_date_violation",
                        f"commit_date {cpr.commit_date} exceeds "
                        f"preregistered cutoff_date {prereg.cutoff_date}",
                        case_id=cid,
                        field_name="preregistration.commit_date",
                    )

        # Hidden command structural checks
        if not entry.hidden.command or any(not tok for tok in entry.hidden.command):
            add_error(
                "invalid_hidden_command",
                "hidden.command must be a non-empty list of non-empty argument strings",
                case_id=cid,
                field_name="hidden.command",
            )
        if not math.isfinite(entry.hidden.timeout_seconds) or entry.hidden.timeout_seconds <= 0:
            add_error(
                "invalid_hidden_timeout",
                "hidden.timeout_seconds must be a positive finite number",
                case_id=cid,
                field_name="hidden.timeout_seconds",
            )
        if entry.hidden.cwd is not None:
            cwd_err = _validate_rel_posix_path(entry.hidden.cwd)
            if cwd_err is not None:
                add_error(
                    "invalid_hidden_cwd",
                    f"hidden.cwd {entry.hidden.cwd!r}: {cwd_err}",
                    case_id=cid,
                    field_name="hidden.cwd",
                )

        # Smoke structural checks
        if entry.smoke is not None:
            has_smoke_cmd = bool(entry.smoke.command)
            has_smoke_script = entry.smoke.script is not None
            if not has_smoke_cmd and not has_smoke_script:
                add_error(
                    "empty_smoke_spec",
                    "smoke must specify command or script",
                    case_id=cid,
                    field_name="smoke",
                )
            if has_smoke_cmd and any(not tok for tok in entry.smoke.command):
                add_error(
                    "invalid_smoke_command",
                    "smoke.command tokens must be non-empty strings",
                    case_id=cid,
                    field_name="smoke.command",
                )
            if not math.isfinite(entry.smoke.timeout_seconds) or entry.smoke.timeout_seconds <= 0:
                add_error(
                    "invalid_smoke_timeout",
                    "smoke.timeout_seconds must be a positive finite number",
                    case_id=cid,
                    field_name="smoke.timeout_seconds",
                )

        # Materialize source & resolve hidden files
        try:
            materialized = materialize_case(entry, base_dir=base_dir)
        except FixtureValidationError as exc:
            add_error(
                "materialization_failed",
                str(exc),
                case_id=cid,
                field_name="source",
            )
            continue

        if entry.smoke is not None and entry.smoke.script is not None:
            if "smoke.py" in materialized.files:
                add_error(
                    "smoke_script_collision",
                    "smoke.script cannot be set when materialized source"
                    " already contains 'smoke.py'",
                    case_id=cid,
                    field_name="smoke.script",
                )

        fix_files: dict[str, str] | None = None
        if entry.source.fix_commit is not None:
            if entry.source.fix_commit == entry.source.base_commit:
                add_error(
                    "identical_fix_commit",
                    "source.fix_commit must differ from source.base_commit",
                    case_id=cid,
                    field_name="source.fix_commit",
                )
            else:
                try:
                    fix_files, _ = materialize_source(
                        entry.source,
                        base_dir=base_dir,
                        editable_files=entry.editable_files,
                        commit_override=entry.source.fix_commit,
                    )
                except FixtureValidationError as exc:
                    add_error(
                        "fix_commit_materialization_failed",
                        str(exc),
                        case_id=cid,
                        field_name="source.fix_commit",
                    )
                else:
                    changed_editable = [
                        ef
                        for ef in entry.editable_files
                        if materialized.files[ef] != fix_files[ef]
                    ]
                    if not changed_editable:
                        add_error(
                            "fix_commit_no_editable_diff",
                            "source.fix_commit does not change any declared"
                            " editable_files relative to base_commit",
                            case_id=cid,
                            field_name="source.fix_commit",
                        )

        base_smoke_passed: bool | None = None
        base_hidden_failed: bool | None = None
        fix_hidden_passed: bool | None = None
        fix_smoke_passed: bool | None = None

        if run_checks and not any(err.case_id == cid for err in errors):
            hidden_script = build_hidden_runner_script(
                entry.hidden, materialized.hidden_files
            )
            with tempfile.TemporaryDirectory(prefix=f"replay_val_{cid}_") as tmp_dir:
                ws = Path(tmp_dir)
                _write_workspace_files(ws, materialized.files)

                if entry.smoke is not None:
                    try:
                        smoke_proc = _run_smoke_in_workspace(entry.smoke, ws)
                    except subprocess.TimeoutExpired:
                        base_smoke_passed = False
                        add_error(
                            "base_smoke_timeout",
                            "Smoke check timed out on base_commit",
                            case_id=cid,
                            field_name="smoke",
                        )
                    else:
                        base_smoke_passed = smoke_proc.returncode == 0
                        if not base_smoke_passed:
                            detail = smoke_proc.stderr or smoke_proc.stdout
                            add_error(
                                "base_smoke_failed",
                                "Smoke check failed on base_commit "
                                f"(rc={smoke_proc.returncode}): {detail}",
                                case_id=cid,
                                field_name="smoke",
                            )
                elif "smoke.py" in materialized.files:
                    smoke_proc = _run_python_script_in_workspace(
                        materialized.files["smoke.py"], ws, timeout_seconds=30.0
                    )
                    base_smoke_passed = smoke_proc.returncode == 0
                    if not base_smoke_passed:
                        add_error(
                            "base_smoke_failed",
                            "Repository smoke.py failed on base_commit "
                            f"(rc={smoke_proc.returncode})",
                            case_id=cid,
                            field_name="smoke",
                        )

                try:
                    hidden_base_proc = _run_python_script_in_workspace(
                        hidden_script,
                        ws,
                        timeout_seconds=entry.hidden.timeout_seconds + 10.0,
                    )
                except subprocess.TimeoutExpired:
                    base_hidden_failed = False
                    add_error(
                        "base_hidden_timeout",
                        "Hidden verification timed out on base_commit",
                        case_id=cid,
                        field_name="hidden",
                    )
                else:
                    base_hidden_failed = hidden_base_proc.returncode != 0
                    if not base_hidden_failed:
                        add_error(
                            "base_hidden_unexpectedly_passed",
                            "Hidden verification passed on buggy base_commit; "
                            "fixture must fail on base_commit",
                            case_id=cid,
                            field_name="hidden",
                        )

                if fix_files is not None:
                    for ef in entry.editable_files:
                        dest = (ws / ef).resolve()
                        dest.relative_to(ws.resolve())
                        dest.write_text(fix_files[ef], encoding="utf-8")

                    try:
                        hidden_fix_proc = _run_python_script_in_workspace(
                            hidden_script,
                            ws,
                            timeout_seconds=entry.hidden.timeout_seconds + 10.0,
                        )
                    except subprocess.TimeoutExpired:
                        fix_hidden_passed = False
                        add_error(
                            "fix_hidden_timeout",
                            "Hidden verification timed out after applying"
                            " fix_commit editable_files",
                            case_id=cid,
                            field_name="source.fix_commit",
                        )
                    else:
                        fix_hidden_passed = hidden_fix_proc.returncode == 0
                        if not fix_hidden_passed:
                            detail = hidden_fix_proc.stderr or hidden_fix_proc.stdout
                            add_error(
                                "fix_hidden_failed",
                                "Hidden verification failed after applying fix_commit "
                                f"editable_files (rc={hidden_fix_proc.returncode}): {detail}",
                                case_id=cid,
                                field_name="source.fix_commit",
                            )

                    if entry.smoke is not None:
                        smoke_fix_proc = _run_smoke_in_workspace(entry.smoke, ws)
                        fix_smoke_passed = smoke_fix_proc.returncode == 0
                        if not fix_smoke_passed:
                            add_error(
                                "fix_smoke_failed",
                                "Smoke check failed after applying fix_commit "
                                f"editable_files (rc={smoke_fix_proc.returncode})",
                                case_id=cid,
                                field_name="smoke",
                            )

        case_results[cid] = CaseValidationResult(
            case_id=cid,
            file_count=len(materialized.files),
            files_sha256=materialized.files_sha256,
            hidden_file_digests={
                f"{rf.spec.placement}:{rf.spec.target_path}": rf.sha256
                for rf in materialized.hidden_files
            },
            base_smoke_passed=base_smoke_passed,
            base_hidden_failed=base_hidden_failed,
            fix_hidden_passed=fix_hidden_passed,
            fix_smoke_passed=fix_smoke_passed,
        )

    return ValidationReport(
        manifest_sha256=computed_manifest_sha,
        errors=tuple(errors),
        warnings=tuple(warnings),
        case_results=case_results,
    )


def _load_run_module(run_module: Any = None) -> Any:
    if run_module is not None:
        return run_module
    try:
        from . import run as local_run

        return local_run
    except ImportError:
        import run as local_run  # type: ignore[import-not-found]

        return local_run


def _coerce_cmd_for_field(
    command: Sequence[str], field_obj: dataclasses.Field[Any], current_value: Any
) -> Any:
    if isinstance(current_value, tuple):
        return tuple(command)
    if isinstance(current_value, list):
        return list(command)
    if isinstance(field_obj.default, tuple):
        return tuple(command)
    if isinstance(field_obj.default, list):
        return list(command)
    return tuple(command)


def to_case_spec(
    materialized: MaterializedCase,
    *,
    run_module: Any = None,
) -> Any:
    """Convert a ``MaterializedCase`` into a ``run.CaseSpec`` instance."""
    run_mod = _load_run_module(run_module)
    entry = materialized.entry
    first_editable, *rest_editable = entry.editable_files

    hidden_script = build_hidden_runner_script(entry.hidden, materialized.hidden_files)
    case_files = dict(materialized.files)

    case_spec_cls = getattr(run_mod, "CaseSpec", None)
    spec_fields = (
        {f.name: f for f in dataclasses.fields(case_spec_cls)}
        if case_spec_cls is not None and dataclasses.is_dataclass(case_spec_cls)
        else {}
    )
    smoke_cmd_field = next(
        (
            name
            for name in ("smoke_cmd", "visible_cmd", "public_cmd")
            if name in spec_fields
        ),
        None,
    )

    if entry.smoke is not None:
        if entry.smoke.script is not None:
            if "smoke.py" in case_files:
                raise FixtureValidationError(
                    f"Case {entry.case_id!r}: smoke.script collides with existing smoke.py"
                )
            case_files["smoke.py"] = entry.smoke.script
        elif entry.smoke.command and smoke_cmd_field is None and "smoke.py" not in case_files:
            case_files["smoke.py"] = build_smoke_wrapper_script(entry.smoke)

    if hasattr(run_mod, "py_case"):
        case = run_mod.py_case(
            entry.case_id,
            case_files,
            entry.task_prompt,
            hidden_script,
            entry.explicit_guidance,
            editable_file=first_editable,
        )
    elif case_spec_cls is not None:
        kwargs: dict[str, Any] = {}
        for fname in spec_fields:
            if fname == "name":
                kwargs[fname] = entry.case_id
            elif fname == "files":
                kwargs[fname] = case_files
            elif fname in ("task", "prompt"):
                kwargs[fname] = entry.task_prompt
            elif fname in ("hidden", "hidden_check", "check"):
                kwargs[fname] = hidden_script
            elif fname in ("explicit", "explicit_guidance", "explicit_hint", "hint"):
                kwargs[fname] = entry.explicit_guidance
            elif fname == "editable_file":
                kwargs[fname] = first_editable
            elif fname == "additional_editable":
                kwargs[fname] = tuple(rest_editable)
        case = case_spec_cls(**kwargs)
    else:
        raise RuntimeError("Provided run module exposes neither py_case nor CaseSpec")

    if dataclasses.is_dataclass(case):
        instance_fields = {f.name: f for f in dataclasses.fields(case)}
        updates: dict[str, Any] = {}
        if "additional_editable" in instance_fields:
            updates["additional_editable"] = tuple(rest_editable)
        if (
            entry.smoke is not None
            and entry.smoke.command
            and smoke_cmd_field is not None
            and smoke_cmd_field in instance_fields
        ):
            resolved_smoke_cmd = [
                tok.replace("{python}", sys.executable) for tok in entry.smoke.command
            ]
            updates[smoke_cmd_field] = _coerce_cmd_for_field(
                resolved_smoke_cmd,
                instance_fields[smoke_cmd_field],
                getattr(case, smoke_cmd_field),
            )
        if updates:
            case = replace(case, **updates)

    if case_spec_cls is not None and not isinstance(case, case_spec_cls):
        raise TypeError(
            f"Constructed case {type(case)!r} is not an instance of {case_spec_cls!r}"
        )
    return case


def cases(
    manifest_path: str | Path | None = None,
    *,
    validate: bool = True,
    run_checks: bool = False,
    require_frozen: bool = False,
    run_module: Any = None,
) -> list[Any]:
    """Load, validate, materialize, and return ``run.CaseSpec`` objects for a replay manifest."""
    raw_path = manifest_path or os.environ.get(MANIFEST_ENV_VAR)
    if not raw_path:
        raise ValueError(
            f"manifest_path is required (or set {MANIFEST_ENV_VAR}); "
            "no built-in replay corpus is bundled."
        )
    manifest, base_dir = load_manifest(raw_path)
    if validate:
        report = validate_manifest(
            manifest,
            base_dir=base_dir,
            run_checks=run_checks,
            require_frozen=require_frozen,
        )
        if not report.ok:
            summary = "; ".join(
                f"[{issue.code}] {issue.message}" for issue in report.errors
            )
            raise FixtureValidationError(
                f"Replay manifest validation failed ({len(report.errors)} error(s)): {summary}",
                issues=report.errors,
            )
    return [
        to_case_spec(materialize_case(entry, base_dir=base_dir), run_module=run_module)
        for entry in manifest.cases
    ]


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, digest, and inspect version-pinned local git replay corpora."
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_val = sub.add_parser("validate", help="Validate a replay corpus manifest.")
    p_val.add_argument("manifest", help="Path to replay manifest JSON file.")
    p_val.add_argument(
        "--run-checks",
        action="store_true",
        help="Execute smoke and fail-on-base / pass-on-fix hidden verification checks.",
    )
    p_val.add_argument(
        "--require-frozen",
        action="store_true",
        help="Fail validation if preregistration.frozen is false.",
    )
    p_val.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON validation report.",
    )

    p_dig = sub.add_parser(
        "digest",
        help=(
            "Compute canonical manifest_sha256, materialized files_sha256,"
            " and hidden file digests."
        ),
    )
    p_dig.add_argument("manifest", help="Path to replay manifest JSON file.")

    p_list = sub.add_parser("list", help="List cases in a replay corpus manifest.")
    p_list.add_argument("manifest", help="Path to replay manifest JSON file.")

    sub.add_parser(
        "schema", help="Print documented preregistration fields and schema version."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "schema":
        payload = {
            "schema_version": SCHEMA_VERSION,
            "preregistration_fields": PREREGISTRATION_FIELD_DOCS,
            "case_preregistration_fields": CASE_PREREGISTRATION_FIELD_DOCS,
            "allowed_difficulties": list(ALLOWED_DIFFICULTIES),
            "allowed_file_scopes": list(ALLOWED_FILE_SCOPES),
            "allowed_placements": list(ALLOWED_PLACEMENTS),
            "allowed_cutoff_directions": list(ALLOWED_CUTOFF_DIRECTIONS),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    manifest, base_dir = load_manifest(args.manifest)

    if args.subcommand == "digest":
        case_digests: dict[str, Any] = {}
        for entry in manifest.cases:
            mat = materialize_case(entry, base_dir=base_dir)
            case_digests[entry.case_id] = {
                "base_commit": entry.source.base_commit,
                "tree_sha": _resolve_git_tree(mat.resolved_repo_path, entry.source.base_commit),
                "files_sha256": mat.files_sha256,
                "hidden_file_digests": {
                    f"{rf.spec.placement}:{rf.spec.target_path}": rf.sha256
                    for rf in mat.hidden_files
                },
            }
        output = {
            "manifest_sha256": compute_manifest_digest(manifest),
            "cases": case_digests,
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0

    if args.subcommand == "list":
        rows = [
            {
                "case_id": entry.case_id,
                "base_commit": entry.source.base_commit,
                "fix_commit": entry.source.fix_commit,
                "editable_files": list(entry.editable_files),
                "difficulty": entry.preregistration.expected_difficulty,
                "file_scope": entry.preregistration.file_scope_category,
            }
            for entry in manifest.cases
        ]
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    if args.subcommand == "validate":
        report = validate_manifest(
            manifest,
            base_dir=base_dir,
            run_checks=args.run_checks,
            require_frozen=args.require_frozen,
        )
        if args.as_json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            status = "OK" if report.ok else "FAILED"
            print(f"Validation {status} (manifest_sha256={report.manifest_sha256})")
            for warning in report.warnings:
                target = warning.case_id or "manifest"
                print(f"  WARNING [{warning.code}] ({target}): {warning.message}")
            for error in report.errors:
                target = error.case_id or "manifest"
                print(f"  ERROR [{error.code}] ({target}): {error.message}")
        return 0 if report.ok else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
