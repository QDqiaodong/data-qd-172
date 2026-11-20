import json
import unittest

from wildfire.core import _km, cluster
from wildfire.location import UncertaintyError, audit_location

T0 = "2026-07-01T00:00:00+00:00"


def _obs(oid, sensor, time, lat=0.0, lon=0.0, severity=0.5, uncertainty=None):
    row = {"id": oid, "sensor": sensor, "time": time,
           "lat": lat, "lon": lon, "severity": severity}
    if uncertainty is not None:
        row["location_uncertainty_m"] = uncertainty
    return row


def _payload(observations, windows=None, radius=1.0, window_minutes=60):
    data = {"radius_km": radius, "window_minutes": window_minutes,
            "observations": observations}
    if windows is not None:
        data["maintenance_windows"] = windows
    return data


def _membership(components):
    return {oid: frozenset(component["observation_ids"])
            for component in components
            for oid in component["observation_ids"]}


def _chain_payload():
    # Certain cores {a, b} and {c, d} plus singleton {e}; the b-c and d-e
    # links are each possible but not certain, so all five observations
    # share one conservative possible component.
    observations = [
        _obs("a", "s-a", "2026-07-01T00:00:00+00:00", lon=0.0),
        _obs("b", "s-b", "2026-07-01T00:05:00+00:00", lon=0.001,
             uncertainty=300),
        _obs("c", "s-c", "2026-07-01T00:10:00+00:00", lon=0.011,
             uncertainty=300),
        _obs("d", "s-d", "2026-07-01T00:15:00+00:00", lon=0.012,
             uncertainty=300),
        _obs("e", "s-e", "2026-07-01T00:20:00+00:00", lon=0.0225,
             uncertainty=300),
    ]
    return _payload(observations)


class DefaultUncertaintyTests(unittest.TestCase):
    def test_missing_uncertainty_identical_to_explicit_zero(self):
        observations = [
            _obs("a", "s-a", T0, lon=0.0),
            _obs("b", "s-b", T0, lon=0.02),
        ]
        plain = audit_location(_payload(observations))
        for zero in (0, 0.0):
            zeroed = [dict(row, location_uncertainty_m=zero)
                      for row in observations]
            result = audit_location(_payload(zeroed))
            self.assertEqual(json.dumps(plain, sort_keys=True),
                             json.dumps(result, sort_keys=True))
        # The effective value is reported back in meters.
        entry = plain["observations"][0]
        self.assertEqual(entry["location_uncertainty_m"], 0.0)


