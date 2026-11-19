from datetime import datetime
from math import asin, cos, radians, sin, sqrt


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamps require an offset")
    return result


def _km(a, b):
    lat1, lon1, lat2, lon2 = map(radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(min(1, h)))


def cluster(data):
    rows = data["observations"]
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("observation IDs must be unique")
    radius, window = float(data["radius_km"]), int(data["window_minutes"])
    if radius <= 0 or window < 0:
        raise ValueError("radius must be positive and window nonnegative")
    for r in rows:
        if not (-90 <= float(r["lat"]) <= 90 and -180 <= float(r["lon"]) <= 180):
            raise ValueError(f"invalid coordinates: {r['id']}")
        if not 0 <= float(r["severity"]) <= 1:
            raise ValueError(f"invalid severity: {r['id']}")
        _time(r["time"])
    parent = list(range(len(rows)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(rows):
        for j in range(i):
            b = rows[j]
            if abs((_time(a["time"]) - _time(b["time"])).total_seconds()) <= window * 60 and _km(a, b) <= radius:
                parent[root(i)] = root(j)
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(root(i), []).append(row)
    incidents = []
    for members in groups.values():
        members.sort(key=lambda r: r["id"])
        incidents.append({"incident_id": members[0]["id"], "observation_ids": [r["id"] for r in members],
                          "sensor_count": len({r["sensor"] for r in members}), "observation_count": len(members),
                          "confidence": round(len({r["sensor"] for r in members}) / len(members), 6),
                          "mean_severity": round(sum(float(r["severity"]) for r in members) / len(members), 6)})
    return {"incidents": sorted(incidents, key=lambda x: x["incident_id"]), "observation_count": len(rows)}
