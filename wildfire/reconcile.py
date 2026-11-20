"""Offline reconciliation of primary and backup gateway exports.

The two gateways receive the same physical sensor samples, but either export
may drop rows and each numbers its own rows. The backup gateway's clock may
also have been stepped: inside the continuous clock section covered by one
reconciliation run every sensor shares one unknown fixed integer offset, and
the corrected backup time is ``clock_ms + offset``.

Anchor records constrain that offset to the integer intersection of
``[utc_min_ms - clock_ms, utc_max_ms - clock_ms]`` over every anchor. A pair
of rows can explain the same physical sample only when sensor, coordinates and
severity are identical and the corrected times differ by at most
``tolerance_ms``. Pairing is a one-to-one bipartite matching; the objective is
the global maximum number of duplicate pairs over *every* anchor-allowed
offset and *every* matching at that offset.

The edge set of the pairing graph changes only at finite breakpoints (each
candidate pair is active on one closed offset interval), so the allowed offset
range is split into at most ``O(E)`` constant-graph segments. Work is
proportional to the number of segments, never to the millisecond width of the
range: an hours-wide anchor range whose pairing conditions never change is a
single segment.

Only backup rows that are unpaired in *every* optimal solution and whose
corrected UTC is identical across those solutions (the optimal offset set is a
single integer) are safe to replay. Everything else stays shelved with an
explicit reason. The produced payload keeps every primary row verbatim and is
directly usable as ``fire-cluster --input`` input.
"""

import sys
from collections import deque
from datetime import datetime, timezone
from math import isfinite

from .core import cluster

_MAX_RECURSION = 1_000_000


class ReconciliationError(ValueError):
    """Raised when reconciliation input is invalid or has no solution."""


def reconcile(data):
    """Reconcile a primary/backup payload into a replayable clustering input.

    Raises :class:`ReconciliationError` for any validation failure, anchor
    conflict, or missing common offset before any output is produced. The
    returned mapping carries the clustering parameters, the merged
    ``observations`` list, and a ``reconciliation`` detail block.
    """
    if not isinstance(data, dict):
        raise ReconciliationError("reconciliation input must be a JSON object")
    tolerance = _required_integer(data, "tolerance_ms", minimum=0)
    radius = _required_number(data, "radius_km", positive=True)
    window = _required_integer(data, "window_minutes", minimum=0)
    primary = _validate_rows(data, "primary", "utc_ms")
    backup = _validate_rows(data, "backup", "clock_ms")
    offset_min, offset_max = _anchor_range(data.get("anchors"))

    candidate_edges = _candidate_edges(primary, backup, offset_min,
                                       offset_max, tolerance)
    segments = _offset_segments(candidate_edges, offset_min, offset_max)

    # Sweep edges over the segment boundaries so work stays proportional to
    # the number of candidate edges and segments, not their millisecond span.
    starts = sorted((edge[2], edge) for edge in candidate_edges)
    ends = sorted((edge[3] + 1, edge) for edge in candidate_edges)
    analyses = []
    active, start_cursor, end_cursor = set(), 0, 0
    for start, end in segments:
        while start_cursor < len(starts) and starts[start_cursor][0] <= start:
            active.add(starts[start_cursor][1])
            start_cursor += 1
        while end_cursor < len(ends) and ends[end_cursor][0] <= start:
            active.discard(ends[end_cursor][1])
            end_cursor += 1
        analyses.append((start, end,
                         _analyze_graph(active, primary, backup)))

    max_pairs = max((len(analysis[0]) for _, _, analysis in analyses),
                    default=0)
    optimal_intervals = []
    for start, end, analysis in analyses:
        if len(analysis[0]) != max_pairs:
            continue
        if optimal_intervals and optimal_intervals[-1]["max"] + 1 == start:
            optimal_intervals[-1]["max"] = end
        else:
            optimal_intervals.append({"min": start, "max": end})

    possible_matches = {row["id"]: set() for row in backup}
    free_capable = set()
    for _, _, analysis in analyses:
        matched, allowed, freeable = analysis
        if len(matched) != max_pairs:
            continue
        for backup_id, primary_id in allowed:
            possible_matches[backup_id].add(primary_id)
        free_capable.update(freeable)

    optimal_min = optimal_intervals[0]["min"]
    optimal_max = optimal_intervals[-1]["max"]
    offset_fixed = optimal_min == optimal_max
    fixed_offset = optimal_min if offset_fixed else None

    backup_reports, included_backup = _backup_reports(
        backup, possible_matches, free_capable,
        optimal_min, optimal_max, offset_fixed, fixed_offset)

    observations = [_primary_observation(row) for row in primary]
    observations.extend(_backup_observation(row, fixed_offset)
                        for row in backup if row["id"] in included_backup)
    observations.sort(key=lambda row: row["id"])

    payload = {
        "radius_km": radius,
        "window_minutes": window,
        "observations": observations,
        "reconciliation": {
            "tolerance_ms": tolerance,
            "anchor_offset_range": {"min": offset_min, "max": offset_max},
            "max_duplicate_pairs": max_pairs,
            "optimal_offset_intervals": optimal_intervals,
            "backup_rows": backup_reports,
        },
    }
    if "maintenance_windows" in data:
        payload["maintenance_windows"] = data["maintenance_windows"]

    # Fail loudly rather than handing the downstream command a half-valid file.
    cluster(payload)
    return payload


