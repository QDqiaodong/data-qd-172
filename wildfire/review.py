"""Offline human review workbench for candidate incidents.

The workbench is a single local JSON file, independent from the publishing
archive in :mod:`wildfire.archive`. Analysts import the *full* current
observation set (the same payload the standalone report and the publishing
archive accept); the workbench re-derives the current incidents with the
existing :func:`wildfire.core.cluster` rules, including maintenance-window
suppression, and reconciles them against the active review cases. Dashboard
acknowledgements in the publishing archive never count as review conclusions.

Each review object gets a ``case_id`` (``c1``, ``c2``, ...) that is never
reused, entirely separate from the clustering ``incident_id``:

* an incident that appears for the first time creates a new case;
* between two imports, a previously active incident and a new incident are
  directly linked when their valid observation id sets intersect;
* the old ``case_id`` continues only when old and new incident are each
  other's unique link -- member changes and ``incident_id`` changes do not
  break a one-to-one continuation;
* merges, splits, and cross-reorganizations retire every involved old case
  and create fresh cases, keeping *all* direct predecessor/successor links
  together with their shared observation ids;
* retired cases never come back: a disappeared incident whose observation
  ids later return verbatim is a brand new review object.

Every active case carries a review basis (the full incident report, each
valid member's id/sensor/time/lat/lon/severity, and the import's radius_km
and window_minutes), a per-case revision, an owner, and an optional human
conclusion. A one-to-one continuation keeps conclusion, owner, and revision
only when the basis is unchanged; any basis change (including a coordinate
or time correction that leaves the cluster report identical) clears the
current conclusion and re-opens the case for review. Cases born from merges,
splits, or cross-reorganizations never inherit a conclusion; they inherit an
owner only when every direct predecessor has the same non-empty owner.

Successful assignments and conclusions bump the case's revision; basis
changes and retirement invalidate old revisions. Other cases never affect
this case's revision. Every mutation and its audit record commit through a
single atomic file replacement, so a failed write leaves the previous
workbench untouched.
"""

import contextlib
import copy
import json
import os
import tempfile
from pathlib import Path

from .core import cluster

SCHEMA_VERSION = 1

MEMBER_FIELDS = ("id", "sensor", "time", "lat", "lon", "severity")

VERDICTS = ("confirmed", "dismissed")


class ReviewError(Exception):
    """Raised for workbench-level validation or consistency failures."""


def init_workbench(path):
    """Create a new, empty workbench file. Refuses to overwrite."""
    path = Path(path)
    if path.exists():
        raise ReviewError(f"workbench already exists: {path}")
    state = {
        "schema_version": SCHEMA_VERSION,
        "next_case": 1,
        "next_event": 1,
        "next_import": 1,
        "cases": {},
        "events": [],
    }
    _atomic_write(path, state)
    return {"workbench": str(path), "initialized": True}


