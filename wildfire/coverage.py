"""One-shot sampling coverage audit for candidate fire incidents.

:func:`wildfire.core.cluster` can only group observations that were actually
received; a quiet interval is indistinguishable from a sensor that went
silent. This module audits what the sensor fleet *should* have sampled so
each candidate incident carries an explainable evidence-completeness
verdict. It never changes clustering: the existing cluster report, incident
membership, ids, confidence, mean severity and maintenance-window
suppression are reused verbatim.

Sampling plans register, per ``sensor_id``, an inclusive ``active_start``,
an exclusive ``active_end``, a positive integer ``cadence_seconds`` and a
``phase`` timestamp. Scheduled slots are ``phase + k * cadence_seconds``
for every integer ``k``; only slots falling in both the audit window
``[start, end)`` and the deployment interval are counted. All timestamps
carry a UTC offset and are compared as the instants they denote, so ``Z``
and ``+00:00`` never produce different slots.

A slot is covered by at most one observation: the sensor must match
exactly, the time distance must be within ``tolerance_seconds``, one
observation can never cover two slots, and duplicate readings for one slot
still count once. The audit refuses to run unless ``tolerance_seconds`` is
strictly below half of every planned cadence, which also guarantees each
reading is eligible for at most one slot; any residual tie is resolved by
slot UTC time and then observation id for a deterministic result. Slots
inside a maintenance window ``[start, end)`` are planned outages: they are
excluded from the denominator, and maintenance-suppressed readings can
never count as samples. Valid observations that match no scheduled slot
are reported separately as ``off_schedule`` and never inflate coverage.

Incident coverage reuses the exact same rules on the intersection of the
audit window with each incident's half-open event range
``[first_member_time, last_member_time + 1 microsecond)``; a single-moment
incident therefore still includes that moment's slots.
"""

from datetime import timedelta, timezone

from .core import _time, _windows, cluster

_MICROSECONDS_PER_SECOND = 1_000_000
_MICROSECOND = timedelta(microseconds=1)


class CoverageError(ValueError):
    """Raised when an audit input is invalid or violates an audit precondition."""


def audit_coverage(data, schedule, *, start, end,
                   tolerance_seconds, minimum_coverage):
    """Audit sampling coverage and attach verdicts to clustered incidents.

    ``data`` is the full observation JSON payload accepted by
    :func:`wildfire.core.cluster`; ``schedule`` is the sampling-plan JSON
    document. Raises :class:`CoverageError` (a ``ValueError``) for any
    invalid timestamp, plan, unplanned observation sensor, or violated
    half-cadence tolerance precondition before producing a report.
    """
    report = cluster(data)
    windows = _merge_windows(_windows(data))

    audit_start = _require_offset_time(start, "audit start")
    audit_end = _require_offset_time(end, "audit end")
    if audit_start >= audit_end:
        raise CoverageError("audit start must be before end")

    if isinstance(tolerance_seconds, bool) or not isinstance(tolerance_seconds, int):
        raise CoverageError("tolerance_seconds must be an integer")
    if tolerance_seconds < 0:
        raise CoverageError("tolerance_seconds must be >= 0")
    if isinstance(minimum_coverage, bool) or not isinstance(minimum_coverage, (int, float)):
        raise CoverageError("minimum_coverage must be a number")
    minimum_coverage = float(minimum_coverage)
    if not 0.0 <= minimum_coverage <= 1.0:
        raise CoverageError("minimum_coverage must be within [0, 1]")

    plans = _parse_schedule(schedule)
    tolerance_us = tolerance_seconds * _MICROSECONDS_PER_SECOND
    for plan in plans:
        if 2 * tolerance_seconds >= plan["cadence_seconds"]:
            raise CoverageError(
                f"tolerance_seconds must be less than half the cadence of "
                f"sensor {plan['sensor_id']!r} "
                f"(cadence_seconds={plan['cadence_seconds']})")

    planned_sensors = {plan["sensor_id"] for plan in plans}
    observations = []
    for row in data["observations"]:
        if row["sensor"] not in planned_sensors:
            raise CoverageError(
                f"observation {row['id']!r} has no sampling plan for sensor "
                f"{row['sensor']!r}")
        observations.append({
            "id": row["id"],
            "sensor": row["sensor"],
            "time": _time(row["time"]),
            "suppressed": _in_maintenance(windows, row["sensor"], _time(row["time"])),
        })

    global_slots, global_covered, used_observation_ids = _audit_scope(
        plans, windows, observations, tolerance_us, audit_start, audit_end)

    sensors = []
    off_schedule = []
    totals = {"expected_slots": 0, "planned_outage_slots": 0,
              "covered_slots": 0, "missing_slots": 0}
    for plan in plans:
        sensor = plan["sensor_id"]
        slots = global_slots[sensor]
        covered = global_covered[sensor]
        entry = _sensor_entry(sensor, slots, covered)
        sensors.append(entry)
        for key in totals:
            totals[key] += entry[key]
        sensor_obs = {row["id"] for row in observations
                      if row["sensor"] == sensor and not row["suppressed"]}
        entry["off_schedule_observation_ids"] = sorted(
            sensor_obs - used_observation_ids)
        off_schedule.extend(
            {"observation_id": row["id"], "sensor_id": sensor,
             "time": _utc_iso(row["time"])}
            for row in observations
            if row["sensor"] == sensor and not row["suppressed"]
            and row["id"] not in used_observation_ids)
    off_schedule.sort(key=lambda row: row["observation_id"])

    eligible = totals["expected_slots"] - totals["planned_outage_slots"]
    coverage = {
        **totals,
        "eligible_slots": eligible,
        "coverage_ratio": _ratio(totals["covered_slots"], eligible),
        "off_schedule_observation_count": len(off_schedule),
    }

    obs_index = {row["id"]: row for row in observations}
    incidents = []
    for incident in report["incidents"]:
        member_times = [obs_index[member_id]["time"]
                        for member_id in incident["observation_ids"]]
        first = min(member_times)
        last = max(member_times)
        event_end = last + _MICROSECOND
        incidents.append({
            **incident,
            "event_window": {"start": _utc_iso(first), "end": _utc_iso(event_end)},
            "sampling_coverage": _incident_coverage(
                plans, windows, observations, tolerance_us,
                audit_start, audit_end, first, event_end, minimum_coverage),
        })

    return {
        "audit_window": {"start": _utc_iso(audit_start),
                         "end": _utc_iso(audit_end)},
        "tolerance_seconds": tolerance_seconds,
        "minimum_coverage": minimum_coverage,
        "coverage": coverage,
        "sensors": sensors,
        "off_schedule_observations": off_schedule,
        "cluster_report": report,
        "incidents": incidents,
    }