def _validate_rows(data, gateway, time_field):
    rows = data.get(gateway)
    if not isinstance(rows, list):
        raise ReconciliationError(f"{gateway} export must be a list")
    seen = set()
    cleaned = []
    for index, row in enumerate(rows):
        where = f"{gateway} row {index}"
        if not isinstance(row, dict):
            raise ReconciliationError(f"{where} must be a JSON object")
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise ReconciliationError(
                f"{where}: id must be a non-empty string")
        if row_id in seen:
            raise ReconciliationError(
                f"duplicate {gateway} row id: {row_id!r}")
        seen.add(row_id)
        sensor = row.get("sensor")
        if not isinstance(sensor, str) or not sensor:
            raise ReconciliationError(
                f"{where} ({row_id!r}): sensor must be a non-empty string")
        lat = _row_number(row, "lat", where, row_id)
        lon = _row_number(row, "lon", where, row_id)
        severity = _row_number(row, "severity", where, row_id)
        if not -90 <= lat <= 90:
            raise ReconciliationError(
                f"{where} ({row_id!r}): lat must be within [-90, 90]")
        if not -180 <= lon <= 180:
            raise ReconciliationError(
                f"{where} ({row_id!r}): lon must be within [-180, 180]")
        if not 0 <= severity <= 1:
            raise ReconciliationError(
                f"{where} ({row_id!r}): severity must be within [0, 1]")
        clock = _row_integer(row, time_field, where, row_id)
        cleaned.append({"id": row_id, "sensor": sensor, "lat": lat,
                        "lon": lon, "severity": severity,
                        time_field: clock})
    return cleaned


def _anchor_range(anchors):
    if not isinstance(anchors, list) or not anchors:
        raise ReconciliationError(
            "anchors must be a non-empty list of timing records")
    offset_min = None
    offset_max = None
    for index, anchor in enumerate(anchors):
        where = f"anchor {index}"
        if not isinstance(anchor, dict):
            raise ReconciliationError(f"{where} must be a JSON object")
        clock = _row_integer(anchor, "clock_ms", where, None)
        utc_min = _row_integer(anchor, "utc_min_ms", where, None)
        utc_max = _row_integer(anchor, "utc_max_ms", where, None)
        if utc_min > utc_max:
            raise ReconciliationError(
                f"{where}: inverted UTC interval [{utc_min}, {utc_max}]")
        low = utc_min - clock
        high = utc_max - clock
        offset_min = low if offset_min is None else max(offset_min, low)
        offset_max = high if offset_max is None else min(offset_max, high)
    if offset_min > offset_max:
        raise ReconciliationError(
            f"no single offset satisfies every anchor: "
            f"intersection [{offset_min}, {offset_max}] is empty")
    return offset_min, offset_max


def _candidate_edges(primary, backup, offset_min, offset_max, tolerance):
    groups = {}
    for row in primary:
        key = _measurement_key(row)
        groups.setdefault(key, {"primary": [], "backup": []})["primary"].append(row)
    for row in backup:
        key = _measurement_key(row)
        groups.setdefault(key, {"primary": [], "backup": []})["backup"].append(row)
    edges = []
    for group in groups.values():
        for p_row in group["primary"]:
            for b_row in group["backup"]:
                center = p_row["utc_ms"] - b_row["clock_ms"]
                low = max(offset_min, center - tolerance)
                high = min(offset_max, center + tolerance)
                if low <= high:
                    edges.append((b_row["id"], p_row["id"], low, high))
    edges.sort(key=lambda edge: (edge[0], edge[1], edge[2], edge[3]))
    return edges


