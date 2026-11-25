"""Bounded, streaming source-range reader and validated Cymbal ``show`` adapter.

Architecture:
- Boundary DTOs (``LineRangeExcerpt``, ``RangeReadMetrics``) are frozen, immutable
  value objects with explicit provenance, truncation reporting, and metrics.
- ``read_range`` streams a regular UTF-8 file inside ``root`` line by line without
  loading the full file into memory, allowing targeted reads deep inside large
  (``>128 KiB``) files while enforcing strict path, symlink, binary, UTF-8,
  line-count, byte-output, and scan-byte guards.
- ``validate_cymbal_show`` validates a Cymbal ``show`` JSON payload against the
  same workspace security and bounding contracts and cross-checks disk state for
  staleness.
"""

from __future__ import annotations

import codecs
import json
import math
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping, Sequence

DEFAULT_MAX_LINES = 120
DEFAULT_MAX_BYTES = 16384
DEFAULT_MAX_SCAN_BYTES = 16 * 1024 * 1024
_SKIP_CHUNK_BYTES = 65536
_CHARS_PER_TOKEN_ESTIMATE = 4

__all__ = [
    "DEFAULT_MAX_LINES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_SCAN_BYTES",
    "ReadRangeError",
    "PathGuardError",
    "BinaryFileError",
    "InvalidEncodingError",
    "ScanLimitExceededError",
    "CymbalShowError",
    "LineRangeExcerpt",
    "RangeReadMetrics",
    "read_range",
    "validate_cymbal_show",
    "estimate_tokens",
    "measure_read_range",
    "measure_read_ranges",
]


class ReadRangeError(ValueError):
    """Base error raised when a range read violates a precondition or guard."""


class PathGuardError(ReadRangeError):
    """Raised when a path escapes ``root``, traverses symlinks, or is not a file."""


class BinaryFileError(ReadRangeError):
    """Raised when NUL bytes indicating binary content are encountered."""


class InvalidEncodingError(ReadRangeError):
    """Raised when scanned file bytes are not valid UTF-8."""


class ScanLimitExceededError(ReadRangeError):
    """Raised when scanning exceeds ``max_scan_bytes`` before completing the range."""


class CymbalShowError(ReadRangeError):
    """Raised when a Cymbal ``show`` payload is malformed or fails strict validation."""


