"""Local publishing archive for candidate incident reports.

The archive is a single offline JSON file. Each successful publishing batch
turns a :func:`wildfire.core.cluster` report into the current incident state:

* new incidents publish a new immutable version,
* incidents whose full content changed withdraw the old version and then
  publish a new one,
* disappeared incidents are withdrawn only,
* unchanged incidents produce no records.

Version identities (``v1``, ``v2`` ...) and record identities (``r1``,
``r2`` ...) come from monotonic counters and are never reused, even when an
incident disappears and later comes back. Every consumer tracks its own set
of acknowledged record ids.
"""

import contextlib
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .core import cluster

SCHEMA_VERSION = 1


class ArchiveError(Exception):
    """Raised for archive-level validation or consistency failures."""


def input_digest(data):
    """Stable hash of a parsed observation JSON payload."""
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def init_archive(path, consumers):
    """Create a new archive file registering the given consumer ids."""
    consumers = list(consumers)
    if not consumers:
        raise ArchiveError("at least one consumer_id is required")
    if any(not isinstance(c, str) or not c for c in consumers):
        raise ArchiveError("consumer ids must be non-empty strings")
    if len(set(consumers)) != len(consumers):
        raise ArchiveError("consumer ids must be unique")
    path = Path(path)
    if path.exists():
        raise ArchiveError(f"archive already exists: {path}")
    state = {
        "schema_version": SCHEMA_VERSION,
        "consumers": consumers,
        "next_version": 1,
        "next_record": 1,
        "current": {},
        "versions": {},
        "records": [],
        "batches": {},
        "acks": {consumer: [] for consumer in consumers},
    }
    _atomic_write(path, state)
    return state


def publish(path, batch_id, raw_input):
    """Publish one clustering batch and return its receipt.

    Replays (same ``batch_id`` and identical input) return the stored receipt
    without allocating new identities. The same ``batch_id`` with different
    input raises :class:`ArchiveError`. Clustering failures propagate before
    the archive is touched.
    """
    if not isinstance(batch_id, str) or not batch_id:
        raise ArchiveError("batch_id must be a non-empty string")
    digest = input_digest(raw_input)
    state = _load(Path(path))

    prior = state["batches"].get(batch_id)
    if prior is not None:
        if prior["input_hash"] != digest:
            raise ArchiveError(
                f"batch_id {batch_id!r} was already published with different input")
        return dict(prior["receipt"])

    # Reuse the existing clustering / maintenance-window rules. A failure here
    # raises before any state is mutated or written.
    report = cluster(raw_input)
    new_incidents = {item["incident_id"]: item for item in report["incidents"]}

    new_records = []
    published = []
    withdrawn = []
    for incident_id in sorted(set(state["current"]) | set(new_incidents)):
        if incident_id not in new_incidents:
            entry = state["current"].pop(incident_id)
            record = _add_record(state, "withdraw", incident_id,
                                 entry["version"], batch_id)
            new_records.append(record)
            withdrawn.append(_ref(record))
            continue

        new_event = new_incidents[incident_id]
        old_entry = state["current"].get(incident_id)
        if old_entry is None:
            version = _add_version(state, incident_id, new_event)
            record = _add_record(state, "publish", incident_id, version, batch_id)
            new_records.append(record)
            published.append(_ref(record))
        elif old_entry["event"] != new_event:
            # Withdraw must be recorded before the replacement publish.
            record = _add_record(state, "withdraw", incident_id,
                                 old_entry["version"], batch_id)
            new_records.append(record)
            withdrawn.append(_ref(record))
            version = _add_version(state, incident_id, new_event)
            record = _add_record(state, "publish", incident_id, version, batch_id)
            new_records.append(record)
            published.append(_ref(record))
        # Unchanged content: no version and no record.

    receipt = {
        "batch_id": batch_id,
        "input_hash": digest,
        "observation_count": report["observation_count"],
        "incident_ids": sorted(new_incidents),
        "record_ids": [record["record_id"] for record in new_records],
        "published": published,
        "withdrawn": withdrawn,
    }
    state["batches"][batch_id] = {"input_hash": digest, "receipt": receipt}
    _atomic_write(Path(path), state)
    return dict(receipt)


def pending(path, consumer_id):
    """Return the consumer's unacknowledged records in record order.

    Each record is augmented with the immutable full content of the version it
    references. Consumers never see records they have already acknowledged.
    """
    state = _load(Path(path))
    _require_consumer(state, consumer_id)
    acknowledged = set(state["acks"][consumer_id])
    result = []
    for record in state["records"]:
        if record["record_id"] in acknowledged:
            continue
        entry = dict(record)
        # Hand out a copy so callers cannot mutate the stored immutable event.
        entry["event"] = copy.deepcopy(state["versions"][record["version"]])
        result.append(entry)
    return result


def acknowledge(path, consumer_id, record_id):
    """Acknowledge one record for one consumer; duplicates are harmless."""
    if not isinstance(record_id, str) or not record_id:
        raise ArchiveError("record id must be a non-empty string")
    state = _load(Path(path))
    _require_consumer(state, consumer_id)
    known = {record["record_id"] for record in state["records"]}
    if record_id not in known:
        raise ArchiveError(f"unknown record: {record_id!r}")
    acknowledged = state["acks"][consumer_id]
    duplicate = record_id in acknowledged
    if not duplicate:
        acknowledged.append(record_id)
        _atomic_write(Path(path), state)
    return {
        "consumer_id": consumer_id,
        "record_id": record_id,
        "acknowledged": True,
        "duplicate": duplicate,
    }


def _ref(record):
    return {
        "incident_id": record["incident_id"],
        "version": record["version"],
        "record_id": record["record_id"],
    }


def _add_version(state, incident_id, event):
    version = f"v{state['next_version']}"
    state["next_version"] += 1
    # Stored events are never mutated: a content change always allocates a new
    # version and replaces the current pointer.
    state["versions"][version] = event
    state["current"][incident_id] = {"version": version, "event": event}
    return version


def _add_record(state, kind, incident_id, version, batch_id):
    seq = state["next_record"]
    record = {
        "record_id": f"r{seq}",
        "seq": seq,
        "kind": kind,
        "incident_id": incident_id,
        "version": version,
        "batch_id": batch_id,
    }
    state["next_record"] += 1
    state["records"].append(record)
    return record


def _require_consumer(state, consumer_id):
    if consumer_id not in state["consumers"]:
        raise ArchiveError(f"unknown consumer: {consumer_id!r}")


def _load(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ArchiveError(f"archive not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"archive is not valid JSON: {exc}") from None
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise ArchiveError("unrecognized archive format")
    return state


def _atomic_write(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent,
                                    prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory):
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