def _offset_segments(edges, offset_min, offset_max):
    boundaries = {offset_min, offset_max + 1}
    for _, _, low, high in edges:
        boundaries.add(low)
        boundaries.add(high + 1)
    points = sorted(boundaries)
    return [(points[i], points[i + 1] - 1)
            for i in range(len(points) - 1)
            if points[i] <= points[i + 1] - 1]


def _analyze_graph(active_edges, primary, backup):
    """Return (matched pairs, edges in some max matching, free-capable backups).

    A maximum matching is found first. The directed alternating graph then
    classifies every edge: unmatched arc ``backup -> primary``, matched arc
    ``primary -> backup``. Using the Dulmage-Mendelsohn decomposition, an
    unmatched arc belongs to some maximum matching when it starts at a vertex
    reachable from a free backup, ends at a vertex that can reach a free
    primary, or lies on a directed cycle (same SCC). A backup vertex can be
    left free by some maximum matching exactly when it is reachable from a
    free backup in that directed graph.
    """
    primary_ids = sorted(row["id"] for row in primary)
    backup_ids = sorted(row["id"] for row in backup)
    adjacency = {backup_id: [] for backup_id in backup_ids}
    pair_set = set()
    for backup_id, primary_id, _, _ in active_edges:
        adjacency[backup_id].append(primary_id)
        pair_set.add((backup_id, primary_id))
    for neighbors in adjacency.values():
        neighbors.sort()

    match_primary = _maximum_matching(adjacency, backup_ids)
    matched = {(backup_id, primary_id)
               for primary_id, backup_id in match_primary.items()}

    left = {backup_id: f"L:{backup_id}" for backup_id in backup_ids}
    right = {primary_id: f"R:{primary_id}" for primary_id in primary_ids}
    directed = {vertex: [] for vertex in list(left.values())
                + list(right.values())}
    for backup_id, primary_id in pair_set:
        l_vertex, r_vertex = left[backup_id], right[primary_id]
        if (backup_id, primary_id) in matched:
            directed[r_vertex].append(l_vertex)
        else:
            directed[l_vertex].append(r_vertex)
    for neighbors in directed.values():
        neighbors.sort()
    reversed_graph = {vertex: [] for vertex in directed}
    for vertex, neighbors in directed.items():
        for neighbor in neighbors:
            reversed_graph[neighbor].append(vertex)

    free_left = [left[backup_id] for backup_id in backup_ids
                 if backup_id not in match_primary.values()]
    free_right = [right[primary_id] for primary_id in primary_ids
                  if primary_id not in match_primary]
    # Dulmage-Mendelsohn characterization. An unmatched arc L->R belongs to
    # some maximum matching when it starts at a vertex reachable from a free
    # backup (alternating path from a free L), its right end can reach a free
    # primary (alternating path ending at a free R), or its ends share an SCC
    # (alternating cycle). A backup can be left free by some maximum matching
    # exactly when it is reachable from a free backup.
    from_free_left = _bfs(free_left, directed)
    to_free_right = _bfs(free_right, reversed_graph)
    components = _strong_components(directed)

    allowed = set(matched)
    for backup_id, primary_id in pair_set - matched:
        l_vertex, r_vertex = left[backup_id], right[primary_id]
        if (l_vertex in from_free_left
                or r_vertex in to_free_right
                or components[l_vertex] == components[r_vertex]):
            allowed.add((backup_id, primary_id))
    freeable = {backup_id for backup_id in backup_ids
                if left[backup_id] in from_free_left}
    return matched, allowed, freeable


def _maximum_matching(adjacency, backup_ids):
    match_primary = {}

    def augment(backup_id, seen):
        for primary_id in adjacency.get(backup_id, ()):
            if primary_id in seen:
                continue
            seen.add(primary_id)
            occupant = match_primary.get(primary_id)
            if occupant is None or augment(occupant, seen):
                match_primary[primary_id] = backup_id
                return True
        return False

    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous_limit, _MAX_RECURSION))
    try:
        for backup_id in backup_ids:
            augment(backup_id, set())
    finally:
        sys.setrecursionlimit(previous_limit)
    return match_primary


def _strong_components(directed):
    visited = set()
    finish_order = []
    for start in sorted(directed):
        if start in visited:
            continue
        stack = [(start, 0)]
        while stack:
            vertex, index = stack[-1]
            if index == 0:
                visited.add(vertex)
            neighbors = directed[vertex]
            if index < len(neighbors):
                stack[-1] = (vertex, index + 1)
                neighbor = neighbors[index]
                if neighbor not in visited:
                    stack.append((neighbor, 0))
            else:
                finish_order.append(vertex)
                stack.pop()

    reversed_graph = {vertex: [] for vertex in directed}
    for vertex, neighbors in directed.items():
        for neighbor in neighbors:
            reversed_graph[neighbor].append(vertex)
    components = {}
    component_id = 0
    for start in reversed(finish_order):
        if start in components:
            continue
        components[start] = component_id
        stack = [start]
        while stack:
            vertex = stack.pop()
            for neighbor in reversed_graph[vertex]:
                if neighbor not in components:
                    components[neighbor] = component_id
                    stack.append(neighbor)
        component_id += 1
    return components