class SpatialBoundaryTests(unittest.TestCase):
    def test_certain_boundary_exact_at_radius(self):
        # Same reported point, uncertainty sum exactly radius_km:
        # d + u == radius is still certain (boundary inclusive).
        observations = [
            _obs("a", "s-a", T0, uncertainty=500),
            _obs("b", "s-b", T0, uncertainty=500.0),
        ]
        result = audit_location(_payload(observations, radius=1.0))
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a", "b"}))
        possible = _membership(result["possible_components"])
        self.assertEqual(possible["a"], frozenset({"a", "b"}))

    def test_just_beyond_certain_boundary_is_possible_only(self):
        # Same reported point, uncertainty sum 1000.5 m > 1 km radius:
        # no longer certain, still possible.
        observations = [
            _obs("a", "s-a", T0, uncertainty=500),
            _obs("b", "s-b", T0, uncertainty=500.5),
        ]
        result = audit_location(_payload(observations, radius=1.0))
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a"}))
        self.assertEqual(certain["b"], frozenset({"b"}))
        possible = _membership(result["possible_components"])
        self.assertEqual(possible["a"], frozenset({"a", "b"}))

    def test_possible_boundary_exact(self):
        # d == radius + u exactly: possible (inclusive) but not certain.
        a = _obs("a", "s-a", T0, lon=0.0)
        b = _obs("b", "s-b", T0, lon=0.5, uncertainty=250.0)
        d0 = _km(a, b)
        radius = d0 - 0.25
        self.assertEqual(d0, radius + 0.25)  # exact float boundary
        result = audit_location(_payload([a, b], radius=radius))
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a"}))
        self.assertEqual(certain["b"], frozenset({"b"}))
        possible = _membership(result["possible_components"])
        self.assertEqual(possible["a"], frozenset({"a", "b"}))

    def test_beyond_possible_boundary_disconnects(self):
        a = _obs("a", "s-a", T0, lon=0.0)
        b = _obs("b", "s-b", T0, lon=0.5, uncertainty=250.0)
        d0 = _km(a, b)
        radius = d0 - 0.5  # d > radius + u: no allowed connection
        result = audit_location(_payload([a, b], radius=radius))
        possible = _membership(result["possible_components"])
        self.assertEqual(possible["a"], frozenset({"a"}))
        self.assertEqual(possible["b"], frozenset({"b"}))
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a"}))
        self.assertEqual(certain["b"], frozenset({"b"}))

    def test_reported_distance_exactly_radius_is_certain(self):
        # With zero uncertainty the certain and possible boundaries
        # coincide with the clustering radius; d == radius is included.
        a = _obs("a", "s-a", T0, lon=0.0)
        b = _obs("b", "s-b", T0, lon=0.5)
        d0 = _km(a, b)
        result = audit_location(_payload([a, b], radius=d0))
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a", "b"}))


