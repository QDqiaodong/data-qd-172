"""Offline location-uncertainty audit for candidate fire incidents.

Duty officers receive wildfire sensor observations whose coordinates carry
a measurement error. :func:`wildfire.core.cluster` groups observations by
their *reported* coordinates, so when coordinate accuracy is low an
incident may hinge on links that only exist inside the error margin. This
module audits that robustness without changing clustering: the existing
cluster report, incident membership, ids, confidence, mean severity,
maintenance-window suppression and the legacy command behavior are reused
verbatim.

Each observation may carry an optional ``location_uncertainty_m`` radius
(meters): the true position is guaranteed to lie within that distance of
the reported coordinates. The field defaults to 0 and must be a finite,
non-negative number; any other value is rejected. Maintenance-suppressed
observations (the existing ``cluster()`` rule) take no part in the audit.

For every pair of unsuppressed observations whose original timestamps
differ by at most ``window_minutes`` (the existing time-window rule), let
``d`` be the great-circle distance between the reported coordinates (the
same haversine used by clustering, Earth radius 6371.0088 km), ``u`` the
sum of the two uncertainty radii expressed in km, and ``radius_km`` the
existing clustering radius:

* ``d + u <= radius_km`` — the link is **certain**: it holds under every
  combination of true positions allowed by the error radii;
* ``d <= radius_km + u`` — the link is **possible**: at least one allowed
  combination of true positions brings the pair within the radius;
* otherwise the link cannot exist under any allowed positions.

Both boundaries are inclusive. Because every certain link holds for all
allowed positions at once, the connected components of the certain graph
stay linked by certain paths no matter where the true positions fall
inside their error radii. The possible graph adds every individually
feasible link, so its components are a conservative outer envelope:
membership in one possible component only means a connecting chain cannot
be ruled out — it does *not* guarantee that all links of such a chain are
simultaneously realizable by one choice of true positions. Two
observations in different possible components, however, have no allowed
direct or indirect connection path at all.

Possible components are not precise, realizable clusters and never
replace the incident report. Since certain links are a subset of the
reported-coordinate incident links, which are a subset of the possible
links, every certain component lies inside one incident and every
incident lies inside one possible component. Component ids are the
lexicographically smallest member observation id (the same convention as
``incident_id``), and all lists are sorted, so reordering the input
cannot change the output.
"""

import math

from .core import _km, _time, cluster


class UncertaintyError(ValueError):
    """Raised when a location_uncertainty_m value is invalid."""


def audit_location(data):
    """Audit location uncertainty over the full observation payload.

    ``data`` is the full observation JSON payload accepted by
    :func:`wildfire.core.cluster`; observations may additionally carry
    ``location_uncertainty_m``. Raises :class:`UncertaintyError` (a
    ``ValueError``) for any non-numeric, non-finite or negative
    uncertainty value before producing a report.
    """
    report = cluster(data)
    radius = float(data["radius_km"])
    window = int(data["window_minutes"])

    uncertainty = {}
    for row in data["observations"]:
        uncertainty[row["id"]] = _uncertainty_m(row)

    suppressed = set(report["suppressed_observation_ids"])
    active = sorted((row for row in data["observations"]
                     if row["id"] not in suppressed),
                    key=lambda row: row["id"])
    times = {row["id"]: _time(row["time"]) for row in active}

    certain_parent = {row["id"]: row["id"] for row in active}
    possible_parent = {row["id"]: row["id"] for row in active}
    for i, a in enumerate(active):
        for b in active[:i]:
            delta = (times[a["id"]] - times[b["id"]]).total_seconds()
            if abs(delta) > window * 60:
                continue
            d = _km(a, b)
            u = (uncertainty[a["id"]] + uncertainty[b["id"]]) / 1000.0
            if d <= radius + u:
                _union(possible_parent, a["id"], b["id"])
                if d + u <= radius:
                    _union(certain_parent, a["id"], b["id"])

    incident_of = {}
    for incident in report["incidents"]:
        for observation_id in incident["observation_ids"]:
            incident_of[observation_id] = incident["incident_id"]

    certain_ids = _component_ids(certain_parent)
    possible_ids = _component_ids(possible_parent)
    observations = [{
        "observation_id": row["id"],
        "incident_id": incident_of[row["id"]],
        "location_uncertainty_m": uncertainty[row["id"]],
        "certain_component_id": certain_ids[row["id"]],
        "possible_component_id": possible_ids[row["id"]],
    } for row in active]

    return {
        "radius_km": radius,
        "window_minutes": window,
        "cluster_report": report,
        "certain_components": _components(certain_parent),
        "possible_components": _components(possible_parent),
        "observations": observations,
    }


def _uncertainty_m(row):
    value = row.get("location_uncertainty_m", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UncertaintyError(
            f"location_uncertainty_m must be a number: {row['id']}")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise UncertaintyError(
            "location_uncertainty_m must be finite and non-negative: "
            f"{row['id']}")
    return value


def _root(parent, node):
    while parent[node] != node:
        parent[node] = parent[parent[node]]
        node = parent[node]
    return node


def _union(parent, a, b):
    parent[_root(parent, a)] = _root(parent, b)


def _groups(parent):
    groups = {}
    for node in parent:
        groups.setdefault(_root(parent, node), []).append(node)
    return groups.values()


def _component_ids(parent):
    ids = {}
    for members in _groups(parent):
        component_id = min(members)
        for node in members:
            ids[node] = component_id
    return ids


def _components(parent):
    result = [{"component_id": min(members), "observation_ids": sorted(members)}
              for members in _groups(parent)]
    result.sort(key=lambda component: component["component_id"])
    return result