@dataclass(frozen=True)
class LineRangeExcerpt:
    """Immutable boundary DTO for a bounded, numbered source slice.

    Invariants:
        - ``requested_start_line >= 1`` and ``requested_end_line >= requested_start_line``.
        - When ``lines`` is non-empty: ``start_line >= 1`` and
          ``end_line == start_line + len(lines) - 1``.
        - When ``lines`` is empty: ``start_line == 0`` and ``end_line == 0``.
        - ``bytes_scanned >= 0`` and ``bytes_returned >= 0``.
    """

    path: str
    start_line: int
    end_line: int
    lines: tuple[str, ...]
    requested_start_line: int
    requested_end_line: int
    source: str = "native"
    symbol: str = ""
    bytes_scanned: int = 0
    bytes_returned: int = 0
    file_size_bytes: int | None = None
    total_lines: int | None = None
    eof_reached: bool = False
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.lines, tuple):
            object.__setattr__(self, "lines", tuple(self.lines))
        if not isinstance(self.truncation_reasons, tuple):
            object.__setattr__(self, "truncation_reasons", tuple(self.truncation_reasons))
        if not isinstance(self.limitations, tuple):
            object.__setattr__(self, "limitations", tuple(self.limitations))

        if not self.path or not isinstance(self.path, str):
            raise ReadRangeError("LineRangeExcerpt.path must be a non-empty string")
        if not self.source or not isinstance(self.source, str):
            raise ReadRangeError("LineRangeExcerpt.source must be a non-empty string")
        if self.requested_start_line < 1 or self.requested_end_line < self.requested_start_line:
            raise ReadRangeError(
                f"Invalid requested range: {self.requested_start_line}..{self.requested_end_line}"
            )
        if self.lines:
            if self.start_line < 1:
                raise ReadRangeError("Non-empty excerpt must have start_line >= 1")
            expected_end = self.start_line + len(self.lines) - 1
            if self.end_line != expected_end:
                raise ReadRangeError(
                    f"end_line ({self.end_line}) does not match start_line + len(lines) - 1 "
                    f"({expected_end})"
                )
        else:
            if self.start_line != 0 or self.end_line != 0:
                raise ReadRangeError("Empty excerpt must have start_line == 0 and end_line == 0")
        if self.bytes_scanned < 0 or self.bytes_returned < 0:
            raise ReadRangeError("Byte counters must be non-negative")
        if self.truncation_reasons and not self.truncated:
            object.__setattr__(self, "truncated", True)

    @property
    def code(self) -> str:
        """Return the unnumbered slice text joined by ``\\n``."""
        return "\n".join(self.lines)

    @property
    def numbered_lines(self) -> tuple[str, ...]:
        """Return 1-based numbered source lines."""
        return tuple(
            f"{self.start_line + idx}: {line}" for idx, line in enumerate(self.lines)
        )

    @property
    def numbered_code(self) -> str:
        """Return the numbered slice text joined by ``\\n``."""
        return "\n".join(self.numbered_lines)

    @property
    def line_count(self) -> int:
        """Return the number of lines in this slice."""
        return len(self.lines)

    @property
    def requested_line_count(self) -> int:
        """Return the number of lines originally requested."""
        return self.requested_end_line - self.requested_start_line + 1

    @property
    def is_empty(self) -> bool:
        """Return True when no lines were returned (for example, past EOF)."""
        return len(self.lines) == 0

    @property
    def incomplete(self) -> bool:
        """Return True if the excerpt was truncated, stale, or has limitations."""
        return bool(self.truncated or self.limitations or self.stale)

    @property
    def coverage(self) -> float:
        """Return fraction of the requested line count covered by this slice."""
        return len(self.lines) / self.requested_line_count

    @property
    def numbered_bytes_returned(self) -> int:
        """Return UTF-8 byte length of the numbered slice text."""
        return len(self.numbered_code.encode("utf-8"))

    def format(self, *, numbered: bool = True) -> str:
        """Render the slice with exact provenance header and optional line numbers."""
        if self.is_empty:
            header = f"{self.path}:{self.requested_start_line}-{self.requested_end_line}"
        else:
            header = f"{self.path}:{self.start_line}-{self.end_line}"
        if self.symbol:
            header += f" ({self.symbol})"
        if self.source != "native":
            header += f" [{self.source}]"
        if self.is_empty:
            eof_note = (
                f"EOF at line {self.total_lines}"
                if self.total_lines is not None
                else "no lines"
            )
            header += f" [EMPTY: {eof_note}]"
        if self.truncated:
            reasons = ", ".join(self.truncation_reasons) or "truncated"
            header += f" [TRUNCATED: {reasons}]"
        if self.limitations:
            header += f" [INCOMPLETE: {', '.join(self.limitations)}]"

        body = self.numbered_code if numbered else self.code
        return f"{header}\n{body}" if body else header


@dataclass(frozen=True)
class RangeReadMetrics:
    """Immutable telemetry summary for one or more bounded range reads."""

    bytes_scanned: int
    bytes_returned: int
    numbered_bytes_returned: int
    file_size_bytes: int
    scan_ratio: float
    return_to_scan_ratio: float
    returned_tokens: int
    full_file_tokens: int
    net_tokens_saved: int
    requested_lines: int
    returned_lines: int
    line_coverage: float
    byte_coverage: float


def estimate_tokens(text: str) -> int:
    """Estimate token count deterministically without external tokenizer dependencies."""
    if not text:
        return 0
    return math.ceil(len(text.encode("utf-8")) / _CHARS_PER_TOKEN_ESTIMATE)


def _estimate_tokens_from_bytes(num_bytes: int) -> int:
    if num_bytes <= 0:
        return 0
    return math.ceil(num_bytes / _CHARS_PER_TOKEN_ESTIMATE)


