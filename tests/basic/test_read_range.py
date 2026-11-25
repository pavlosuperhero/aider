from dataclasses import FrozenInstanceError
import json

import pytest

from aider.read_range import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    BinaryFileError,
    CymbalShowError,
    InvalidEncodingError,
    LineRangeExcerpt,
    PathGuardError,
    ReadRangeError,
    ScanLimitExceededError,
    estimate_tokens,
    measure_read_range,
    measure_read_ranges,
    read_range,
    validate_cymbal_show,
)


def _write_numbered_file(path, num_lines: int, *, pad: int = 40, newline: str = "\n") -> None:
    filler = "x" * pad
    content = newline.join(f"line_{i:05d} = '{filler}'" for i in range(1, num_lines + 1)) + newline
    path.write_bytes(content.encode("utf-8"))


def test_read_line_4000_in_over_128kib_file_streaming_and_metrics(tmp_path):
    big_file = tmp_path / "large_module.py"
    _write_numbered_file(big_file, 5000, pad=50)
    file_size = big_file.stat().st_size
    assert file_size > 131072  # Strictly larger than legacy 128 KiB read_source limit.

    excerpt = read_range(tmp_path, "large_module.py", 3995, 4005)
    assert isinstance(excerpt, LineRangeExcerpt)
    assert excerpt.path == "large_module.py"
    assert excerpt.source == "native"
    assert excerpt.start_line == 3995
    assert excerpt.end_line == 4005
    assert excerpt.requested_start_line == 3995
    assert excerpt.requested_end_line == 4005
    assert excerpt.line_count == 11
    assert not excerpt.truncated
    assert excerpt.truncation_reasons == ()
    assert not excerpt.incomplete
    assert excerpt.file_size_bytes == file_size
    assert excerpt.total_lines is None  # Stopped streaming early before EOF.

    # Verify exact numbered slice includes line 4000.
    assert excerpt.lines[0].startswith("line_03995 = ")
    assert excerpt.lines[5].startswith("line_04000 = ")
    assert excerpt.lines[-1].startswith("line_04005 = ")
    assert "4000: line_04000 = " in excerpt.numbered_code
    formatted = excerpt.format()
    assert formatted.startswith("large_module.py:3995-4005\n3995: line_03995 = ")
    assert "4000: line_04000 = " in formatted

    # Streaming proof: scanned up to line 4005, not the entire 5000-line file.
    assert excerpt.bytes_scanned < file_size
    assert excerpt.bytes_returned < excerpt.bytes_scanned
    assert excerpt.bytes_returned == len(excerpt.code.encode("utf-8"))

    # Measurement check: bytes scanned vs. returned, net tokens, and coverage.
    metrics = measure_read_range(excerpt)
    assert metrics.bytes_scanned == excerpt.bytes_scanned
    assert metrics.bytes_returned == excerpt.bytes_returned
    assert metrics.file_size_bytes == file_size
    assert 0.0 < metrics.scan_ratio < 0.9
    assert 0.0 < metrics.return_to_scan_ratio < 0.05
    assert metrics.returned_tokens > 0
    assert metrics.full_file_tokens > metrics.returned_tokens
    assert metrics.net_tokens_saved == metrics.full_file_tokens - metrics.returned_tokens
    assert metrics.net_tokens_saved > 0
    assert metrics.requested_lines == 11
    assert metrics.returned_lines == 11
    assert metrics.line_coverage == pytest.approx(1.0)
    assert 0.0 < metrics.byte_coverage < 0.02

    # Reading early lines scans only a tiny prefix of the >128 KiB file.
    early = read_range(tmp_path, "large_module.py", 1, 5)
    assert early.bytes_scanned < 1024
    assert early.bytes_scanned < excerpt.bytes_scanned