def _bfs(starts, graph):
    seen = set(starts)
    queue = deque(starts)
    while queue:
        vertex = queue.popleft()
        for neighbor in graph.get(vertex, ()):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return seen


def _backup_reports(backup, possible_matches, free_capable,
                    optimal_min, optimal_max, offset_fixed, fixed_offset):
    reports = []
    included = set()
    for row in sorted(backup, key=lambda item: item["id"]):
        backup_id = row["id"]
        candidates = sorted(possible_matches[backup_id])
        can_be_unpaired = backup_id in free_capable
        if candidates:
            if can_be_unpaired:
                decision = "shelved"
                reason_code = "duplicate_in_some_optimal_solutions"
                reason = ("a duplicate exists in some optimal solutions but "
                          "another optimal solution leaves this row unpaired")
            elif len(candidates) == 1:
                decision = "shelved"
                reason_code = "duplicate_always_same_primary"
                reason = ("duplicate of primary row "
                          f"{candidates[0]!r} in every optimal solution; the "
                          "primary export already carries this sample")
            else:
                decision = "shelved"
                reason_code = "duplicate_primary_ambiguous"
                reason = ("duplicate of one of several primary rows depending "
                          "on the optimal solution")
        elif offset_fixed:
            decision = "included"
            reason_code = "included_no_duplicate_utc_fixed"
            reason = ("unpaired in every optimal solution and the offset is "
                      "fixed; added with its corrected UTC")
            included.add(backup_id)
        else:
            decision = "shelved"
            reason_code = "no_duplicate_utc_undetermined"
            reason = ("unpaired in every optimal solution but corrected UTC "
                      "differs across the still-allowed optimal offsets")

        report = {
            "id": backup_id,
            "possible_primary_ids": candidates,
            "can_be_unpaired_in_optimal_solution": can_be_unpaired,
            "decision": decision,
            "reason_code": reason_code,
            "reason": reason,
        }
        if offset_fixed:
            report["corrected_utc_ms"] = row["clock_ms"] + fixed_offset
        else:
            report["possible_corrected_utc_ms"] = {
                "min": row["clock_ms"] + optimal_min,
                "max": row["clock_ms"] + optimal_max,
            }
        reports.append(report)
    return reports, included


def _primary_observation(row):
    return {
        "id": f"primary:{row['id']}",
        "sensor": row["sensor"],
        "time": _to_iso(row["utc_ms"]),
        "lat": row["lat"],
        "lon": row["lon"],
        "severity": row["severity"],
    }


def _backup_observation(row, fixed_offset):
    return {
        "id": f"backup:{row['id']}",
        "sensor": row["sensor"],
        "time": _to_iso(row["clock_ms"] + fixed_offset),
        "lat": row["lat"],
        "lon": row["lon"],
        "severity": row["severity"],
    }


def _to_iso(epoch_ms):
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds")


def _measurement_key(row):
    return (row["sensor"], row["lat"], row["lon"], row["severity"])


def _required_integer(data, field, minimum):
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReconciliationError(f"{field} must be an integer")
    if value < minimum:
        raise ReconciliationError(f"{field} must be >= {minimum}")
    return value


def _required_number(data, field, positive):
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not isfinite(value):
        raise ReconciliationError(f"{field} must be a finite number")
    if positive and value <= 0:
        raise ReconciliationError(f"{field} must be positive")
    return value


def _row_integer(row, field, where, row_id):
    if field not in row:
        suffix = f" ({row_id!r})" if row_id is not None else ""
        raise ReconciliationError(f"{where}{suffix}: missing field {field!r}")
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int):
        suffix = f" ({row_id!r})" if row_id is not None else ""
        raise ReconciliationError(f"{where}{suffix}: {field} must be an integer")
    return value


def _row_number(row, field, where, row_id):
    if field not in row:
        raise ReconciliationError(
            f"{where} ({row_id!r}): missing field {field!r}")
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not isfinite(value):
        raise ReconciliationError(
            f"{where} ({row_id!r}): {field} must be a finite number")
    return value