def import_observations(path, data):
    """Replace the current observation set and reconcile review cases.

    The payload is the complete current observation set (never an
    incremental delta). Clustering failures raise before the workbench is
    read for mutation; the whole reconciliation commits through one atomic
    write. Re-importing the same effective state (observation order
    included) is a no-op that allocates no identities.
    """
    path = Path(path)
    _validate_import_input(data)
    # Reuse the existing clustering / maintenance-window rules. A failure
    # here raises before any state is mutated or written.
    report = cluster(data)
    state = _load(path)

    observations_by_id = {row["id"]: row for row in data["observations"]}
    new_events = {item["incident_id"]: item for item in report["incidents"]}
    new_sets = {iid: set(item["observation_ids"])
                for iid, item in new_events.items()}

    active = {cid: case for cid, case in state["cases"].items()
              if case["status"] == "active"}
    old_sets = {cid: set(case["observation_ids"])
                for cid, case in active.items()}

    # Direct links: old active incident and new incident share at least one
    # valid observation id. Retired cases never participate.
    successors_of = {cid: set() for cid in active}
    predecessors_of = {iid: set() for iid in new_events}
    for cid, old_ids in old_sets.items():
        for iid, new_ids in new_sets.items():
            if old_ids & new_ids:
                successors_of[cid].add(iid)
                predecessors_of[iid].add(cid)

    # A case_id continues only across a mutually unique link.
    continued = {}
    for iid, preds in predecessors_of.items():
        if len(preds) == 1:
            (pred,) = preds
            if successors_of[pred] == {iid}:
                continued[pred] = iid
    continued_iids = set(continued.values())

    import_seq = state["next_import"]
    changed = False
    receipt = {
        "changed": False,
        "import_seq": None,
        "observation_count": report["observation_count"],
        "suppressed_observation_ids": report["suppressed_observation_ids"],
        "incident_ids": sorted(new_events),
        "created": [],
        "continued": [],
        "rebased": [],
        "retired": [],
        "links": [],
        "todo": [],
    }

    # 1. One-to-one continuations. An unchanged basis keeps everything; a
    #    changed basis keeps case_id and owner but clears the conclusion.
    for cid in sorted(continued, key=lambda c: active[c]["seq"]):
        iid = continued[cid]
        case = state["cases"][cid]
        basis = _basis_for(new_events[iid], observations_by_id, data)
        if _basis_equal(case["basis"], basis):
            receipt["continued"].append({"case_id": cid, "incident_id": iid,
                                         "revision": case["revision"]})
            continue
        invalidated = case["conclusion"]
        case["basis"] = basis
        case["incident_id"] = iid
        case["observation_ids"] = list(new_events[iid]["observation_ids"])
        case["revision"] += 1
        case["conclusion"] = None
        _add_event(state, cid, "basis_changed", revision=case["revision"],
                   import_seq=import_seq, incident_id=iid,
                   basis=copy.deepcopy(basis),
                   invalidated_conclusion=copy.deepcopy(invalidated))
        changed = True
        receipt["rebased"].append({"case_id": cid, "incident_id": iid,
                                   "revision": case["revision"],
                                   "owner": case["owner"]})

    # 2. Creations: brand new incidents and every side of a merge, split, or
    #    cross-reorganization. New cases never inherit a conclusion.
    new_case_for_incident = {}
    for iid in sorted(new_events):
        if iid in continued_iids:
            continue
        event = new_events[iid]
        preds = sorted(predecessors_of[iid], key=lambda c: active[c]["seq"])
        pred_links = [{"case_id": pred,
                       "shared_observation_ids":
                           sorted(old_sets[pred] & new_sets[iid])}
                      for pred in preds]
        owner = None
        if preds:
            owners = {active[pred]["owner"] for pred in preds}
            if len(owners) == 1:
                (candidate,) = owners
                if candidate is not None:
                    owner = candidate
        seq = state["next_case"]
        cid = f"c{seq}"
        state["next_case"] += 1
        basis = _basis_for(event, observations_by_id, data)
        state["cases"][cid] = {
            "case_id": cid,
            "seq": seq,
            "status": "active",
            "revision": 1,
            "incident_id": iid,
            "observation_ids": list(event["observation_ids"]),
            "basis": basis,
            "owner": owner,
            "conclusion": None,
            "predecessors": pred_links,
            "successors": [],
            "created_import": import_seq,
            "retired_import": None,
        }
        _add_event(state, cid, "created", revision=1, import_seq=import_seq,
                   incident_id=iid, basis=copy.deepcopy(basis),
                   predecessors=copy.deepcopy(pred_links), owner=owner)
        new_case_for_incident[iid] = cid
        changed = True
        receipt["created"].append({
            "case_id": cid, "incident_id": iid, "revision": 1,
            "owner": owner, "predecessors": copy.deepcopy(pred_links)})
        for link in pred_links:
            receipt["links"].append({
                "predecessor": link["case_id"], "successor": cid,
                "shared_observation_ids": link["shared_observation_ids"]})

    # 3. Retirements: every previously active case without a one-to-one
    #    continuation. Retired cases keep their history but never reactivate.
    for cid in sorted(active, key=lambda c: active[c]["seq"]):
        if cid in continued:
            continue
        case = state["cases"][cid]
        succ_links = [{"case_id": new_case_for_incident[iid],
                       "shared_observation_ids":
                           sorted(old_sets[cid] & new_sets[iid])}
                      for iid in sorted(successors_of[cid])]
        case["status"] = "retired"
        case["revision"] += 1
        case["successors"] = succ_links
        case["retired_import"] = import_seq
        _add_event(state, cid, "retired", revision=case["revision"],
                   import_seq=import_seq,
                   successors=copy.deepcopy(succ_links))
        changed = True
        receipt["retired"].append({"case_id": cid,
                                   "revision": case["revision"],
                                   "successors": copy.deepcopy(succ_links)})

    if changed:
        state["next_import"] += 1
        receipt["changed"] = True
        receipt["import_seq"] = import_seq
        _atomic_write(path, state)
    receipt["todo"] = [case["case_id"] for _, case in _sorted_cases(state)
                       if case["status"] == "active"
                       and case["conclusion"] is None]
    return receipt