def _parse_schedule(schedule):
    if not isinstance(schedule, dict):
        raise CoverageError("sampling schedule must be a JSON object")
    rows = schedule.get("sensors")
    if not isinstance(rows, list):
        raise CoverageError("sampling schedule must contain a 'sensors' list")
    seen = set()
    plans = []
    for index, entry in enumerate(rows):
        where = f"sampling plan entry {index}"
        if not isinstance(entry, dict):
            raise CoverageError(f"{where} must be a JSON object")
        for field in ("sensor_id", "active_start", "active_end",
                      "cadence_seconds", "phase"):
            if field not in entry:
                raise CoverageError(f"{where} missing field {field!r}")
        sensor = entry["sensor_id"]
        if not isinstance(sensor, str) or not sensor:
            raise CoverageError(f"{where}: sensor_id must be a non-empty string")
        if sensor in seen:
            raise CoverageError(f"duplicate sampling plan for sensor {sensor!r}")
        seen.add(sensor)
        active_start = _require_offset_time(entry["active_start"],
                                            f"{where} active_start")
        active_end = _require_offset_time(entry["active_end"],
                                          f"{where} active_end")
        if active_start >= active_end:
            raise CoverageError(
                f"{where}: active_start must be before active_end")
        cadence = entry["cadence_seconds"]
        if isinstance(cadence, bool) or not isinstance(cadence, int):
            raise CoverageError(f"{where}: cadence_seconds must be an integer")
        if cadence <= 0:
            raise CoverageError(
                f"{where}: cadence_seconds must be a positive integer")
        phase = _require_offset_time(entry["phase"], f"{where} phase")
        plans.append({"sensor_id": sensor, "active_start": active_start,
                      "active_end": active_end, "cadence_seconds": cadence,
                      "phase": phase})
    plans.sort(key=lambda plan: plan["sensor_id"])
    return plans


def _audit_scope(plans, windows, observations, tolerance_us, lo, hi):
    """Return slots, coverings and used observation ids for one time scope."""
    slots_by_sensor = {}
    for plan in plans:
        slots_by_sensor[plan["sensor_id"]] = [
            {"time": slot_time,
             "planned_outage": _in_maintenance(windows, plan["sensor_id"], slot_time)}
            for slot_time in _planned_slots(plan, lo, hi)]

    edges = []
    for row in observations:
        if row["suppressed"]:
            continue
        for slot in slots_by_sensor.get(row["sensor"], ()):
            if slot["planned_outage"]:
                continue
            delta = abs(_microseconds(row["time"] - slot["time"]))
            if delta <= tolerance_us:
                # Sort key enforces the deterministic tie contract: smallest
                # total deviation, then slot UTC instant, then observation id.
                edges.append((delta, slot["time"], row["id"], row["sensor"]))
    edges.sort(key=lambda edge: (edge[0], edge[1], edge[2]))

    taken_slots = set()
    used_observations = set()
    covered_by_sensor = {plan["sensor_id"]: {} for plan in plans}
    for _, slot_time, observation_id, sensor in edges:
        if observation_id in used_observations:
            continue
        slot_key = (sensor, slot_time)
        if slot_key in taken_slots:
            continue
        taken_slots.add(slot_key)
        used_observations.add(observation_id)
        covered_by_sensor[sensor][slot_time] = observation_id
    return slots_by_sensor, covered_by_sensor, used_observations


