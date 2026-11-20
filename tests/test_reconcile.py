import json
import unittest
from pathlib import Path

from wildfire.core import cluster
from wildfire.reconcile import reconcile, ReconciliationError


def _primary(row_id, sensor, utc_ms, lat=10.0, lon=20.0, severity=0.5):
    return {"id": row_id, "sensor": sensor, "utc_ms": utc_ms,
            "lat": lat, "lon": lon, "severity": severity}


def _backup(row_id, sensor, clock_ms, lat=10.0, lon=20.0, severity=0.5):
    return {"id": row_id, "sensor": sensor, "clock_ms": clock_ms,
            "lat": lat, "lon": lon, "severity": severity}


def _anchor(clock_ms, utc_min_ms, utc_max_ms):
    return {"clock_ms": clock_ms, "utc_min_ms": utc_min_ms,
            "utc_max_ms": utc_max_ms}


def _payload(primary, backup, anchors, tolerance_ms=0, radius_km=10.0,
             window_minutes=60, maintenance_windows=None):
    payload = {"radius_km": radius_km, "window_minutes": window_minutes,
               "tolerance_ms": tolerance_ms, "primary": primary,
               "backup": backup, "anchors": anchors}
    if maintenance_windows is not None:
        payload["maintenance_windows"] = maintenance_windows
    return payload


def _reports(result):
    return {row["id"]: row for row in result["reconciliation"]["backup_rows"]}


class FixedOffsetMatchingTests(unittest.TestCase):
    def setUp(self):
        self.anchors = [_anchor(0, 0, 0)]  # offset pinned to exactly 0

    def test_tolerance_one_forced_matching_keeps_only_primary_rows(self):
        payload = _payload(
            primary=[_primary("p1", "S", 100), _primary("p2", "S", 102)],
            backup=[_backup("b1", "S", 101), _backup("b2", "S", 103)],
            anchors=self.anchors, tolerance_ms=1)
        result = reconcile(payload)
        detail = result["reconciliation"]
        self.assertEqual(detail["max_duplicate_pairs"], 2)
        self.assertEqual(detail["optimal_offset_intervals"],
                         [{"min": 0, "max": 0}])
        reports = _reports(result)
        # The size-2 maximum matching is unique: b2's only edge is p2, so b1
        # must take p1. The b1-p2 edge only appears in size-1 (non-maximum)
        # pairings and must not be reported as an optimal-solution candidate.
        self.assertEqual(reports["b1"]["possible_primary_ids"], ["p1"])
        self.assertFalse(reports["b1"]["can_be_unpaired_in_optimal_solution"])
        self.assertEqual(reports["b1"]["decision"], "shelved")
        self.assertEqual(reports["b1"]["reason_code"],
                         "duplicate_always_same_primary")
        self.assertEqual(reports["b2"]["possible_primary_ids"], ["p2"])
        self.assertFalse(reports["b2"]["can_be_unpaired_in_optimal_solution"])
        self.assertEqual(reports["b2"]["reason_code"],
                         "duplicate_always_same_primary")
        # Greedily pairing 102 with 101 would be a non-maximum pairing and must
        # not promote 103 into the replay.
        self.assertEqual([row["id"] for row in result["observations"]],
                         ["primary:p1", "primary:p2"])
        self.assertEqual(_reports(result)["b2"]["decision"], "shelved")

    def test_always_duplicate_backup_reports_both_primary_candidates(self):
        payload = _payload(
            primary=[_primary("p1", "S", 99), _primary("p2", "S", 101)],
            backup=[_backup("b1", "S", 100)],
            anchors=self.anchors, tolerance_ms=1)
        result = reconcile(payload)
        report = _reports(result)["b1"]
        self.assertEqual(result["reconciliation"]["max_duplicate_pairs"], 1)
        self.assertEqual(report["possible_primary_ids"], ["p1", "p2"])
        self.assertFalse(report["can_be_unpaired_in_optimal_solution"])
        self.assertEqual(report["decision"], "shelved")
        self.assertEqual(report["reason_code"],
                         "duplicate_primary_ambiguous")
        self.assertEqual([row["id"] for row in result["observations"]],
                         ["primary:p1", "primary:p2"])

    def test_unique_backup_sample_is_included_with_corrected_time(self):
        payload = _payload(
            primary=[_primary("p1", "S", 100)],
            backup=[_backup("b1", "S", 500)],
            anchors=self.anchors, tolerance_ms=1)
        result = reconcile(payload)
        report = _reports(result)["b1"]
        self.assertEqual(report["decision"], "included")
        self.assertEqual(report["reason_code"],
                         "included_no_duplicate_utc_fixed")
        self.assertEqual(report["corrected_utc_ms"], 500)
        self.assertEqual(report["possible_primary_ids"], [])
        self.assertTrue(report["can_be_unpaired_in_optimal_solution"])
        included = next(row for row in result["observations"]
                        if row["id"] == "backup:b1")
        self.assertEqual(included["time"], "1970-01-01T00:00:00.500+00:00")
        self.assertEqual(included["sensor"], "S")
        # Primary row keeps its original values and UTC.
        primary = next(row for row in result["observations"]
                       if row["id"] == "primary:p1")
        self.assertEqual(primary["time"], "1970-01-01T00:00:00.100+00:00")