class TimeWindowTests(unittest.TestCase):
    def test_close_pair_beyond_window_not_connected(self):
        observations = [
            _obs("a", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("b", "s-b", "2026-07-01T00:31:00+00:00"),
        ]
        result = audit_location(_payload(observations, window_minutes=30))
        for key in ("certain_components", "possible_components"):
            membership = _membership(result[key])
            self.assertEqual(membership["a"], frozenset({"a"}))
            self.assertEqual(membership["b"], frozenset({"b"}))

    def test_pair_at_exact_window_edge_connected(self):
        observations = [
            _obs("a", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("b", "s-b", "2026-07-01T00:30:00+00:00"),
        ]
        result = audit_location(_payload(observations, window_minutes=30))
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a", "b"}))


class AntimeridianTests(unittest.TestCase):
    def test_dateline_crossing_uses_great_circle_distance(self):
        # 0.2 degrees apart across the date line: ~22 km great-circle,
        # while a planar longitude difference would be 359.8 degrees.
        a = _obs("a", "s-a", T0, lon=179.9)
        b = _obs("b", "s-b", T0, lon=-179.9)
        self.assertLess(_km(a, b), 40.0)
        result = audit_location(_payload([a, b], radius=40.0))
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a", "b"}))
        incidents = result["cluster_report"]["incidents"]
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["observation_ids"], ["a", "b"])


class MaintenanceSuppressionTests(unittest.TestCase):
    def _payload_with_outage(self):
        windows = [{"sensor_id": "s-x",
                    "start": "2026-07-01T00:00:00+00:00",
                    "end": "2026-07-01T00:10:00+00:00"}]
        observations = [
            _obs("a", "s-a", "2026-07-01T00:05:00+00:00", lon=0.0,
                 uncertainty=600),
            _obs("x", "s-x", "2026-07-01T00:06:00+00:00", lon=0.01,
                 uncertainty=600),
            _obs("b", "s-b", "2026-07-01T00:07:00+00:00", lon=0.02,
                 uncertainty=600),
        ]
        return _payload(observations, windows)

    def test_suppressed_observation_excluded_and_cannot_bridge(self):
        result = audit_location(self._payload_with_outage())
        self.assertEqual(
            result["cluster_report"]["suppressed_observation_ids"], ["x"])
        listed = {row["observation_id"] for row in result["observations"]}
        self.assertEqual(listed, {"a", "b"})
        for key in ("certain_components", "possible_components"):
            membership = _membership(result[key])
            self.assertNotIn("x", membership)
            # a and b are 2.22 km apart with 1.2 km of combined slack:
            # without the suppressed bridge no connection is possible.
            self.assertEqual(membership["a"], frozenset({"a"}))
            self.assertEqual(membership["b"], frozenset({"b"}))

    def test_active_bridge_would_join_possible_component(self):
        payload = self._payload_with_outage()
        del payload["maintenance_windows"]
        result = audit_location(payload)
        possible = _membership(result["possible_components"])
        self.assertEqual(possible["a"], frozenset({"a", "x", "b"}))


class ChainScenarioTests(unittest.TestCase):
    def test_certain_cores_merge_into_one_possible_component(self):
        result = audit_location(_chain_payload())
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a", "b"}))
        self.assertEqual(certain["c"], frozenset({"c", "d"}))
        self.assertEqual(certain["e"], frozenset({"e"}))
        # The two possible-only links (b-c and d-e) pull every core into
        # one conservative possible component.
        possible = _membership(result["possible_components"])
        self.assertEqual(possible["a"],
                         frozenset({"a", "b", "c", "d", "e"}))
        # Component ids are the smallest member id, like incident_id.
        self.assertEqual([c["component_id"] for c in
                          result["certain_components"]], ["a", "c", "e"])
        self.assertEqual([c["component_id"] for c in
                          result["possible_components"]], ["a"])
        # Reported-coordinate incidents stay the middle layer:
        # certain components refine incidents, incidents refine the
        # possible envelope.
        incidents = result["cluster_report"]["incidents"]
        self.assertEqual([i["observation_ids"] for i in incidents],
                         [["a", "b"], ["c", "d"], ["e"]])
        by_id = {row["observation_id"]: row
                 for row in result["observations"]}
        self.assertEqual(by_id["c"]["incident_id"], "c")
        self.assertEqual(by_id["c"]["certain_component_id"], "c")
        self.assertEqual(by_id["c"]["possible_component_id"], "a")
        self.assertEqual(by_id["e"]["possible_component_id"], "a")

    def test_incident_link_may_be_possible_only(self):
        # Within the radius at reported coordinates (one incident) but
        # beyond it once the uncertainty slack is added: the incident
        # splits into two certain components inside one possible one.
        a = _obs("a", "s-a", T0, lon=0.0)
        b = _obs("b", "s-b", T0, lon=0.008, uncertainty=200.0)
        d0 = _km(a, b)
        self.assertLess(d0, 1.0)
        self.assertGreater(d0 + 0.2, 1.0)
        result = audit_location(_payload([a, b], radius=1.0))
        incidents = result["cluster_report"]["incidents"]
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["observation_ids"], ["a", "b"])
        certain = _membership(result["certain_components"])
        self.assertEqual(certain["a"], frozenset({"a"}))
        self.assertEqual(certain["b"], frozenset({"b"}))
        possible = _membership(result["possible_components"])
        self.assertEqual(possible["a"], frozenset({"a", "b"}))

    def test_cluster_report_embedded_verbatim(self):
        payload = _chain_payload()
        result = audit_location(payload)
        self.assertEqual(result["cluster_report"], cluster(payload))
        self.assertEqual(result["radius_km"], 1.0)
        self.assertEqual(result["window_minutes"], 60)


class ReorderTests(unittest.TestCase):
    def test_input_reorder_does_not_change_report(self):
        payload = _chain_payload()
        payload["maintenance_windows"] = [
            {"sensor_id": "s-z",
             "start": "2026-07-01T00:00:00+00:00",
             "end": "2026-07-01T00:10:00+00:00"},
            {"sensor_id": "s-y",
             "start": "2026-07-01T01:00:00+00:00",
             "end": "2026-07-01T02:00:00+00:00"},
        ]
        payload["observations"].append(
            _obs("sup", "s-z", "2026-07-01T00:05:00+00:00", lon=0.001))
        first = audit_location(payload)
        shuffled = dict(payload)
        shuffled["observations"] = list(reversed(payload["observations"]))
        shuffled["maintenance_windows"] = list(
            reversed(payload["maintenance_windows"]))
        second = audit_location(shuffled)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        rotated = dict(payload)
        rotated["observations"] = (payload["observations"][2:]
                                   + payload["observations"][:2])
        third = audit_location(rotated)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(third, sort_keys=True))


