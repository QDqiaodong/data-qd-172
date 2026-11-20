import json
import unittest
from pathlib import Path
from wildfire.core import cluster


def _observation(oid, sensor, time, lat=0.0, lon=0.0, severity=0.5):
    return {"id": oid, "sensor": sensor, "time": time, "lat": lat, "lon": lon, "severity": severity}


def _bridge_payload():
    # a and c are 1.33 km apart (beyond the 1 km radius); b sits 0.67 km from
    # each, so with b active all three merge into one transitive incident.
    return {"radius_km": 1.0, "window_minutes": 0,
            "observations": [_observation("a", "s-a", "2026-07-01T00:00:00+00:00", lon=0.0, severity=0.2),
                             _observation("b", "s-b", "2026-07-01T00:00:00+00:00", lon=0.006, severity=0.9),
                             _observation("c", "s-c", "2026-07-01T00:00:00+00:00", lon=0.012, severity=0.4)],
            "maintenance_windows": [{"sensor_id": "s-b",
                                     "start": "2026-06-30T23:00:00+00:00",
                                     "end": "2026-07-01T01:00:00+00:00"}]}


class ClusterTests(unittest.TestCase):
    def test_fixture_forms_two_incidents(self):
        result = cluster(json.loads(Path("fixtures/input.json").read_text()))
        self.assertEqual(len(result["incidents"]), 2)
        self.assertEqual(result["incidents"][0]["observation_ids"], ["o1", "o2"])

    def test_rejects_invalid_coordinates(self):
        with self.assertRaisesRegex(ValueError, "coordinates"):
            cluster({"radius_km": 1, "window_minutes": 1, "observations": [{"id": "x", "sensor": "s", "time": "2026-01-01T00:00:00+00:00", "lat": 91, "lon": 0, "severity": 0.2}]})