def current_cases(path):
    """List every active case with conclusion, owner, revision, and basis."""
    state = _load(Path(path))
    return [_case_view(case) for _, case in _sorted_cases(state)
            if case["status"] == "active"]


def todo_cases(path, owner=None, unassigned=False):
    """List active cases that still lack a current effective conclusion."""
    if owner is not None:
        _require_non_empty("owner", owner)
        if unassigned:
            raise ReviewError("owner filter cannot be combined with "
                              "the unassigned filter")
    result = []
    for view in current_cases(path):
        if not view["todo"]:
            continue
        if unassigned and view["owner"] is not None:
            continue
        if owner is not None and view["owner"] != owner:
            continue
        result.append(view)
    return result


def assign_owner(path, case_id, expected_revision, actor, owner, reason):
    """Assign (or reassign) the owner of an active, still-pending case.

    The expected revision is checked before anything else, so stale
    credentials never pass as a no-op. Re-setting the current owner with
    the matching revision is a harmless duplicate.
    """
    path = Path(path)
    _require_non_empty("actor", actor)
    _require_non_empty("owner", owner)
    _require_non_empty("reason", reason)
    state = _load(path)
    case = _find_case(state, case_id)
    _check_expected_revision(case, expected_revision)
    if case["status"] != "active":
        raise ReviewError(f"case {case_id!r} is retired")
    if case["conclusion"] is not None:
        raise ReviewError(f"case {case_id!r} already has a conclusion")
    if case["owner"] == owner:
        return {"case_id": case_id, "changed": False,
                "revision": case["revision"], "owner": owner}
    previous = case["owner"]
    case["owner"] = owner
    case["revision"] += 1
    _add_event(state, case_id, "owner_assigned", revision=case["revision"],
               actor=actor, reason=reason, from_owner=previous,
               to_owner=owner)
    _atomic_write(path, state)
    return {"case_id": case_id, "changed": True,
            "revision": case["revision"], "owner": owner}


def submit_conclusion(path, case_id, expected_revision, actor, verdict, note):
    """Record a human conclusion for an owned, active, still-pending case.

    Only the current owner may conclude, and only once per revision: the
    first success bumps the revision, so replays and opposite verdicts
    carrying the old revision are rejected without touching the audit log.
    """
    path = Path(path)
    _require_non_empty("actor", actor)
    if verdict not in VERDICTS:
        raise ReviewError(f"verdict must be one of {sorted(VERDICTS)}")
    _require_non_empty("note", note)
    state = _load(path)
    case = _find_case(state, case_id)
    _check_expected_revision(case, expected_revision)
    if case["status"] != "active":
        raise ReviewError(f"case {case_id!r} is retired")
    if case["conclusion"] is not None:
        raise ReviewError(f"case {case_id!r} already has a conclusion")
    if case["owner"] is None:
        raise ReviewError(f"case {case_id!r} is unassigned")
    if actor != case["owner"]:
        raise ReviewError(f"actor {actor!r} is not the current owner of "
                          f"case {case_id!r}")
    case["revision"] += 1
    event = _add_event(state, case_id, "conclusion",
                       revision=case["revision"], actor=actor,
                       verdict=verdict, note=note,
                       basis=copy.deepcopy(case["basis"]))
    case["conclusion"] = {"verdict": verdict, "note": note, "actor": actor,
                          "revision": case["revision"],
                          "event_seq": event["seq"]}
    _atomic_write(path, state)
    return {"case_id": case_id, "revision": case["revision"],
            "conclusion": copy.deepcopy(case["conclusion"])}