class OffsetRangeTests(unittest.TestCase):
    def _base_payload(self):
        anchors = [_anchor(0, 0, 10)]  # any offset in [0, 10]
        primary = [_primary("pS", "S", 100), _primary("pT", "T", 210)]
        backup = [_backup("bS", "S", 100), _backup("bT", "T", 200),
                  _backup("bW", "W", 300)]
        return _payload(primary, backup, anchors, tolerance_ms=0)

    def test_split_optimal_offsets_shelve_every_backup_row(self):
        result = reconcile(self._base_payload())
        detail = result["reconciliation"]
        self.assertEqual(detail["max_duplicate_pairs"], 1)
        # Offset 0 explains S; offset 10 explains T; 1..9 explain nothing, so
        # the two optimal points stay in separate closed intervals.
        self.assertEqual(detail["optimal_offset_intervals"],
                         [{"min": 0, "max": 0}, {"min": 10, "max": 10}])
        reports = _reports(result)
        for backup_id in ("bS", "bT"):
            self.assertTrue(
                reports[backup_id]["can_be_unpaired_in_optimal_solution"])
            self.assertEqual(reports[backup_id]["decision"], "shelved")
            self.assertEqual(reports[backup_id]["reason_code"],
                             "duplicate_in_some_optimal_solutions")
            self.assertNotIn("corrected_utc_ms", reports[backup_id])
            self.assertEqual(reports[backup_id]["possible_corrected_utc_ms"],
                             {"min": 100 if backup_id == "bS" else 200,
                              "max": 110 if backup_id == "bS" else 210})
        w_report = reports["bW"]
        self.assertEqual(w_report["possible_primary_ids"], [])
        self.assertEqual(w_report["decision"], "shelved")
        self.assertEqual(w_report["reason_code"],
                         "no_duplicate_utc_undetermined")
        self.assertEqual([row["id"] for row in result["observations"]],
                         ["primary:pS", "primary:pT"])

    def test_shared_offset_narrows_and_unique_backups_are_included(self):
        payload = self._base_payload()
        payload["primary"].append(_primary("pU", "U", 410))
        payload["backup"].append(_backup("bU", "U", 400))
        result = reconcile(payload)
        detail = result["reconciliation"]
        self.assertEqual(detail["max_duplicate_pairs"], 2)
        self.assertEqual(detail["optimal_offset_intervals"],
                         [{"min": 10, "max": 10}])
        reports = _reports(result)
        self.assertEqual(reports["bS"]["decision"], "included")
        self.assertEqual(reports["bS"]["corrected_utc_ms"], 110)
        self.assertEqual(reports["bW"]["decision"], "included")
        self.assertEqual(reports["bW"]["corrected_utc_ms"], 310)
        for backup_id in ("bT", "bU"):
            self.assertEqual(reports[backup_id]["decision"], "shelved")
        observation_ids = sorted(row["id"] for row in result["observations"])
        self.assertEqual(
            observation_ids,
            ["backup:bS", "backup:bW", "primary:pS", "primary:pT",
             "primary:pU"])

        # The product is a direct clustering input: all five merged rows are
        # counted once, shelved backup rows never reach downstream statistics.
        report = cluster(result)
        self.assertEqual(report["observation_count"], 5)
        touched = set(report["suppressed_observation_ids"])
        for incident in report["incidents"]:
            touched.update(incident["observation_ids"])
        self.assertEqual(touched, set(observation_ids))
        self.assertNotIn("backup:bT", touched)
        self.assertNotIn("backup:bU", touched)
        # Every retained row lands in one co-located, time-bounded incident;
        # the co-located S pair contributes two observations, and no second
        # copy of T or U is present.
        only_incident = report["incidents"][0]
        self.assertEqual(len(report["incidents"]), 1)
        self.assertEqual(sorted(only_incident["observation_ids"]),
                         sorted(observation_ids))
        self.assertEqual(only_incident["observation_count"], 5)