def test_excerpt_is_immutable_and_enforces_invariants(tmp_path):
    (tmp_path / "sample.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    excerpt = read_range(tmp_path, "sample.py", 1, 2)

    with pytest.raises(FrozenInstanceError):
        excerpt.start_line = 99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        excerpt.lines = ("mutated",)  # type: ignore[misc]

    assert isinstance(excerpt.lines, tuple)
    assert isinstance(excerpt.truncation_reasons, tuple)
    assert isinstance(excerpt.limitations, tuple)

    with pytest.raises(ReadRangeError):
        LineRangeExcerpt(
            path="sample.py",
            start_line=1,
            end_line=5,
            lines=("only_one",),
            requested_start_line=1,
            requested_end_line=5,
        )

    with pytest.raises(ReadRangeError):
        LineRangeExcerpt(
            path="sample.py",
            start_line=1,
            end_line=1,
            lines=(),
            requested_start_line=1,
            requested_end_line=1,
        )


def test_path_symlink_traversal_and_non_file_guards(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sub = repo / "pkg"
    sub.mkdir()
    target = sub / "mod.py"
    target.write_text("x = 1\ny = 2\n", encoding="utf-8")

    outside = tmp_path / "outside.py"
    outside.write_text("secret = 42\n", encoding="utf-8")

    (repo / "link_to_file.py").symlink_to(target)
    (repo / "link_outside.py").symlink_to(outside)
    (repo / "link_dir").symlink_to(sub)

    # Valid nested relative path succeeds and normalizes cleanly.
    ok = read_range(repo, "./pkg/mod.py", 1, 2)
    assert ok.path == "pkg/mod.py"
    assert ok.lines == ("x = 1", "y = 2")

    # Reject traversal, absolute paths, symlinks, symlinked dirs, directories, missing files.
    bad_paths = [
        "",
        "   ",
        ".",
        "../outside.py",
        "pkg/../../outside.py",
        "pkg/../pkg/mod.py",
        str(target),
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "pkg/mod.py\x00.txt",
        "link_to_file.py",
        "link_outside.py",
        "link_dir/mod.py",
        "pkg",
        "does_not_exist.py",
    ]
    for bad in bad_paths:
        with pytest.raises(PathGuardError):
            read_range(repo, bad, 1, 2)

    with pytest.raises(PathGuardError):
        read_range(tmp_path / "missing_root", "mod.py", 1, 2)


def test_argument_precondition_validation(tmp_path):
    (tmp_path / "a.py").write_text("one\ntwo\n", encoding="utf-8")

    for kwargs in [
        dict(start_line=0, end_line=1),
        dict(start_line=-5, end_line=1),
        dict(start_line=3, end_line=2),
        dict(start_line=True, end_line=2),
        dict(start_line=1, end_line=False),
        dict(start_line=1, end_line=2, max_lines=0),
        dict(start_line=1, end_line=2, max_bytes=0),
        dict(start_line=1, end_line=2, max_scan_bytes=0),
        dict(start_line=1, end_line=2, max_lines=True),
    ]:
        with pytest.raises(ReadRangeError):
            read_range(tmp_path, "a.py", **kwargs)


def test_binary_and_utf8_guards_including_prefix_and_multibyte_chunks(tmp_path):
    # NUL byte in target line.
    (tmp_path / "bin_direct.dat").write_bytes(b"abc\x00def\n")
    with pytest.raises(BinaryFileError):
        read_range(tmp_path, "bin_direct.dat", 1, 1)

    # NUL byte in skipped prefix line before start_line is still rejected.
    (tmp_path / "bin_prefix.dat").write_bytes(b"hdr\x00\nline2\nline3\n")
    with pytest.raises(BinaryFileError):
        read_range(tmp_path, "bin_prefix.dat", 2, 3)

    # Invalid UTF-8 in target line and in prefix line.
    (tmp_path / "bad_utf8.py").write_bytes(b"\xff\xfeimport os\n")
    with pytest.raises(InvalidEncodingError):
        read_range(tmp_path, "bad_utf8.py", 1, 1)

    (tmp_path / "bad_utf8_prefix.py").write_bytes(b"ok\n\x80invalid\nvalid\n")
    with pytest.raises(InvalidEncodingError):
        read_range(tmp_path, "bad_utf8_prefix.py", 3, 3)

    # Truncated multi-byte UTF-8 sequence at EOF.
    (tmp_path / "truncated_utf8.py").write_bytes(b"valid\n\xe2\x82")
    with pytest.raises(InvalidEncodingError):
        read_range(tmp_path, "truncated_utf8.py", 2, 2)

    # Valid multi-byte UTF-8 spanning the 64 KiB skip-chunk boundary succeeds cleanly.
    long_prefix = ("a" * 65534) + "\U0001f680" + "\n" + "target = 'caf\u00e9 \U0001f30d'\n"
    (tmp_path / "utf8_boundary.py").write_bytes(long_prefix.encode("utf-8"))
    excerpt = read_range(tmp_path, "utf8_boundary.py", 2, 2)
    assert excerpt.lines == ("target = 'caf\u00e9 \U0001f30d'",)
    assert not excerpt.truncated


def test_max_lines_truncation_vs_exact_eof(tmp_path):
    _write_numbered_file(tmp_path / "many.py", 200, pad=10)

    # Default max_lines=120 caps 1..200 and reports max_lines truncation.
    capped = read_range(tmp_path, "many.py", 1, 200)
    assert capped.line_count == DEFAULT_MAX_LINES
    assert capped.start_line == 1
    assert capped.end_line == DEFAULT_MAX_LINES
    assert capped.requested_start_line == 1
    assert capped.requested_end_line == 200
    assert capped.truncated is True
    assert capped.truncation_reasons == ("max_lines",)
    assert capped.incomplete is True
    assert capped.coverage == pytest.approx(120 / 200)
    assert "[TRUNCATED: max_lines]" in capped.format()

    # File ending exactly at max_lines is NOT truncated.
    _write_numbered_file(tmp_path / "exact120.py", 120, pad=10)
    exact = read_range(tmp_path, "exact120.py", 1, 200)
    assert exact.line_count == 120
    assert exact.truncated is False
    assert exact.truncation_reasons == ()
    assert exact.eof_reached is True
    assert exact.total_lines == 120

    # File shorter than requested range is NOT truncated, reports EOF cleanly.
    _write_numbered_file(tmp_path / "short.py", 10, pad=5)
    short = read_range(tmp_path, "short.py", 5, 50)
    assert short.start_line == 5
    assert short.end_line == 10
    assert short.line_count == 6
    assert short.truncated is False
    assert short.eof_reached is True
    assert short.total_lines == 10


def test_max_bytes_truncation_line_boundary_and_utf8_single_line(tmp_path):
    (tmp_path / "lines.py").write_text(
        "aaaa\nbbbb\ncccc\ndddd\n", encoding="utf-8"
    )
    # "aaaa\nbbbb" = 9 bytes; adding "\ncccc" would be 14 bytes > 10.
    bounded = read_range(tmp_path, "lines.py", 1, 4, max_bytes=10)
    assert bounded.lines == ("aaaa", "bbbb")
    assert bounded.start_line == 1
    assert bounded.end_line == 2
    assert bounded.bytes_returned == 9
    assert bounded.bytes_returned <= 10
    assert bounded.truncated is True
    assert bounded.truncation_reasons == ("max_bytes",)

    # Exact fit at max_bytes with CRLF is NOT truncated.
    (tmp_path / "crlf.py").write_bytes(b"aaaa\r\nbbbb\r\n")
    exact_crlf = read_range(tmp_path, "crlf.py", 1, 2, max_bytes=9)
    assert exact_crlf.lines == ("aaaa", "bbbb")
    assert exact_crlf.bytes_returned == 9
    assert exact_crlf.truncated is False

    # Single first line exceeding max_bytes truncates cleanly on a UTF-8 boundary.
    emoji_line = "ab\U0001f600cd\nnext_line\n"  # 'ab' (2B) + emoji (4B) + 'cd' (2B)
    (tmp_path / "emoji.py").write_bytes(emoji_line.encode("utf-8"))
    cut_mid_emoji = read_range(tmp_path, "emoji.py", 1, 2, max_bytes=4)
    assert cut_mid_emoji.lines == ("ab",)
    assert cut_mid_emoji.bytes_returned == 2
    assert cut_mid_emoji.bytes_returned <= 4
    assert cut_mid_emoji.truncated is True
    assert cut_mid_emoji.truncation_reasons == ("max_bytes", "line_truncated")

    # Default max_bytes=16384 bounds very wide lines cleanly.
    wide = ("w" * 25000) + "\nsecond\n"
    (tmp_path / "wide.py").write_text(wide, encoding="utf-8")
    wide_excerpt = read_range(tmp_path, "wide.py", 1, 2)
    assert wide_excerpt.bytes_returned == DEFAULT_MAX_BYTES
    assert wide_excerpt.truncated is True
    assert wide_excerpt.truncation_reasons == ("max_bytes", "line_truncated")


def test_empty_file_past_eof_and_no_trailing_newline(tmp_path):
    (tmp_path / "empty.py").write_text("", encoding="utf-8")
    empty = read_range(tmp_path, "empty.py", 1, 10)
    assert empty.is_empty
    assert empty.lines == ()
    assert empty.start_line == 0
    assert empty.end_line == 0
    assert empty.eof_reached is True
    assert empty.total_lines == 0
    assert empty.truncated is False
    assert empty.coverage == 0.0
    assert "[EMPTY: EOF at line 0]" in empty.format()

    (tmp_path / "no_nl.py").write_text("one\ntwo\nthree", encoding="utf-8")
    tail = read_range(tmp_path, "no_nl.py", 2, 5)
    assert tail.lines == ("two", "three")
    assert tail.start_line == 2
    assert tail.end_line == 3
    assert tail.eof_reached is True
    assert tail.total_lines == 3
    assert tail.truncated is False

    past = read_range(tmp_path, "no_nl.py", 10, 20)
    assert past.is_empty
    assert past.total_lines == 3
    assert past.eof_reached is True
    assert "[EMPTY: EOF at line 3]" in past.format()


def test_max_scan_bytes_guard_allows_prefix_reads_and_blocks_deep_scans(tmp_path):
    _write_numbered_file(tmp_path / "deep.py", 2000, pad=40)

    # Reading near top works even when max_scan_bytes is much smaller than the file.
    top = read_range(tmp_path, "deep.py", 1, 3, max_scan_bytes=1024)
    assert top.line_count == 3
    assert top.bytes_scanned <= 1024

    # Reading deep into the file trips max_scan_bytes deterministically.
    with pytest.raises(ScanLimitExceededError):
        read_range(tmp_path, "deep.py", 1000, 1005, max_scan_bytes=1024)


def test_validate_cymbal_show_valid_truncated_stale_and_guarded(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = (
        "def alpha():\n"
        "    return 1\n\n"
        "def copy(self):\n"
        "    p = PreparedRequest()\n"
        "    p.method = self.method\n"
        "    return p\n"
    )
    (pkg / "models.py").write_text(src, encoding="utf-8")

    # Valid JSON string payload matching disk returns source='cymbal'.
    payload_json = json.dumps(
        {
            "file": "pkg/models.py",
            "start_line": 4,
            "end_line": 7,
            "symbol": "copy",
            "body": (
                "def copy(self):\n"
                "    p = PreparedRequest()\n"
                "    p.method = self.method\n"
                "    return p\n"
            ),
        }
    )
    valid = validate_cymbal_show(tmp_path, payload_json)
    assert valid.source == "cymbal"
    assert valid.symbol == "copy"
    assert valid.path == "pkg/models.py"
    assert valid.start_line == 4
    assert valid.end_line == 7
    assert not valid.stale
    assert not valid.truncated
    assert "pkg/models.py:4-7 (copy) [cymbal]" in valid.format()

    # Upstream truncated flag is preserved and bounded by max_lines.
    truncated_payload = {
        "path": "pkg/models.py",
        "range": {"start_line": 1, "end_line": 7},
        "name": "models",
        "truncated": True,
    }
    capped = validate_cymbal_show(tmp_path, truncated_payload, max_lines=3)
    assert capped.source == "cymbal"
    assert capped.line_count == 3
    assert capped.truncated is True
    assert capped.truncation_reasons == ("max_lines", "cymbal_truncated")

    # Stale Cymbal body falls back to authoritative disk content by default.
    stale_payload = {
        "path": "pkg/models.py",
        "start_line": 4,
        "end_line": 7,
        "symbol": "copy",
        "body": "def copy(self):\n    return None\n    # outdated\n    pass\n",
    }
    fallback = validate_cymbal_show(tmp_path, stale_payload)
    assert fallback.source == "native"
    assert fallback.stale is True
    assert fallback.incomplete is True
    assert fallback.limitations == ("cymbal_stale",)
    assert fallback.lines[1] == "    p = PreparedRequest()"
    assert "[INCOMPLETE: cymbal_stale]" in fallback.format()

    # In strict mode, stale Cymbal body raises CymbalShowError.
    with pytest.raises(CymbalShowError, match="does not match file on disk"):
        validate_cymbal_show(tmp_path, stale_payload, strict=True)

    # Single blank line in Cymbal body matches blank line on disk (not marked stale).
    blank_valid = validate_cymbal_show(
        tmp_path,
        {"path": "pkg/models.py", "start_line": 3, "end_line": 3, "body": "\n"},
    )
    assert blank_valid.source == "cymbal"
    assert not blank_valid.stale
    assert blank_valid.lines == ("",)

    # Path traversal in Cymbal payload is still blocked by PathGuardError.
    with pytest.raises(PathGuardError):
        validate_cymbal_show(
            tmp_path, {"path": "../outside.py", "start_line": 1, "end_line": 1}
        )

    # Malformed payloads raise CymbalShowError.
    for bad_payload in [
        "{not json",
        "[]",
        {},
        {"path": "pkg/models.py", "start_line": 0},
        {"path": "pkg/models.py", "start_line": 5, "end_line": 2},
        {"path": "pkg/models.py", "start_line": 1, "body": 123},
        {"version": "0.1", "results": "not-an-object"},
        {"version": "0.1", "results": {"file": "pkg/models.py", "lines": "not-a-list"}},
    ]:
        with pytest.raises(CymbalShowError):
            validate_cymbal_show(tmp_path, bad_payload)


def test_validate_cymbal_show_v0_18_0_json_payloads(tmp_path):
    (tmp_path / "api.py").write_text(
        "def parse(x):\n    return int(x)\n", encoding="utf-8"
    )
    (tmp_path / "consumer.py").write_text(
        "from api import parse\n\ndef run(v):\n    return parse(v) + 1\n",
        encoding="utf-8",
    )
    outside = tmp_path.parent / "outside_read_range_guard.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    (tmp_path / "link_outside.py").symlink_to(outside)

    # 1. Live v0.18.0 symbol show payload with wrapped results dict, lines[], and symbol dict.
    symbol_payload = {
        "version": "0.1",
        "results": {
            "file": "/abs/api.py",
            "lines": [
                {"line": 1, "content": "def parse(x):"},
                {"line": 2, "content": "    return int(x)"},
            ],
            "symbol": {
                "name": "parse",
                "kind": "function",
                "file": "/abs/api.py",
                "rel_path": "api.py",
                "start_line": 1,
                "end_line": 2,
                "depth": 0,
                "signature": "(x)",
                "language": "python",
                "body_hash": "deadbeef",
            },
        },
    }
    sym_excerpt = validate_cymbal_show(tmp_path, json.dumps(symbol_payload))
    assert sym_excerpt.source == "cymbal"
    assert sym_excerpt.path == "api.py"
    assert sym_excerpt.symbol == "parse"
    assert sym_excerpt.start_line == 1
    assert sym_excerpt.end_line == 2
    assert sym_excerpt.lines == ("def parse(x):", "    return int(x)")
    assert sym_excerpt.stale is False
    assert sym_excerpt.truncated is False

    # 2. Live v0.18.0 file:start-end show payload without symbol dict, normalizing abs 'file'.
    range_payload = {
        "version": "0.1",
        "results": {
            "file": str(tmp_path.resolve() / "consumer.py"),
            "lines": [
                {"line": 3, "content": "def run(v):"},
                {"line": 4, "content": "    return parse(v) + 1"},
            ],
        },
    }
    range_excerpt = validate_cymbal_show(tmp_path, range_payload)
    assert range_excerpt.source == "cymbal"
    assert range_excerpt.path == "consumer.py"
    assert range_excerpt.symbol == ""
    assert range_excerpt.start_line == 3
    assert range_excerpt.end_line == 4
    assert range_excerpt.lines == ("def run(v):", "    return parse(v) + 1")
    assert range_excerpt.stale is False

    # 3. Stale v0.18.0 lines content falls back to disk (or raises in strict mode).
    stale_v018 = {
        "version": "0.1",
        "results": {
            "file": str(tmp_path.resolve() / "consumer.py"),
            "lines": [
                {"line": 3, "content": "def run(v):"},
                {"line": 4, "content": "    return OLD_CALL(v)"},
            ],
        },
    }
    stale_excerpt = validate_cymbal_show(tmp_path, stale_v018)
    assert stale_excerpt.source == "native"
    assert stale_excerpt.stale is True
    assert stale_excerpt.limitations == ("cymbal_stale",)
    assert stale_excerpt.lines == ("def run(v):", "    return parse(v) + 1")
    with pytest.raises(CymbalShowError, match="does not match file on disk"):
        validate_cymbal_show(tmp_path, stale_v018, strict=True)

    # 4. Security guards: outside absolute 'file' and symlinked absolute 'file' are blocked.
    for unsafe_file in (str(outside), str(tmp_path.resolve() / "link_outside.py")):
        with pytest.raises(PathGuardError):
            validate_cymbal_show(
                tmp_path,
                {
                    "version": "0.1",
                    "results": {
                        "file": unsafe_file,
                        "lines": [{"line": 1, "content": "secret = 1"}],
                    },
                },
            )


def test_measure_read_ranges_aggregates_and_deduplicates_file_size(tmp_path):
    _write_numbered_file(tmp_path / "big.py", 1000, pad=30)
    a = read_range(tmp_path, "big.py", 10, 19)
    b = read_range(tmp_path, "big.py", 500, 509)

    agg = measure_read_ranges([a, b])
    assert agg.bytes_scanned == a.bytes_scanned + b.bytes_scanned
    assert agg.bytes_returned == a.bytes_returned + b.bytes_returned
    assert agg.file_size_bytes == (tmp_path / "big.py").stat().st_size
    assert agg.requested_lines == 20
    assert agg.returned_lines == 20
    assert agg.line_coverage == pytest.approx(1.0)
    assert agg.net_tokens_saved > 0
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2

    empty_agg = measure_read_ranges([])
    assert empty_agg.bytes_scanned == 0
    assert empty_agg.line_coverage == 0.0
