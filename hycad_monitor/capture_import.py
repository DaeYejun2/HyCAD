"""Import an approved Sysdig JSONL capture with the live UTC/quality policy.

This is an offline administrator command, never a browser-supplied file path.
Binary .scap and CSV are deliberately unsupported: first export the original
Sysdig fields without discarding null values, source timestamps or replica IDs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import pickle
import tempfile

from preprocess import FEATURES, TAU_NS, pick_replica_field
from .engine import MAX_CACHE_BYTES, MAX_CACHE_WINDOWS
from .stream import EventWindowAggregator

MAX_LINE_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_EXPANDED_BYTES = 8 * 1024**3
MAX_EVENTS = 20_000_000
ALLOWED_SUFFIXES = (".jsonl", ".jsonl.gz", ".ndjson", ".ndjson.gz", ".json", ".json.gz")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, content: bytes, overwrite: bool) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError("Output already exists; use --overwrite intentionally")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".capture-import-", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def import_capture(source: Path, output: Path, replicas: list[str], *, overwrite: bool = False,
                   max_source_bytes: int = MAX_SOURCE_BYTES,
                   max_expanded_bytes: int = MAX_EXPANDED_BYTES,
                   max_events: int = MAX_EVENTS) -> dict:
    """Return a provenance report and write a primitive aggregate pickle.

    No normal/attack label is inferred from the filename. Missing replicas and
    zero accepts remain in the cache for Detector to reject. The first and last
    windows are incomplete. Parser/field/order errors disqualify affected
    windows, and the original UTC boundary and quality survive later replay.
    """
    source, output = Path(source), Path(output)
    if not source.name.lower().endswith(ALLOWED_SUFFIXES):
        raise ValueError("Unsupported capture format; use Sysdig JSONL or JSONL.gz, not CSV/scap")
    if (len(replicas) < 2 or len(replicas) > 64 or len(set(replicas)) != len(replicas)
            or any(not isinstance(rep, str) or not rep or len(rep) > 256 for rep in replicas)):
        raise ValueError("Expected 2 to 64 unique replica names")
    if (not 0 < max_source_bytes <= MAX_SOURCE_BYTES
            or not 0 < max_expanded_bytes <= MAX_EXPANDED_BYTES
            or not 0 < max_events <= MAX_EVENTS):
        raise ValueError("Import limits must be positive and cannot exceed hard limits")
    if source.resolve() == output.resolve():
        raise ValueError("Input and output must differ")
    before = source.stat()
    if not source.is_file() or before.st_size > max_source_bytes:
        raise ValueError("Capture is not a regular file or exceeds the source size limit")
    if output.exists() and not overwrite:
        raise FileExistsError("Output already exists; use --overwrite intentionally")
    if output.with_suffix(".metadata.json").exists() and not overwrite:
        raise FileExistsError("Metadata output already exists; use --overwrite intentionally")

    aggregator = EventWindowAggregator(replicas)
    snapshots: list[dict] = []
    diagnostics = Counter()
    expanded_bytes = 0
    previous_timestamp = None
    accepted_last_ns = None
    serialized_window_bytes = 0
    source_first = source_last = None
    source_digest = file_sha256(source)

    def append_windows(rows: list[dict]) -> None:
        nonlocal serialized_window_bytes
        serialized_window_bytes += sum(len(pickle.dumps(row, protocol=4)) for row in rows)
        if serialized_window_bytes > MAX_CACHE_BYTES:
            raise ValueError("Capture exceeds the aggregate byte limit")
        snapshots.extend(rows)
        if len(snapshots) > MAX_CACHE_WINDOWS:
            raise ValueError("Capture exceeds the window count limit")

    opener = gzip.open if source.name.lower().endswith(".gz") else open
    with opener(source, "rb") as handle:
        while True:
            line = handle.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            expanded_bytes += len(line)
            if expanded_bytes > max_expanded_bytes:
                raise ValueError("Capture exceeds the expanded byte limit")
            if len(line) > MAX_LINE_BYTES:
                # Reject rather than parsing a suffix of a truncated event.
                raise ValueError("Capture contains an oversized JSONL event")
            if not line.strip():
                continue
            diagnostics["lines"] += 1
            if diagnostics["lines"] > max_events:
                raise ValueError("Capture exceeds the event count limit")
            if line.strip() in (b"!source_reset", b"!bridge_source_start"):
                aggregator.invalidate("source_reset")
                diagnostics["source_resets"] += 1
                continue
            try:
                event = json.loads(line)
                fields = event.get("output_fields", event)
                if not isinstance(fields, dict):
                    raise ValueError("Invalid event object")
            except (ValueError, AttributeError, UnicodeDecodeError):
                diagnostics["malformed_lines"] += 1
                aggregator.invalidate("malformed_event")
                continue

            # Other services in a shared capture do not poison this fixed cohort.
            replica = fields.get(pick_replica_field(fields))
            if not isinstance(replica, str) or not replica:
                diagnostics["malformed_lines"] += 1
                aggregator.invalidate("invalid_event")
                continue
            if replica not in replicas:
                diagnostics["other_replica_events"] += 1
                continue
            timestamp = fields.get("evt.rawtime")
            if isinstance(timestamp, str) and timestamp.isascii() and timestamp.isdigit():
                try:
                    timestamp = int(timestamp)
                except ValueError:
                    timestamp = None
            invalid_before = aggregator.diagnostics["invalid_events"]
            append_windows(aggregator.add(event))
            if aggregator.diagnostics["invalid_events"] == invalid_before:
                # Only a schema-valid event may supply an event-time watermark.
                # A malformed future timestamp must not extend the source span.
                if previous_timestamp is not None and timestamp < previous_timestamp:
                    diagnostics["out_of_order_events"] += 1
                    # Ordering within one bucket does not change count/set
                    # features. A genuinely late prior-bucket event is rejected
                    # by the live aggregator, and its old bucket is invalidated
                    # here too because the file is inspected retrospectively.
                    bucket_start = timestamp // TAU_NS * 30
                    for prior in reversed(snapshots):
                        if prior["start"] == bucket_start:
                            prior.update(complete=False, reason="late_event")
                            break
                        if prior["start"] < bucket_start:
                            break
                previous_timestamp = max(timestamp, previous_timestamp or 0)
                source_first = min(timestamp, source_first if source_first is not None else timestamp)
                source_last = max(timestamp, source_last if source_last is not None else timestamp)
                accepted_last_ns = max(timestamp, accepted_last_ns or 0)

    # EOF is never an event-time watermark; this closes only the last observed
    # bucket, explicitly marking its unknown observed end rather than filling it.
    if accepted_last_ns is not None:
        end = (accepted_last_ns // TAU_NS + 1) * 30
        tail = aggregator.close_until(end)
        for snapshot in tail:
            if snapshot["reason"] == "source_not_advanced":
                snapshot["reason"] = "capture_tail_duration_unknown"
        append_windows(tail)
    if not snapshots or not aggregator.diagnostics["accepted_events"]:
        raise ValueError("No valid events for the configured replicas")
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Capture changed while being imported; copy a closed capture first")

    t0 = int(snapshots[0]["start"]) * 1_000_000_000
    data = {"t0": t0, "feats": list(FEATURES), "freq": {}, "sets": {},
            "n": aggregator.diagnostics["accepted_events"], "window_alignment": "utc",
            "window_quality": {}, "replica_field": "explicit_cohort", "replicas": list(replicas)}
    for snapshot in snapshots:
        index = (int(snapshot["start"]) * 1_000_000_000 - t0) // TAU_NS
        for rep, values in snapshot["counts"].items():
            data["freq"][(index, rep)] = values
        for rep, values in snapshot["sets"].items():
            data["sets"][(index, rep)] = values
        data["window_quality"][index] = {"complete": snapshot["complete"], "reason": snapshot["reason"]}
    raw = pickle.dumps(data, protocol=4)
    if len(raw) > MAX_CACHE_BYTES:
        raise ValueError("Aggregate exceeds the cache size limit")
    report = {
        "schema_version": 1, "source_name": source.name, "source_sha256": source_digest,
        "source_bytes": before.st_size, "expanded_bytes": expanded_bytes,
        "cache_sha256": hashlib.sha256(raw).hexdigest(), "cache_bytes": len(raw),
        "replicas": list(replicas), "window_seconds": 30, "window_alignment": "utc",
        "source_first_event_ns": source_first, "source_last_event_ns": source_last,
        "window_count": len(snapshots),
        "complete_window_count": sum(row["complete"] for row in snapshots),
        "quality_reasons": dict(Counter(row["reason"] for row in snapshots if row["reason"])),
        "diagnostics": {**dict(diagnostics), **aggregator.diagnostics},
        "normality_verified": False,
        "notice": "Re-evaluation input only; a filename is not an attack label or a validated normal baseline.",
    }
    _atomic_write(output, raw, overwrite)
    _atomic_write(output.with_suffix(".metadata.json"),
                  (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(), overwrite)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--replicas", required=True, help="Comma-separated exact IDs; no auto-cohort inference")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        report = import_capture(args.input, args.output, args.replicas.split(","), overwrite=args.overwrite)
    except (OSError, ValueError, EOFError) as exc:
        parser.exit(1, f"Capture import failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