def case_history(path, case_id):
    """Return the full audit trail of one case, active or retired.

    Conclusion events carry the immutable basis they were recorded against;
    ``currently_effective`` marks the one conclusion (if any) that still
    governs the case. Predecessor/successor links stay queryable after
    retirement.
    """
    state = _load(Path(path))
    case = _find_case(state, case_id)
    effective_seq = None
    if case["status"] == "active" and case["conclusion"] is not None:
        effective_seq = case["conclusion"]["event_seq"]
    events = []
    for event in state["events"]:
        if event["case_id"] != case_id:
            continue
        entry = copy.deepcopy(event)
        if entry["kind"] == "conclusion":
            entry["currently_effective"] = entry["seq"] == effective_seq
        events.append(entry)
    return {
        "case_id": case_id,
        "status": case["status"],
        "revision": case["revision"],
        "owner": case["owner"],
        "incident_id": case["incident_id"],
        "observation_ids": list(case["observation_ids"]),
        "current_conclusion": (copy.deepcopy(case["conclusion"])
                               if case["status"] == "active" else None),
        "basis": copy.deepcopy(case["basis"]),
        "predecessors": copy.deepcopy(case["predecessors"]),
        "successors": copy.deepcopy(case["successors"]),
        "created_import": case["created_import"],
        "retired_import": case["retired_import"],
        "events": events,
    }


def _validate_import_input(data):
    if not isinstance(data, dict):
        raise ReviewError("import payload must be a JSON object")
    observations = data.get("observations")
    if not isinstance(observations, list):
        raise ReviewError("import payload requires an observations list")
    for row in observations:
        if not isinstance(row, dict):
            raise ReviewError("each observation must be a JSON object")
        _require_non_empty("observation id", row.get("id"))
        _require_non_empty("observation sensor", row.get("sensor"))


def _basis_for(event, observations_by_id, data):
    """The review basis: full incident report, valid members, parameters."""
    members = []
    for oid in event["observation_ids"]:
        row = observations_by_id[oid]
        members.append({field: row[field] for field in MEMBER_FIELDS})
    return {
        "incident": copy.deepcopy(event),
        "members": members,
        "radius_km": data["radius_km"],
        "window_minutes": data["window_minutes"],
    }


def _canon(value):
    """Canonical form for basis comparison.

    Object key order is ignored, numbers compare numerically, and strings
    compare as-is (timestamps are never normalized).
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", float(value))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("list", tuple(_canon(item) for item in value))
    if isinstance(value, dict):
        return ("dict", tuple(sorted((key, _canon(item))
                                     for key, item in value.items())))
    if value is None:
        return ("null",)
    raise ReviewError(f"unsupported value in review basis: {value!r}")


def _basis_equal(old, new):
    return _canon(old) == _canon(new)


def _case_view(case):
    return {
        "case_id": case["case_id"],
        "incident_id": case["incident_id"],
        "revision": case["revision"],
        "owner": case["owner"],
        "conclusion": copy.deepcopy(case["conclusion"]),
        "todo": case["conclusion"] is None,
        "basis": copy.deepcopy(case["basis"]),
    }


def _sorted_cases(state):
    return sorted(state["cases"].items(), key=lambda item: item[1]["seq"])


def _add_event(state, case_id, kind, **fields):
    seq = state["next_event"]
    state["next_event"] += 1
    event = {"seq": seq, "case_id": case_id, "kind": kind}
    event.update(fields)
    state["events"].append(event)
    return event


def _require_non_empty(label, value):
    if not isinstance(value, str) or not value:
        raise ReviewError(f"{label} must be a non-empty string")


def _find_case(state, case_id):
    _require_non_empty("case_id", case_id)
    case = state["cases"].get(case_id)
    if case is None:
        raise ReviewError(f"unknown case: {case_id!r}")
    return case


def _check_expected_revision(case, expected_revision):
    if isinstance(expected_revision, bool) or \
            not isinstance(expected_revision, int):
        raise ReviewError("expected_revision must be an integer")
    if expected_revision != case["revision"]:
        raise ReviewError(
            f"stale revision for case {case['case_id']}: "
            f"expected {expected_revision}, current {case['revision']}")


def _load(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReviewError(f"workbench not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ReviewError(f"workbench is not valid JSON: {exc}") from None
    if not isinstance(state, dict) or \
            state.get("schema_version") != SCHEMA_VERSION:
        raise ReviewError("unrecognized workbench format")
    return state


def _atomic_write(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent,
                                    prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True,
                      ensure_ascii=False)
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
