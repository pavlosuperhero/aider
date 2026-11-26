"""Durable JSONL checkpoints for held-out trials."""

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tempfile


def fingerprint(metadata, cases, root):
    """Pin the schedule, materialized fixtures and code even on a dirty checkout."""
    config = {
        key: value
        for key, value in metadata.items()
        if key not in {"run_id", "timestamp_utc", "git_dirty"}
    }
    digest = sha256(json.dumps(config, sort_keys=True).encode())
    for case in cases:
        digest.update(json.dumps(asdict(case), sort_keys=True).encode())

    paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "-coz",
            "--exclude-standard",
            "--",
            "aider",
            "scripts/local_context_trial",
        ],
        cwd=root,
    )
    for name in sorted(set(os.fsdecode(paths).split("\0")) - {""}):
        path = root / name
        if name.startswith("scripts/") and path.suffix != ".py":
            continue
        contents = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        digest.update(json.dumps([name, contents]).encode())
    return digest.hexdigest()


def _sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def open_run(out, metadata, signature, attempt_count):
    """Open or create a checkpoint while the caller holds the output lock."""
    sidecar = out.with_suffix(".meta.json")
    if sidecar.exists():
        saved = json.loads(sidecar.read_text(encoding="utf-8"))["latest"]
        if not isinstance(saved, dict) or saved.get("checkpoint_signature") != signature:
            raise ValueError("checkpoint inputs changed or output belongs to a different run")
        return saved, _read_rows(out, saved["run_id"], attempt_count)
    if out.exists():
        raise ValueError("results exist without checkpoint metadata; choose a new output file")

    metadata = dict(metadata, checkpoint_signature=signature, planned_attempts=attempt_count)
    payload = {"schema_version": metadata["schema_version"], "latest": metadata, "runs": [metadata]}
    fd, temporary = tempfile.mkstemp(dir=sidecar.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, sidecar)
    finally:
        Path(temporary).unlink(missing_ok=True)
    _sync_directory(sidecar.parent)
    return metadata, []


def _read_rows(out, run_id, attempt_count):
    if not out.exists():
        return []
    rows = []
    with out.open("rb+") as stream:
        while line := stream.readline():
            if not line.endswith(b"\n"):
                stream.truncate(stream.tell() - len(line))
                os.fsync(stream.fileno())
                break
            row = json.loads(line)
            if (
                not isinstance(row, dict)
                or len(rows) >= attempt_count
                or row.get("checkpoint_job") != len(rows)
                or row.get("metadata", {}).get("run_id") != run_id
            ):
                raise ValueError(f"unexpected checkpoint row at line {len(rows) + 1}")
            rows.append(row)
    return rows


def append(out, row):
    new_file = not out.exists()
    with out.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if new_file:
        _sync_directory(out.parent)