class ValidationTests(unittest.TestCase):
    def test_rejects_invalid_uncertainty_values(self):
        bad_values = (-1, -0.5, "100", "abc", None, True, False,
                      float("nan"), float("inf"), float("-inf"),
                      [100], {"m": 100})
        for value in bad_values:
            with self.subTest(value=value):
                row = _obs("a", "s-a", T0)
                row["location_uncertainty_m"] = value
                observations = [row, _obs("b", "s-b", T0)]
                with self.assertRaisesRegex(ValueError,
                                            "location_uncertainty_m"):
                    audit_location(_payload(observations))

    def test_rejects_invalid_uncertainty_on_suppressed_observation(self):
        windows = [{"sensor_id": "s-a",
                    "start": "2026-07-01T00:00:00+00:00",
                    "end": "2026-07-01T01:00:00+00:00"}]
        observations = [
            _obs("a", "s-a", "2026-07-01T00:30:00+00:00", uncertainty=-1),
            _obs("b", "s-b", T0),
        ]
        with self.assertRaisesRegex(UncertaintyError,
                                    "location_uncertainty_m"):
            audit_location(_payload(observations, windows))

    def test_accepts_numeric_uncertainty_forms(self):
        observations = [
            _obs("a", "s-a", T0, uncertainty=0),
            _obs("b", "s-b", T0, uncertainty=0.0),
            _obs("c", "s-c", T0, uncertainty=100),
            _obs("d", "s-d", T0, uncertainty=2.5),
            _obs("e", "s-e", T0, uncertainty=1e3),
        ]
        result = audit_location(_payload(observations))
        effective = {row["observation_id"]: row["location_uncertainty_m"]
                     for row in result["observations"]}
        self.assertEqual(effective, {"a": 0.0, "b": 0.0, "c": 100.0,
                                     "d": 2.5, "e": 1000.0})

    def test_uncertainty_error_is_a_value_error(self):
        observations = [_obs("a", "s-a", T0, uncertainty=float("nan"))]
        with self.assertRaises(ValueError):
            audit_location(_payload(observations))


class EmptyAndSingletonTests(unittest.TestCase):
    def test_empty_observations(self):
        result = audit_location(_payload([]))
        self.assertEqual(result["certain_components"], [])
        self.assertEqual(result["possible_components"], [])
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["cluster_report"]["observation_count"], 0)

    def test_single_observation_forms_singleton_components(self):
        result = audit_location(
            _payload([_obs("solo", "s-a", T0, uncertainty=50)]))
        self.assertEqual(result["certain_components"],
                         [{"component_id": "solo",
                           "observation_ids": ["solo"]}])
        self.assertEqual(result["possible_components"],
                         [{"component_id": "solo",
                           "observation_ids": ["solo"]}])
        entry = result["observations"][0]
        self.assertEqual(entry["observation_id"], "solo")
        self.assertEqual(entry["incident_id"], "solo")
        self.assertEqual(entry["certain_component_id"], "solo")
        self.assertEqual(entry["possible_component_id"], "solo")


class LegacyCompatibilityTests(unittest.TestCase):
    def test_cluster_ignores_uncertainty_field(self):
        payload = _chain_payload()
        stripped = _payload([{k: v for k, v in row.items()
                              if k != "location_uncertainty_m"}
                             for row in payload["observations"]])
        self.assertEqual(cluster(payload), cluster(stripped))


if __name__ == "__main__":
    unittest.main()