def measure_read_range(
    excerpt: LineRangeExcerpt,
    *,
    token_counter: Callable[[str], int] | None = None,
    numbered: bool = True,
) -> RangeReadMetrics:
    """Measure bytes scanned vs. returned, net token savings, and coverage."""
    rendered = excerpt.format(numbered=numbered)
    returned_tokens = token_counter(rendered) if token_counter is not None else estimate_tokens(
        rendered
    )
    file_size = excerpt.file_size_bytes if excerpt.file_size_bytes is not None else 0
    full_file_tokens = _estimate_tokens_from_bytes(file_size)
    scan_ratio = (excerpt.bytes_scanned / file_size) if file_size > 0 else 0.0
    return_to_scan_ratio = (
        (excerpt.bytes_returned / excerpt.bytes_scanned) if excerpt.bytes_scanned > 0 else 0.0
    )
    byte_coverage = (excerpt.bytes_returned / file_size) if file_size > 0 else 0.0
    return RangeReadMetrics(
        bytes_scanned=excerpt.bytes_scanned,
        bytes_returned=excerpt.bytes_returned,
        numbered_bytes_returned=excerpt.numbered_bytes_returned,
        file_size_bytes=file_size,
        scan_ratio=scan_ratio,
        return_to_scan_ratio=return_to_scan_ratio,
        returned_tokens=returned_tokens,
        full_file_tokens=full_file_tokens,
        net_tokens_saved=full_file_tokens - returned_tokens,
        requested_lines=excerpt.requested_line_count,
        returned_lines=excerpt.line_count,
        line_coverage=excerpt.coverage,
        byte_coverage=byte_coverage,
    )


def measure_read_ranges(
    excerpts: Sequence[LineRangeExcerpt],
    *,
    token_counter: Callable[[str], int] | None = None,
    numbered: bool = True,
) -> RangeReadMetrics:
    """Aggregate bytes scanned/returned, net token savings, and coverage across slices."""
    if not excerpts:
        return RangeReadMetrics(
            bytes_scanned=0,
            bytes_returned=0,
            numbered_bytes_returned=0,
            file_size_bytes=0,
            scan_ratio=0.0,
            return_to_scan_ratio=0.0,
            returned_tokens=0,
            full_file_tokens=0,
            net_tokens_saved=0,
            requested_lines=0,
            returned_lines=0,
            line_coverage=0.0,
            byte_coverage=0.0,
        )

    per_item = [
        measure_read_range(item, token_counter=token_counter, numbered=numbered)
        for item in excerpts
    ]
    bytes_scanned = sum(m.bytes_scanned for m in per_item)
    bytes_returned = sum(m.bytes_returned for m in per_item)
    numbered_bytes = sum(m.numbered_bytes_returned for m in per_item)
    # Count each distinct file's full size once when computing aggregate file size / tokens.
    unique_file_sizes: dict[str, int] = {}
    for item in excerpts:
        if item.file_size_bytes is not None:
            unique_file_sizes[item.path] = max(
                unique_file_sizes.get(item.path, 0), item.file_size_bytes
            )
    file_size = sum(unique_file_sizes.values())
    returned_tokens = sum(m.returned_tokens for m in per_item)
    full_file_tokens = _estimate_tokens_from_bytes(file_size)
    requested_lines = sum(m.requested_lines for m in per_item)
    returned_lines = sum(m.returned_lines for m in per_item)
    return RangeReadMetrics(
        bytes_scanned=bytes_scanned,
        bytes_returned=bytes_returned,
        numbered_bytes_returned=numbered_bytes,
        file_size_bytes=file_size,
        scan_ratio=(bytes_scanned / file_size) if file_size > 0 else 0.0,
        return_to_scan_ratio=(bytes_returned / bytes_scanned) if bytes_scanned > 0 else 0.0,
        returned_tokens=returned_tokens,
        full_file_tokens=full_file_tokens,
        net_tokens_saved=full_file_tokens - returned_tokens,
        requested_lines=requested_lines,
        returned_lines=returned_lines,
        line_coverage=(returned_lines / requested_lines) if requested_lines > 0 else 0.0,
        byte_coverage=(bytes_returned / file_size) if file_size > 0 else 0.0,
    )


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReadRangeError(f"{name} must be a positive integer, got {value!r}")
    return value