class MaintenanceWindowTests(unittest.TestCase):
    def test_suppressed_observation_cannot_bridge_incidents(self):
        result = cluster(_bridge_payload())
        self.assertEqual([i["observation_ids"] for i in result["incidents"]], [["a"], ["c"]])
        self.assertEqual([i["incident_id"] for i in result["incidents"]], ["a", "c"])
        self.assertEqual(result["suppressed_observation_ids"], ["b"])
        self.assertEqual(result["observation_count"], 3)
        for incident, severity in zip(result["incidents"], (0.2, 0.4)):
            self.assertEqual(incident["sensor_count"], 1)
            self.assertEqual(incident["observation_count"], 1)
            self.assertEqual(incident["confidence"], 1.0)
            self.assertEqual(incident["mean_severity"], severity)

    def test_active_bridge_would_otherwise_join_incidents(self):
        payload = _bridge_payload()
        del payload["maintenance_windows"]
        result = cluster(payload)
        self.assertEqual([i["observation_ids"] for i in result["incidents"]], [["a", "b", "c"]])
        self.assertEqual(result["suppressed_observation_ids"], [])

    def test_missing_and_empty_windows_yield_empty_suppression(self):
        payload = _bridge_payload()
        for value in (None, []):
            if value is None:
                payload.pop("maintenance_windows", None)
            else:
                payload["maintenance_windows"] = value
            result = cluster(payload)
            self.assertEqual(result["suppressed_observation_ids"], [])
            self.assertEqual(len(result["incidents"]), 1)
            self.assertEqual(result["observation_count"], 3)

    def test_window_start_inclusive_end_exclusive(self):
        payload = {"radius_km": 1.0, "window_minutes": 0,
                   "observations": [_observation("at-start", "s", "2026-07-01T10:00:00+00:00", lon=0.0),
                                    _observation("at-end", "s", "2026-07-01T11:00:00+00:00", lon=0.0)],
                   "maintenance_windows": [{"sensor_id": "s",
                                            "start": "2026-07-01T10:00:00+00:00",
                                            "end": "2026-07-01T11:00:00+00:00"}]}
        result = cluster(payload)
        self.assertEqual(result["suppressed_observation_ids"], ["at-start"])
        self.assertEqual([i["observation_ids"] for i in result["incidents"]], [["at-end"]])
        self.assertEqual(result["observation_count"], 2)

    def test_window_applies_only_to_matching_sensor(self):
        payload = _bridge_payload()
        payload["maintenance_windows"] = [{"sensor_id": "s-other",
                                           "start": "2026-06-30T23:00:00+00:00",
                                           "end": "2026-07-01T01:00:00+00:00"}]
        result = cluster(payload)
        self.assertEqual(result["suppressed_observation_ids"], [])
        self.assertEqual([i["observation_ids"] for i in result["incidents"]], [["a", "b", "c"]])

    def test_overlapping_and_duplicate_windows_suppress_once(self):
        payload = _bridge_payload()
        payload["maintenance_windows"] = [
            {"sensor_id": "s-b", "start": "2026-06-30T23:00:00+00:00", "end": "2026-07-01T01:00:00+00:00"},
            {"sensor_id": "s-b", "start": "2026-06-30T23:00:00+00:00", "end": "2026-07-01T01:00:00+00:00"},
            {"sensor_id": "s-b", "start": "2026-06-30T22:00:00+00:00", "end": "2026-06-30T23:00:00+00:00"},
            {"sensor_id": "s-b", "start": "2026-07-01T00:30:00+00:00", "end": "2026-07-01T02:00:00+00:00"},
        ]
        result = cluster(payload)
        self.assertEqual(result["suppressed_observation_ids"], ["b"])
        self.assertEqual([i["observation_ids"] for i in result["incidents"]], [["a"], ["c"]])

    def test_suppressed_ids_sorted_lexicographically(self):
        payload = {"radius_km": 1.0, "window_minutes": 0,
                   "observations": [_observation("z9", "s", "2026-07-01T10:00:00+00:00"),
                                    _observation("a10", "s", "2026-07-01T10:00:00+00:00")],
                   "maintenance_windows": [{"sensor_id": "s",
                                            "start": "2026-07-01T09:00:00+00:00",
                                            "end": "2026-07-01T12:00:00+00:00"}]}
        result = cluster(payload)
        self.assertEqual(result["suppressed_observation_ids"], ["a10", "z9"])
        self.assertEqual(result["incidents"], [])
        self.assertEqual(result["observation_count"], 2)

    def test_rejects_window_start_not_before_end(self):
        for start, end in (("2026-07-01T10:00:00+00:00", "2026-07-01T10:00:00+00:00"),
                           ("2026-07-01T11:00:00+00:00", "2026-07-01T10:00:00+00:00")):
            payload = {"radius_km": 1.0, "window_minutes": 0,
                       "observations": [_observation("x", "s", "2026-07-01T10:30:00+00:00")],
                       "maintenance_windows": [{"sensor_id": "s", "start": start, "end": end}]}
            with self.assertRaisesRegex(ValueError, "before end"):
                cluster(payload)

    def test_rejects_window_missing_required_field(self):
        for window in ({"start": "2026-07-01T10:00:00+00:00", "end": "2026-07-01T11:00:00+00:00"},
                       {"sensor_id": "s", "end": "2026-07-01T11:00:00+00:00"},
                       {"sensor_id": "s", "start": "2026-07-01T10:00:00+00:00"}):
            payload = {"radius_km": 1.0, "window_minutes": 0,
                       "observations": [_observation("x", "s", "2026-07-01T10:30:00+00:00")],
                       "maintenance_windows": [window]}
            with self.assertRaisesRegex(ValueError, "missing field"):
                cluster(payload)

    def test_rejects_window_timestamp_without_offset(self):
        payload = {"radius_km": 1.0, "window_minutes": 0,
                   "observations": [_observation("x", "s", "2026-07-01T10:30:00+00:00")],
                   "maintenance_windows": [{"sensor_id": "s",
                                            "start": "2026-07-01T10:00:00",
                                            "end": "2026-07-01T11:00:00+00:00"}]}
        with self.assertRaisesRegex(ValueError, "offset"):
            cluster(payload)


if __name__ == "__main__":
    unittest.main()