class EdgeCaseTests(unittest.TestCase):
    def test_empty_exports_are_replayable(self):
        payload = _payload([], [], [_anchor(0, 5, 5)])
        result = reconcile(payload)
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["reconciliation"]["max_duplicate_pairs"], 0)
        self.assertEqual(result["reconciliation"]["optimal_offset_intervals"],
                         [{"min": 5, "max": 5}])
        self.assertEqual(result["reconciliation"]["backup_rows"], [])
        self.assertEqual(cluster(result)["observation_count"], 0)

    def test_empty_primary_includes_all_backups_when_offset_fixed(self):
        payload = _payload(
            primary=[],
            backup=[_backup("b1", "S", 100), _backup("b2", "T", 200)],
            anchors=[_anchor(0, 7, 7)], tolerance_ms=5)
        result = reconcile(payload)
        self.assertEqual(
            sorted(row["id"] for row in result["observations"]),
            ["backup:b1", "backup:b2"])
        self.assertEqual(
            [row["time"] for row in result["observations"]
             if row["id"] == "backup:b1"],
            ["1970-01-01T00:00:00.107+00:00"])

    def test_conflicting_anchors_raise(self):
        payload = _payload(
            [_primary("p1", "S", 10)],
            [_backup("b1", "S", 10)],
            [_anchor(0, 0, 5), _anchor(0, 9, 9)])
        with self.assertRaisesRegex(ReconciliationError, "no single offset"):
            reconcile(payload)

    def test_inverted_anchor_interval_raises(self):
        payload = _payload([], [], [_anchor(0, 9, 8)])
        with self.assertRaisesRegex(ReconciliationError, "inverted"):
            reconcile(payload)

    def test_missing_anchor_list_raises(self):
        payload = _payload([], [], [])
        with self.assertRaisesRegex(ReconciliationError, "non-empty"):
            reconcile(payload)

    def test_anchor_field_wrong_type_raises(self):
        payload = _payload([], [], [{"clock_ms": 0, "utc_min_ms": 1}])
        with self.assertRaisesRegex(ReconciliationError, "utc_max_ms"):
            reconcile(payload)
        payload = _payload([], [], "nope")
        with self.assertRaisesRegex(ReconciliationError, "non-empty"):
            reconcile(payload)

    def test_duplicate_same_gateway_row_id_raises(self):
        payload = _payload(
            [_primary("x", "S", 1), _primary("x", "S", 2)],
            [], [_anchor(0, 0, 0)])
        with self.assertRaisesRegex(ReconciliationError, "duplicate primary"):
            reconcile(payload)
        payload = _payload(
            [], [_backup("x", "S", 1), _backup("x", "S", 2)],
            [_anchor(0, 0, 0)])
        with self.assertRaisesRegex(ReconciliationError, "duplicate backup"):
            reconcile(payload)

    def test_shared_row_number_with_different_measurement_is_not_a_pair(self):
        # The same raw row number on both gateways must never drive pairing.
        payload = _payload(
            [_primary("r7", "S", 100, severity=0.9)],
            [_backup("r7", "S", 100, severity=0.1)],
            [_anchor(0, 0, 0)], tolerance_ms=0)
        result = reconcile(payload)
        report = _reports(result)["r7"]
        self.assertEqual(report["possible_primary_ids"], [])
        self.assertEqual(report["decision"], "included")
        observation_ids = {row["id"] for row in result["observations"]}
        self.assertEqual(observation_ids, {"primary:r7", "backup:r7"})

    def test_reordering_inputs_and_anchors_does_not_change_output(self):
        anchors = [_anchor(50, 40, 60), _anchor(0, -10, 10)]
        primary = [_primary("p2", "T", 210), _primary("p1", "S", 100),
                   _primary("p3", "U", 410)]
        backup = [_backup("b3", "U", 400), _backup("b1", "S", 100),
                  _backup("b2", "T", 200), _backup("b4", "W", 300)]
        first = reconcile(_payload(primary, backup, anchors))
        shuffled = _payload(list(reversed(primary)),
                            [backup[2], backup[0], backup[3], backup[1]],
                            list(reversed(anchors)))
        second = reconcile(shuffled)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True))

    def test_hours_wide_anchor_range_does_not_blow_up_with_ms_width(self):
        three_hours = 3 * 60 * 60 * 1000
        anchors = [_anchor(0, -three_hours, three_hours)]
        # The pairing condition is true over the entire range, so the whole
        # 21.6-million-ms span is one segment.
        payload = _payload(
            [_primary("p1", "S", 0)],
            [_backup("b1", "S", 0), _backup("b2", "W", 5_000_000)],
            anchors, tolerance_ms=three_hours)
        result = reconcile(payload)
        detail = result["reconciliation"]
        self.assertEqual(detail["anchor_offset_range"],
                         {"min": -three_hours, "max": three_hours})
        self.assertEqual(detail["max_duplicate_pairs"], 1)
        self.assertEqual(detail["optimal_offset_intervals"],
                         [{"min": -three_hours, "max": three_hours}])
        reports = _reports(result)
        self.assertFalse(reports["b1"]["can_be_unpaired_in_optimal_solution"])
        self.assertEqual(reports["b1"]["decision"], "shelved")
        self.assertEqual(reports["b1"]["reason_code"],
                         "duplicate_always_same_primary")
        self.assertEqual(reports["b2"]["decision"], "shelved")
        self.assertEqual(reports["b2"]["reason_code"],
                         "no_duplicate_utc_undetermined")

    def test_wide_range_without_any_edges_is_one_segment(self):
        hour = 60 * 60 * 1000
        payload = _payload(
            [_primary("p1", "S", 0)],
            [_backup("b1", "W", 123)],
            [_anchor(0, -2 * hour, 2 * hour)], tolerance_ms=0)
        result = reconcile(payload)
        self.assertEqual(result["reconciliation"]["optimal_offset_intervals"],
                         [{"min": -2 * hour, "max": 2 * hour}])
        report = _reports(result)["b1"]
        self.assertEqual(report["decision"], "shelved")
        self.assertEqual(report["possible_corrected_utc_ms"],
                         {"min": 123 - 2 * hour, "max": 123 + 2 * hour})

    def test_maintenance_windows_are_preserved_for_downstream(self):
        window = [{"sensor_id": "S",
                   "start": "1970-01-01T00:00:00+00:00",
                   "end": "1970-01-02T00:00:00+00:00"}]
        payload = _payload(
            [_primary("p1", "S", 100)],
            [_backup("b1", "T", 200)],
            [_anchor(0, 0, 0)], maintenance_windows=window)
        result = reconcile(payload)
        self.assertEqual(result["maintenance_windows"], window)
        report = cluster(result)
        self.assertEqual(report["suppressed_observation_ids"],
                         ["primary:p1"])

    def test_validation_failures(self):
        good = _payload([_primary("p1", "S", 1)],
                        [_backup("b1", "S", 1)], [_anchor(0, 0, 0)])
        cases = [
            (dict(good, tolerance_ms=-1), "tolerance_ms"),
            (dict(good, tolerance_ms=1.5), "tolerance_ms"),
            (dict(good, radius_km=0), "radius_km"),
            (dict(good, window_minutes=-2), "window_minutes"),
        ]
        for broken, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaisesRegex(ReconciliationError, needle):
                    reconcile(broken)
        bad_rows = [
            ([_primary("p1", "S", 1, lat=91)], [], "lat"),
            ([_primary("p1", "S", 1, lon=-181)], [], "lon"),
            ([_primary("p1", "S", 1, severity=1.1)], [], "severity"),
        ]
        for primary, backup, needle in bad_rows:
            with self.subTest(needle=needle):
                with self.assertRaisesRegex(ReconciliationError, needle):
                    reconcile(_payload(primary, backup, [_anchor(0, 0, 0)]))
        missing = _payload([dict(_primary("p1", "S", 1))],
                           [_backup("b1", "S", 1)], [_anchor(0, 0, 0)])
        del missing["primary"][0]["utc_ms"]
        with self.assertRaisesRegex(ReconciliationError, "utc_ms"):
            reconcile(missing)
        with self.assertRaisesRegex(ReconciliationError, "JSON object"):
            reconcile([])


if __name__ == "__main__":
    unittest.main()