def _normalize_rel_path(rel_path: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(rel_path, str) or not rel_path or not rel_path.strip():
        raise PathGuardError("rel_path must be a non-empty relative path")
    if "\x00" in rel_path:
        raise PathGuardError("rel_path must not contain NUL bytes")

    win_path = PureWindowsPath(rel_path)
    if win_path.drive or win_path.is_absolute():
        raise PathGuardError(f"Absolute path is not allowed: {rel_path!r}")

    posix_raw = rel_path.replace("\\", "/")
    if posix_raw.startswith("/"):
        raise PathGuardError(f"Absolute path is not allowed: {rel_path!r}")

    posix_path = PurePosixPath(posix_raw)
    if posix_path.is_absolute():
        raise PathGuardError(f"Absolute path is not allowed: {rel_path!r}")

    parts: list[str] = []
    for part in posix_path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise PathGuardError(f"Path traversal is not allowed: {rel_path!r}")
        parts.append(part)

    if not parts:
        raise PathGuardError(f"rel_path does not name a file: {rel_path!r}")

    return "/".join(parts), tuple(parts)


def _resolve_safe_path(root: Path | str, rel_path: str) -> tuple[Path, str]:
    """Validate ``rel_path`` stays strictly inside ``root`` without symlink traversal."""
    normalized, parts = _normalize_rel_path(rel_path)
    try:
        resolved_root = Path(root).resolve(strict=True)
    except OSError as exc:
        raise PathGuardError(f"Root directory is not accessible: {root!r}") from exc
    if not resolved_root.is_dir():
        raise PathGuardError(f"Root is not a directory: {root!r}")

    current = resolved_root
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                raise PathGuardError(f"Symlink component rejected: {normalized!r}")
        except OSError as exc:
            raise PathGuardError(f"Cannot inspect path component: {normalized!r}") from exc

    try:
        resolved_target = current.resolve(strict=True)
    except OSError as exc:
        raise PathGuardError(f"File does not exist or is unreadable: {normalized!r}") from exc

    if resolved_target != current or not resolved_target.is_relative_to(resolved_root):
        raise PathGuardError(f"Resolved path escapes root or traverses symlink: {normalized!r}")
    if not resolved_target.is_file():
        raise PathGuardError(f"Target is not a regular file: {normalized!r}")

    return resolved_target, normalized


def _open_regular_file(path: Path, rel_path: str) -> tuple[Any, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise PathGuardError(f"Unable to open file safely: {rel_path!r}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PathGuardError(f"Target is not a regular file: {rel_path!r}")
        stream = os.fdopen(fd, "rb")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    return stream, int(st.st_size)


def _strip_line_ending(text: str) -> str:
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    return text


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate a valid Unicode string to at most ``max_bytes`` UTF-8 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    cut = max_bytes
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    return encoded[:cut].decode("utf-8")


def _check_scan_budget(
    bytes_scanned: int, max_scan_bytes: int | None, rel_path: str
) -> None:
    if max_scan_bytes is not None and bytes_scanned > max_scan_bytes:
        raise ScanLimitExceededError(
            f"Scanning {rel_path!r} exceeded max_scan_bytes ({max_scan_bytes})"
        )


def _bounded_read_limit(
    desired: int, bytes_scanned: int, max_scan_bytes: int | None
) -> int:
    if max_scan_bytes is None:
        return desired
    remaining = max_scan_bytes - bytes_scanned
    if remaining < 0:
        return 1
    return min(desired, remaining + 1)


def read_range(
    root: Path | str,
    rel_path: str,
    start_line: int,
    end_line: int,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_scan_bytes: int | None = DEFAULT_MAX_SCAN_BYTES,
    symbol: str = "",
) -> LineRangeExcerpt:
    """Stream a bounded, numbered UTF-8 line range from a regular file inside ``root``.

    Preconditions:
        - ``rel_path`` must resolve inside ``root`` without symlinks or traversal,
          and must name a regular file.
        - ``start_line >= 1``, ``end_line >= start_line``, ``max_lines >= 1``,
          ``max_bytes >= 1``.
        - If ``max_scan_bytes`` is not ``None``, it must be ``>= 1``.
        - Scanned bytes must not contain NUL bytes and must be valid UTF-8.
    Postconditions:
        - Returns an immutable ``LineRangeExcerpt`` with exact provenance and
          truncation flags.
        - ``len(result.lines) <= max_lines`` and ``result.bytes_returned <= max_bytes``.
        - Stops streaming as soon as the effective range or byte/line limit is reached.
    """
    req_start = _require_positive_int(start_line, "start_line")
    req_end = _require_positive_int(end_line, "end_line")
    if req_end < req_start:
        raise ReadRangeError(f"end_line ({req_end}) must be >= start_line ({req_start})")
    line_cap = _require_positive_int(max_lines, "max_lines")
    byte_cap = _require_positive_int(max_bytes, "max_bytes")
    scan_cap: int | None = (
        None if max_scan_bytes is None else _require_positive_int(max_scan_bytes, "max_scan_bytes")
    )

    target_path, normalized_path = _resolve_safe_path(root, rel_path)
    effective_end = min(req_end, req_start + line_cap - 1)

    bytes_scanned = 0
    bytes_returned = 0
    collected_lines: list[str] = []
    truncation_reasons: list[str] = []
    eof_reached = False
    total_lines: int | None = None

    stream, file_size_bytes = _open_regular_file(target_path, normalized_path)
    try:
        utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")

        # Phase 1: Stream and discard prefix lines before req_start without storing them.
        current_line = 1
        while current_line < req_start:
            utf8_decoder.reset()
            saw_any_bytes = False
            ended_with_newline = False
            while True:
                limit = _bounded_read_limit(_SKIP_CHUNK_BYTES, bytes_scanned, scan_cap)
                chunk = stream.readline(limit)
                if not chunk:
                    eof_reached = True
                    break
                saw_any_bytes = True
                bytes_scanned += len(chunk)
                _check_scan_budget(bytes_scanned, scan_cap, normalized_path)
                if b"\x00" in chunk:
                    raise BinaryFileError(
                        f"Binary NUL byte detected in {normalized_path!r} at line {current_line}"
                    )
                try:
                    utf8_decoder.decode(chunk, final=False)
                except UnicodeDecodeError as exc:
                    raise InvalidEncodingError(
                        f"Invalid UTF-8 in {normalized_path!r} at line {current_line}"
                    ) from exc
                if chunk.endswith(b"\n"):
                    ended_with_newline = True
                    break

            try:
                utf8_decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise InvalidEncodingError(
                    f"Invalid UTF-8 in {normalized_path!r} at line {current_line}"
                ) from exc

            if not saw_any_bytes:
                total_lines = current_line - 1
                eof_reached = True
                break
            if not ended_with_newline:
                total_lines = current_line
                eof_reached = True
                break
            current_line += 1

        # Phase 2: Stream and collect lines in [req_start, effective_end] within byte_cap.
        stopped_for_bytes = False
        while not eof_reached and current_line <= effective_end:
            separator_bytes = 1 if collected_lines else 0
            remaining_bytes = byte_cap - bytes_returned - separator_bytes

            if collected_lines and remaining_bytes < 0:
                peek_limit = _bounded_read_limit(1, bytes_scanned, scan_cap)
                peek = stream.read(peek_limit)
                if not peek:
                    eof_reached = True
                    total_lines = current_line - 1
                else:
                    bytes_scanned += len(peek)
                    _check_scan_budget(bytes_scanned, scan_cap, normalized_path)
                    if b"\x00" in peek:
                        raise BinaryFileError(
                            f"Binary NUL byte detected in {normalized_path!r} "
                            f"at line {current_line}"
                        )
                    truncation_reasons.append("max_bytes")
                    stopped_for_bytes = True
                break

            # Read enough bytes to accommodate remaining_bytes + CRLF + 1 overflow byte
            # (or a 4-byte UTF-8 character boundary on the very first line).
            desired_limit = (
                (byte_cap + 4) if not collected_lines else (remaining_bytes + 3)
            )
            limit = _bounded_read_limit(desired_limit, bytes_scanned, scan_cap)
            raw = stream.readline(limit)
            if not raw:
                eof_reached = True
                total_lines = current_line - 1
                break

            bytes_scanned += len(raw)
            _check_scan_budget(bytes_scanned, scan_cap, normalized_path)
            if b"\x00" in raw:
                raise BinaryFileError(
                    f"Binary NUL byte detected in {normalized_path!r} at line {current_line}"
                )

            line_complete = raw.endswith(b"\n") or len(raw) < limit
            hit_eof_mid_line = not raw.endswith(b"\n") and len(raw) < limit

            utf8_decoder.reset()
            try:
                decoded = utf8_decoder.decode(raw, final=line_complete)
            except UnicodeDecodeError as exc:
                raise InvalidEncodingError(
                    f"Invalid UTF-8 in {normalized_path!r} at line {current_line}"
                ) from exc

            if line_complete:
                line_text = _strip_line_ending(decoded)
                line_bytes = len(line_text.encode("utf-8"))
                if line_bytes <= remaining_bytes:
                    collected_lines.append(line_text)
                    bytes_returned += separator_bytes + line_bytes
                    if hit_eof_mid_line:
                        eof_reached = True
                        total_lines = current_line
                        break
                    current_line += 1
                    continue

                if collected_lines:
                    truncation_reasons.append("max_bytes")
                    stopped_for_bytes = True
                    break

                # First line itself exceeds byte_cap: return a UTF-8-safe prefix.
                truncated_line = _truncate_utf8(line_text, byte_cap)
                collected_lines.append(truncated_line)
                bytes_returned = len(truncated_line.encode("utf-8"))
                truncation_reasons.extend(("max_bytes", "line_truncated"))
                stopped_for_bytes = True
                break

            # Line did not complete within desired_limit -> strictly exceeds remaining_bytes.
            if collected_lines:
                truncation_reasons.append("max_bytes")
                stopped_for_bytes = True
                break

            # First line exceeds byte_cap and wasn't finished: truncate decoded prefix safely.
            partial = decoded[:-1] if decoded.endswith("\r") else decoded
            truncated_line = _truncate_utf8(partial, byte_cap)
            collected_lines.append(truncated_line)
            bytes_returned = len(truncated_line.encode("utf-8"))
            truncation_reasons.extend(("max_bytes", "line_truncated"))
            stopped_for_bytes = True
            break

        # Phase 3: If capped by max_lines before req_end, peek 1 byte to check if more exists.
        if not eof_reached and not stopped_for_bytes and effective_end < req_end:
            peek_limit = _bounded_read_limit(1, bytes_scanned, scan_cap)
            peek = stream.read(peek_limit)
            if not peek:
                eof_reached = True
                total_lines = effective_end
            else:
                bytes_scanned += len(peek)
                _check_scan_budget(bytes_scanned, scan_cap, normalized_path)
                if b"\x00" in peek:
                    raise BinaryFileError(
                        f"Binary NUL byte detected in {normalized_path!r} "
                        f"after line {effective_end}"
                    )
                truncation_reasons.append("max_lines")
    finally:
        stream.close()

    if collected_lines:
        actual_start = req_start
        actual_end = req_start + len(collected_lines) - 1
    else:
        actual_start = 0
        actual_end = 0

    return LineRangeExcerpt(
        path=normalized_path,
        start_line=actual_start,
        end_line=actual_end,
        lines=tuple(collected_lines),
        requested_start_line=req_start,
        requested_end_line=req_end,
        source="native",
        symbol=symbol,
        bytes_scanned=bytes_scanned,
        bytes_returned=bytes_returned,
        file_size_bytes=file_size_bytes,
        total_lines=total_lines,
        eof_reached=eof_reached,
        truncated=bool(truncation_reasons),
        truncation_reasons=tuple(truncation_reasons),
    )


def _relativize_cymbal_path(raw_path: str, root: Path | str | None) -> str:
    if root is None or "\x00" in raw_path:
        return raw_path
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return raw_path
    bases: list[Path] = []
    root_path = Path(root)
    try:
        bases.append(root_path.resolve(strict=True))
    except OSError:
        pass
    bases.append(root_path)
    for base in bases:
        try:
            rel = candidate.relative_to(base)
        except ValueError:
            continue
        if ".." in rel.parts:
            return raw_path
        return rel.as_posix()
    return raw_path


def _extract_cymbal_fields(
    payload: Mapping[str, Any] | str,
    *,
    root: Path | str | None = None,
) -> tuple[str, int, int, str, str | None, bool]:
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CymbalShowError(f"Invalid Cymbal show JSON: {exc}") from exc
    elif isinstance(payload, Mapping):
        parsed = payload
    else:
        raise CymbalShowError("Cymbal show payload must be a mapping or JSON string")

    if not isinstance(parsed, Mapping):
        raise CymbalShowError("Cymbal show JSON must decode to an object")

    outer = parsed
    results_obj = parsed.get("results")
    if isinstance(results_obj, Mapping):
        parsed = results_obj
    elif results_obj is not None:
        raise CymbalShowError("Cymbal show 'results' field must be an object")

    raw_symbol_field = parsed.get("symbol")
    symbol_meta: Mapping[str, Any] | None = None
    if isinstance(raw_symbol_field, Mapping):
        symbol_meta = raw_symbol_field
        raw_symbol = symbol_meta.get("name") or symbol_meta.get("symbol") or ""
        if not isinstance(raw_symbol, str):
            raise CymbalShowError("Cymbal show symbol.name must be a string")
    elif raw_symbol_field is None:
        raw_symbol = parsed.get("name") or ""
        if not isinstance(raw_symbol, str):
            raise CymbalShowError("Cymbal show symbol must be a string")
    elif isinstance(raw_symbol_field, str):
        raw_symbol = raw_symbol_field
    else:
        raise CymbalShowError("Cymbal show symbol must be a string or object")

    raw_path: Any = parsed.get("rel_path")
    sym_rel = symbol_meta.get("rel_path") if symbol_meta is not None else None
    sym_file = (
        symbol_meta.get("file") or symbol_meta.get("path")
        if symbol_meta is not None
        else None
    )
    entry_file = parsed.get("path") or parsed.get("file") or parsed.get("file_path")

    if not (isinstance(raw_path, str) and raw_path.strip()):
        if isinstance(entry_file, str) and entry_file.strip():
            relativized = _relativize_cymbal_path(entry_file, root)
            if (
                isinstance(sym_rel, str)
                and sym_rel.strip()
                and (entry_file == sym_file or Path(relativized).is_absolute())
            ):
                raw_path = sym_rel
            else:
                raw_path = relativized
        elif isinstance(sym_rel, str) and sym_rel.strip():
            raw_path = sym_rel
        elif isinstance(sym_file, str) and sym_file.strip():
            raw_path = _relativize_cymbal_path(sym_file, root)

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise CymbalShowError("Cymbal show payload is missing a valid path")

    raw_lines = parsed.get("lines")
    lines_start: int | None = None
    lines_end: int | None = None
    lines_body: str | None = None
    if raw_lines is not None:
        if not isinstance(raw_lines, list):
            raise CymbalShowError("Cymbal show field 'lines' must be a list")
        line_contents: list[str] = []
        line_nums: list[int] = []
        for item in raw_lines:
            if isinstance(item, Mapping):
                ln = item.get("line") if "line" in item else item.get("lineno")
                if ln is not None:
                    try:
                        line_nums.append(_require_positive_int(ln, "lines[].line"))
                    except ReadRangeError as exc:
                        raise CymbalShowError(str(exc)) from exc
                content = item.get("content") if "content" in item else item.get("text")
                if content is None:
                    content = ""
                if not isinstance(content, str):
                    raise CymbalShowError("Cymbal show lines[].content must be a string")
                line_contents.append(content.rstrip("\r\n"))
            elif isinstance(item, str):
                line_contents.append(item.rstrip("\r\n"))
            else:
                raise CymbalShowError("Cymbal show 'lines' items must be objects or strings")
        if line_nums:
            lines_start = line_nums[0]
            lines_end = line_nums[-1]
        if raw_lines:
            lines_body = "\n".join(line_contents)

    range_obj = parsed.get("range") or parsed.get("span")
    if isinstance(range_obj, Mapping):
        raw_start = (
            range_obj.get("start_line")
            if "start_line" in range_obj
            else range_obj.get("start")
        )
        raw_end = (
            range_obj.get("end_line")
            if "end_line" in range_obj
            else range_obj.get("end")
        )
    else:
        raw_start = (
            parsed.get("start_line")
            if "start_line" in parsed
            else parsed.get("line")
        )
        raw_end = parsed.get("end_line")

    if raw_start is None:
        if lines_start is not None:
            raw_start = lines_start
        elif symbol_meta is not None:
            raw_start = (
                symbol_meta.get("start_line")
                if "start_line" in symbol_meta
                else symbol_meta.get("line")
            )
    if raw_end is None:
        if lines_end is not None:
            raw_end = lines_end
        elif symbol_meta is not None and lines_body is None:
            raw_end = symbol_meta.get("end_line")

    body: str | None = None
    for key in ("body", "code", "source", "content", "text"):
        val = parsed.get(key)
        if val is not None:
            if not isinstance(val, str):
                raise CymbalShowError(f"Cymbal show field {key!r} must be a string")
            body = val
            break
    if body is None and lines_body is not None:
        body = lines_body

    try:
        start_line = _require_positive_int(raw_start, "start_line")
    except ReadRangeError as exc:
        raise CymbalShowError(str(exc)) from exc

    if raw_end is None:
        if body is not None:
            body_line_count = len(body.splitlines()) or 1
            end_line = start_line + body_line_count - 1
        else:
            end_line = start_line
    else:
        try:
            end_line = _require_positive_int(raw_end, "end_line")
        except ReadRangeError as exc:
            raise CymbalShowError(str(exc)) from exc

    if end_line < start_line:
        raise CymbalShowError(f"Cymbal show end_line ({end_line}) < start_line ({start_line})")

    upstream_truncated = bool(outer.get("truncated", False) or parsed.get("truncated", False))
    return raw_path, start_line, end_line, raw_symbol, body, upstream_truncated


def _body_matches_disk(body: str, disk: LineRangeExcerpt) -> bool:
    if "\x00" in body:
        return False
    if body == "":
        body_lines: list[str] = []
    else:
        normalized_body = body.replace("\r\n", "\n")
        if normalized_body.endswith("\n"):
            normalized_body = normalized_body[:-1]
        body_lines = normalized_body.split("\n")

    if disk.is_empty:
        return len(body_lines) == 0

    if len(body_lines) < len(disk.lines):
        return False

    if "line_truncated" in disk.truncation_reasons:
        # Only the first line was returned and truncated to max_bytes.
        return body_lines[0].startswith(disk.lines[0])

    if not disk.truncated and len(body_lines) != len(disk.lines):
        return False

    return tuple(body_lines[: len(disk.lines)]) == disk.lines


def validate_cymbal_show(
    root: Path | str,
    payload: Mapping[str, Any] | str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_scan_bytes: int | None = DEFAULT_MAX_SCAN_BYTES,
    strict: bool = False,
) -> LineRangeExcerpt:
    """Validate a Cymbal ``show`` payload against workspace guards and on-disk contents.

    Preconditions:
        - ``payload`` must be a mapping or JSON object with ``path``/``file`` and
          1-based ``start_line`` (and optional ``end_line``, ``symbol``, ``body``).
        - The referenced file must pass all native ``read_range`` path, symlink,
          binary, UTF-8, and scan-byte guards.
    Postconditions:
        - Returns an immutable ``LineRangeExcerpt`` bounded by ``max_lines`` and
          ``max_bytes``.
        - When ``body`` is present and matches disk (or when coordinates are
          validated directly), returns ``source="cymbal"``.
        - When ``body`` disagrees with disk (stale Cymbal index): raises
          ``CymbalShowError`` if ``strict=True``, otherwise returns the
          authoritative on-disk slice with ``source="native"``, ``stale=True``,
          and ``limitations=("cymbal_stale",)``.
    """
    raw_path, start_line, end_line, symbol, body, upstream_truncated = _extract_cymbal_fields(
        payload, root=root
    )
    disk_excerpt = read_range(
        root,
        raw_path,
        start_line,
        end_line,
        max_lines=max_lines,
        max_bytes=max_bytes,
        max_scan_bytes=max_scan_bytes,
        symbol=symbol,
    )

    reasons = list(disk_excerpt.truncation_reasons)
    if upstream_truncated and "cymbal_truncated" not in reasons:
        reasons.append("cymbal_truncated")

    if body is not None and not _body_matches_disk(body, disk_excerpt):
        if strict:
            raise CymbalShowError(
                f"Cymbal show body for {disk_excerpt.path}:{start_line}-{end_line} "
                "does not match file on disk"
            )
        limitations = (*disk_excerpt.limitations, "cymbal_stale")
        return replace(
            disk_excerpt,
            source="native",
            stale=True,
            truncated=bool(reasons),
            truncation_reasons=tuple(reasons),
            limitations=limitations,
        )

    return replace(
        disk_excerpt,
        source="cymbal",
        truncated=bool(reasons),
        truncation_reasons=tuple(reasons),
    )