def _planned_slots(plan, lo, hi):
    span_lo = max(lo, plan["active_start"])
    span_hi = min(hi, plan["active_end"])
    if span_lo >= span_hi:
        return []
    cadence_us = plan["cadence_seconds"] * _MICROSECONDS_PER_SECOND
    phase = plan["phase"]
    k_min = _ceil_div(_microseconds(span_lo - phase), cadence_us)
    k_limit = _ceil_div(_microseconds(span_hi - phase), cadence_us)
    slots = []
    k = k_min
    while k < k_limit:
        slot_time = phase + timedelta(microseconds=k * cadence_us)
        if slot_time >= span_hi:
            break
        slots.append(slot_time)
        k += 1
    return slots


def _sensor_entry(sensor, slots, covered):
    outage = sum(1 for slot in slots if slot["planned_outage"])
    covered_count = len(covered)
    expected = len(slots)
    eligible = expected - outage
    details = []
    for slot in sorted(slots, key=lambda item: item["time"]):
        if slot["planned_outage"]:
            status, observation_id = "planned_outage", None
        elif slot["time"] in covered:
            status, observation_id = "covered", covered[slot["time"]]
        else:
            status, observation_id = "missing", None
        details.append({"slot_time": _utc_iso(slot["time"]),
                        "status": status,
                        "observation_id": observation_id})
    return {
        "sensor_id": sensor,
        "expected_slots": expected,
        "planned_outage_slots": outage,
        "eligible_slots": eligible,
        "covered_slots": covered_count,
        "missing_slots": eligible - covered_count,
        "coverage_ratio": _ratio(covered_count, eligible),
        "off_schedule_observation_ids": [],
        "slots": details,
    }


def _incident_coverage(plans, windows, observations, tolerance_us,
                       audit_start, audit_end, first, event_end,
                       minimum_coverage):
    lo, hi = max(audit_start, first), min(audit_end, event_end)
    if lo >= hi:
        eligible = covered = outage = 0
        missing_details = []
    else:
        slots_by_sensor, covered_by_sensor, _ = _audit_scope(
            plans, windows, observations, tolerance_us, lo, hi)
        outage = sum(
            1 for slots in slots_by_sensor.values()
            for slot in slots if slot["planned_outage"])
        covered = sum(len(covered) for covered in covered_by_sensor.values())
        expected = sum(len(slots) for slots in slots_by_sensor.values())
        eligible = expected - outage
        missing_details = [
            {"sensor_id": plan["sensor_id"], "slot_time": _utc_iso(slot["time"])}
            for plan in plans
            for slot in slots_by_sensor[plan["sensor_id"]]
            if not slot["planned_outage"]
            and slot["time"] not in covered_by_sensor[plan["sensor_id"]]]
        missing_details.sort(key=lambda row: (row["slot_time"], row["sensor_id"]))
    if eligible == 0:
        status, ratio = "no_expected_samples", None
    else:
        ratio = _ratio(covered, eligible)
        status = "incomplete" if covered / eligible < minimum_coverage else "sufficient"
    return {
        "status": status,
        "expected_slots": eligible + outage,
        "planned_outage_slots": outage,
        "eligible_slots": eligible,
        "covered_slots": covered,
        "missing_slots": eligible - covered,
        "coverage_ratio": ratio,
        "minimum_coverage": minimum_coverage,
        "missing_slot_details": missing_details,
    }


def _merge_windows(raw_windows):
    grouped = {}
    for sensor, start, end in raw_windows:
        grouped.setdefault(sensor, []).append((start, end))
    merged = {}
    for sensor, intervals in grouped.items():
        intervals.sort()
        union = []
        for start, end in intervals:
            if union and start <= union[-1][1]:
                if end > union[-1][1]:
                    union[-1] = (union[-1][0], end)
            else:
                union.append((start, end))
        merged[sensor] = union
    return merged


def _in_maintenance(merged_windows, sensor, moment):
    return any(start <= moment < end
               for start, end in merged_windows.get(sensor, ()))


def _require_offset_time(value, label):
    if not isinstance(value, str):
        raise CoverageError(f"{label} must be an ISO 8601 timestamp string")
    try:
        return _time(value)
    except ValueError as exc:
        raise CoverageError(f"{label}: {exc}") from None


def _microseconds(delta):
    return delta // _MICROSECOND


def _ceil_div(numerator, denominator):
    return -((-numerator) // denominator)


def _ratio(covered, eligible):
    if eligible == 0:
        return None
    return round(covered / eligible, 6)


def _utc_iso(moment):
    return moment.astimezone(timezone.utc).isoformat()
