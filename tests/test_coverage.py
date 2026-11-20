import json
import unittest
from datetime import datetime, timezone

from wildfire.coverage import audit_coverage, CoverageError
from wildfire.core import cluster


def _obs(oid, sensor, time, lat=0.0, lon=0.0, severity=0.5):
    return {"id": oid, "sensor": sensor, "time": time,
            "lat": lat, "lon": lon, "severity": severity}


def _payload(observations, windows=None, radius=1.0, window_minutes=60):
    data = {"radius_km": radius, "window_minutes": window_minutes,
            "observations": observations}
    if windows is not None:
        data["maintenance_windows"] = windows
    return data


def _plan(sensor, active_start, active_end, cadence, phase):
    return {"sensor_id": sensor, "active_start": active_start,
            "active_end": active_end, "cadence_seconds": cadence,
            "phase": phase}


def _schedule(entries):
    return {"sensors": entries}


# Scenarios that reason about hourly grids use this cadence; finer grids
# use CADENCE. Tolerance stays strictly below half of every cadence used.
CADENCE = 600
HOURLY_CADENCE = 3600
TOLERANCE = 60


class SlotGenerationTests(unittest.TestCase):
    def test_z_and_offset_spellings_denote_identical_slots(self):
        observations = [
            _obs("a", "s-a", "2026-07-01T01:00:00+00:00"),
            _obs("b", "s-b", "2026-07-01T01:00:00Z"),
        ]
        schedule = _schedule([
            _plan("s-a", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T02:00:00+00:00", HOURLY_CADENCE,
                  "2026-07-01T00:00:00Z"),
            _plan("s-b", "2026-07-01T00:00:00Z",
                  "2026-07-01T02:00:00Z", HOURLY_CADENCE,
                  "2026-07-01T00:00:00+00:00"),
        ])
        result = audit_coverage(
            _payload(observations), schedule,
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T02:00:00Z",
            tolerance_seconds=TOLERANCE, minimum_coverage=0.8)
        for sensor in result["sensors"]:
            slot_times = [slot["slot_time"] for slot in sensor["slots"]]
            self.assertEqual(slot_times[0], "2026-07-01T00:00:00+00:00")
            self.assertEqual(slot_times[1], "2026-07-01T01:00:00+00:00")
            covered = [slot for slot in sensor["slots"]
                       if slot["status"] == "covered"]
            self.assertEqual(len(covered), 1)
            self.assertEqual(covered[0]["slot_time"],
                             "2026-07-01T01:00:00+00:00")

    def test_deployment_and_audit_boundaries_are_half_open(self):
        # Deployment start and audit start both land exactly on a slot and
        # count; the slot on both ends is excluded.
        schedule = _schedule([_plan(
            "s", "2026-07-01T01:00:00+00:00",
            "2026-07-01T04:00:00+00:00", HOURLY_CADENCE,
            "2026-07-01T00:00:00+00:00")])
        observations = [
            _obs("o1", "s", "2026-07-01T01:00:00+00:00"),
            _obs("o2", "s", "2026-07-01T02:00:00+00:00"),
            _obs("o3", "s", "2026-07-01T03:00:00+00:00"),
            # This reading lands on the excluded end slot and must not
            # resurrect it.
            _obs("o4", "s", "2026-07-01T04:00:00+00:00"),
        ]
        result = audit_coverage(
            _payload(observations), schedule,
            start="2026-07-01T01:00:00+00:00",
            end="2026-07-01T04:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        sensor = result["sensors"][0]
        self.assertEqual([slot["slot_time"] for slot in sensor["slots"]],
                         ["2026-07-01T01:00:00+00:00",
                          "2026-07-01T02:00:00+00:00",
                          "2026-07-01T03:00:00+00:00"])
        self.assertEqual(sensor["expected_slots"], 3)
        self.assertEqual(sensor["covered_slots"], 3)
        self.assertEqual(sensor["missing_slots"], 0)
        self.assertEqual(sensor["off_schedule_observation_ids"], ["o4"])

    def test_phase_offsets_slot_grid(self):
        schedule = _schedule([_plan(
            "s", "2026-07-01T00:00:00+00:00",
            "2026-07-01T01:00:00+00:00", CADENCE,
            "2026-07-01T00:03:00+00:00")])
        result = audit_coverage(
            _payload([]), schedule,
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T01:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        self.assertEqual(
            [slot["slot_time"] for slot in result["sensors"][0]["slots"]],
            ["2026-07-01T00:03:00+00:00", "2026-07-01T00:13:00+00:00",
             "2026-07-01T00:23:00+00:00", "2026-07-01T00:33:00+00:00",
             "2026-07-01T00:43:00+00:00", "2026-07-01T00:53:00+00:00"])


class CoverageMatchingTests(unittest.TestCase):
    def _hourly_schedule(self, sensor="s"):
        return _schedule([_plan(
            sensor, "2026-07-01T00:00:00+00:00",
            "2026-07-01T04:00:00+00:00", HOURLY_CADENCE,
            "2026-07-01T00:00:00+00:00")])

    def test_maintenance_slot_excluded_and_suppressed_reading_not_coverage(self):
        windows = [{"sensor_id": "s",
                    "start": "2026-07-01T01:00:00+00:00",
                    "end": "2026-07-01T02:00:00+00:00"}]
        observations = [
            _obs("at-outage", "s", "2026-07-01T01:00:00+00:00"),
            _obs("ok-0", "s", "2026-07-01T00:00:00+00:00"),
            _obs("ok-2", "s", "2026-07-01T02:00:00+00:00"),
            _obs("ok-3", "s", "2026-07-01T03:00:00+00:00"),
        ]
        result = audit_coverage(
            _payload(observations, windows), self._hourly_schedule(),
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T04:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        sensor = result["sensors"][0]
        self.assertEqual(sensor["expected_slots"], 4)
        self.assertEqual(sensor["planned_outage_slots"], 1)
        self.assertEqual(sensor["eligible_slots"], 3)
        self.assertEqual(sensor["covered_slots"], 3)
        self.assertEqual(sensor["missing_slots"], 0)
        outage_slots = [slot for slot in sensor["slots"]
                        if slot["status"] == "planned_outage"]
        self.assertEqual([slot["slot_time"] for slot in outage_slots],
                         ["2026-07-01T01:00:00+00:00"])
        # Planned outages must never be reported as missing.
        self.assertNotIn("missing", {slot["status"] for slot in outage_slots})
        # The maintenance-period reading is suppressed by the existing
        # clustering rule and can neither cover nor become off_schedule.
        self.assertEqual(result["cluster_report"]["suppressed_observation_ids"],
                         ["at-outage"])
        self.assertEqual(sensor["off_schedule_observation_ids"], [])
        self.assertEqual(result["off_schedule_observations"], [])

    def test_duplicate_and_overlapping_maintenance_windows_count_once(self):
        windows = [
            {"sensor_id": "s",
             "start": "2026-07-01T01:00:00+00:00",
             "end": "2026-07-01T01:30:00+00:00"},
            {"sensor_id": "s",
             "start": "2026-07-01T01:00:00+00:00",
             "end": "2026-07-01T02:00:00+00:00"},
            {"sensor_id": "s",
             "start": "2026-07-01T01:30:00+00:00",
             "end": "2026-07-01T02:30:00+00:00"},
        ]
        result = audit_coverage(
            _payload([], windows), self._hourly_schedule(),
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T04:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        sensor = result["sensors"][0]
        # Only the 01:00 and 02:00 slots intersect the merged [01:00,02:30).
        self.assertEqual(sensor["planned_outage_slots"], 2)
        self.assertEqual(sensor["eligible_slots"], 2)

    def test_two_readings_for_one_slot_count_once_with_traceable_winner(self):
        observations = [
            _obs("dup-late", "s", "2026-07-01T00:00:30+00:00"),
            _obs("dup-exact", "s", "2026-07-01T00:00:00+00:00"),
        ]
        result = audit_coverage(
            _payload(observations), self._hourly_schedule(),
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T01:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        sensor = result["sensors"][0]
        self.assertEqual(sensor["covered_slots"], 1)
        covered = [slot for slot in sensor["slots"]
                   if slot["status"] == "covered"]
        self.assertEqual(covered[0]["observation_id"], "dup-exact")
        # The duplicate contributes no coverage and is reported separately.
        self.assertEqual(sensor["off_schedule_observation_ids"], ["dup-late"])
        self.assertEqual(
            [row["observation_id"] for row in
             result["off_schedule_observations"]], ["dup-late"])

    def test_equal_deviation_tie_broken_by_observation_id_stably(self):
        observations = [
            _obs("z-reading", "s", "2026-07-01T00:00:05+00:00"),
            _obs("a-reading", "s", "2026-07-01T00:00:05+00:00"),
        ]
        def run(rows):
            return audit_coverage(
                _payload(rows), self._hourly_schedule(),
                start="2026-07-01T00:00:00+00:00",
                end="2026-07-01T01:00:00+00:00",
                tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        first = run(observations)
        second = run(list(reversed(observations)))
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        covered = [slot for slot in first["sensors"][0]["slots"]
                   if slot["status"] == "covered"]
        self.assertEqual(covered[0]["observation_id"], "a-reading")
        self.assertEqual(
            first["sensors"][0]["off_schedule_observation_ids"],
            ["z-reading"])

    def test_out_of_tolerance_reading_is_off_schedule_and_slot_missing(self):
        observations = [_obs("late", "s",
                             "2026-07-01T00:05:00+00:00")]  # 300 s off
        result = audit_coverage(
            _payload(observations), self._hourly_schedule(),
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T01:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        sensor = result["sensors"][0]
        self.assertEqual(sensor["covered_slots"], 0)
        self.assertEqual(sensor["missing_slots"], 1)
        missing = [slot for slot in sensor["slots"]
                   if slot["status"] == "missing"]
        self.assertEqual(missing[0]["observation_id"], None)
        self.assertEqual(sensor["off_schedule_observation_ids"], ["late"])

    def test_one_observation_never_covers_two_slots(self):
        # With tolerance below half a cadence the reading is eligible for the
        # nearest slot only; it cannot cover two neighboring slots.
        observations = [_obs("near-first", "s",
                             "2026-07-01T00:00:40+00:00")]
        result = audit_coverage(
            _payload(observations), self._hourly_schedule(),
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T02:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        sensor = result["sensors"][0]
        self.assertEqual(sensor["covered_slots"], 1)
        statuses = [slot["status"] for slot in sensor["slots"]]
        self.assertEqual(sorted(statuses), ["covered", "missing"])


class ValidationTests(unittest.TestCase):
    def _schedule(self):
        return _schedule([_plan(
            "s", "2026-07-01T00:00:00+00:00",
            "2026-07-01T04:00:00+00:00", CADENCE,
            "2026-07-01T00:00:00+00:00")])

    def _kwargs(self, **overrides):
        values = dict(start="2026-07-01T00:00:00+00:00",
                      end="2026-07-01T04:00:00+00:00",
                      tolerance_seconds=TOLERANCE, minimum_coverage=0.8)
        values.update(overrides)
        return values

    def test_rejects_tolerance_at_or_above_half_cadence(self):
        for tolerance in (300, 301, 600):
            with self.assertRaisesRegex(CoverageError, "half the cadence"):
                audit_coverage(_payload([]), self._schedule(),
                               **self._kwargs(tolerance_seconds=tolerance))
        # The strict boundary: cadence / 2 - 1 is accepted.
        audit_coverage(_payload([]), self._schedule(),
                       **self._kwargs(tolerance_seconds=299))

    def test_rejects_observation_without_plan(self):
        payload = _payload([_obs("x", "ghost",
                                 "2026-07-01T00:00:00+00:00")])
        with self.assertRaisesRegex(CoverageError, "no sampling plan"):
            audit_coverage(payload, self._schedule(), **self._kwargs())

    def test_rejects_duplicate_sensor_plans(self):
        schedule = _schedule([
            _plan("s", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T04:00:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
            _plan("s", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T04:00:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
        ])
        with self.assertRaisesRegex(CoverageError, "duplicate sampling plan"):
            audit_coverage(_payload([]), schedule, **self._kwargs())

    def test_rejects_bad_cadence_and_deployment_interval(self):
        for cadence in (0, -600, 600.0, True):
            schedule = _schedule([_plan(
                "s", "2026-07-01T00:00:00+00:00",
                "2026-07-01T04:00:00+00:00", cadence,
                "2026-07-01T00:00:00+00:00")])
            with self.assertRaises(CoverageError):
                audit_coverage(_payload([]), schedule, **self._kwargs())
        schedule = _schedule([_plan(
            "s", "2026-07-01T04:00:00+00:00",
            "2026-07-01T00:00:00+00:00", CADENCE,
            "2026-07-01T00:00:00+00:00")])
        with self.assertRaisesRegex(CoverageError, "active_start"):
            audit_coverage(_payload([]), schedule, **self._kwargs())

    def test_rejects_timestamps_without_offset(self):
        # Offset-free observation timestamps are rejected explicitly (the
        # clustering validation raises ValueError, CoverageError's parent).
        good = _obs("g", "s", "2026-07-01T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "offset"):
            audit_coverage(
                _payload([_obs("x", "s", "2026-07-01T00:00:00")] + [good]),
                self._schedule(), **self._kwargs())
        schedule = _schedule([_plan(
            "s", "2026-07-01T00:00:00",
            "2026-07-01T04:00:00+00:00", HOURLY_CADENCE,
            "2026-07-01T00:00:00+00:00")])
        with self.assertRaisesRegex(CoverageError, "offset"):
            audit_coverage(_payload([good]), schedule, **self._kwargs())
        with self.assertRaisesRegex(CoverageError, "offset"):
            audit_coverage(_payload([good]), self._schedule(),
                           **self._kwargs(start="2026-07-01T00:00:00"))

    def test_rejects_minimum_coverage_outside_unit_interval(self):
        for value in (-0.1, 1.1):
            with self.assertRaisesRegex(CoverageError, "minimum_coverage"):
                audit_coverage(_payload([]), self._schedule(),
                               **self._kwargs(minimum_coverage=value))

    def test_rejects_inverted_audit_window(self):
        with self.assertRaisesRegex(CoverageError, "audit start"):
            audit_coverage(
                _payload([]), self._schedule(),
                **self._kwargs(start="2026-07-01T04:00:00+00:00",
                               end="2026-07-01T00:00:00+00:00"))

    def test_rejects_missing_schedule_fields(self):
        base = _plan("s", "2026-07-01T00:00:00+00:00",
                     "2026-07-01T04:00:00+00:00", CADENCE,
                     "2026-07-01T00:00:00+00:00")
        for field in ("sensor_id", "active_start", "active_end",
                      "cadence_seconds", "phase"):
            broken = dict(base)
            del broken[field]
            with self.assertRaisesRegex(CoverageError, "missing field"):
                audit_coverage(_payload([]), _schedule([broken]),
                               **self._kwargs())


class EmptyAndReorderTests(unittest.TestCase):
    def test_no_expected_slots_returns_null_empty_state(self):
        schedule = _schedule([_plan(
            "s", "2026-08-01T00:00:00+00:00",
            "2026-08-01T04:00:00+00:00", CADENCE,
            "2026-08-01T00:00:00+00:00")])
        # The reading exists while the sensor is outside its deployment, so
        # it still clusters into an incident but no slot is expected.
        observations = [_obs("lone", "s", "2026-07-01T00:00:00+00:00")]
        result = audit_coverage(
            _payload(observations), schedule,
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T04:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=0.8)
        sensor = result["sensors"][0]
        self.assertEqual(sensor["expected_slots"], 0)
        self.assertIsNone(sensor["coverage_ratio"])
        self.assertEqual(sensor["slots"], [])
        self.assertEqual(sensor["off_schedule_observation_ids"], ["lone"])
        self.assertIsNone(result["coverage"]["coverage_ratio"])
        incident = result["incidents"][0]
        verdict = incident["sampling_coverage"]
        self.assertEqual(verdict["status"], "no_expected_samples")
        self.assertIsNone(verdict["coverage_ratio"])
        self.assertEqual(verdict["eligible_slots"], 0)

    def test_input_reordering_does_not_change_conclusion(self):
        windows = [{"sensor_id": "s-b",
                    "start": "2026-07-01T00:10:00+00:00",
                    "end": "2026-07-01T00:20:00+00:00"}]
        observations = [
            _obs("a2", "s-a", "2026-07-01T00:10:00+00:00", lon=0.001),
            _obs("b1", "s-b", "2026-07-01T00:00:00+00:00"),
            _obs("a1", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("a1-dup", "s-a", "2026-07-01T00:00:10+00:00"),
            _obs("rogue", "s-a", "2026-07-01T00:07:00+00:00"),
        ]
        schedule_entries = [
            _plan("s-b", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T00:30:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
            _plan("s-a", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T00:30:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
        ]
        kwargs = dict(start="2026-07-01T00:00:00+00:00",
                      end="2026-07-01T00:30:00+00:00",
                      tolerance_seconds=TOLERANCE, minimum_coverage=0.8)
        first = audit_coverage(
            _payload(observations, windows),
            _schedule(schedule_entries), **kwargs)
        shuffled = list(observations)
        shuffled.reverse()
        second = audit_coverage(
            _payload(shuffled, list(reversed(windows))),
            _schedule(list(reversed(schedule_entries))), **kwargs)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))


class IncidentLinkageTests(unittest.TestCase):
    def _two_sensor_schedule(self):
        return _schedule([
            _plan("s-a", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T01:00:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
            _plan("s-b", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T01:00:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
        ])

    def _kwargs(self):
        return dict(start="2026-07-01T00:00:00+00:00",
                    end="2026-07-01T01:00:00+00:00",
                    tolerance_seconds=TOLERANCE, minimum_coverage=0.9)

    def test_silent_sensor_keeps_incident_and_marks_incomplete(self):
        # Co-located readings within the clustering time window form one
        # incident spanning [00:00, 00:10:00.000001). Sensor s-b never
        # reports its 00:10 slot.
        observations = [
            _obs("a1", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("a2", "s-a", "2026-07-01T00:10:00+00:00"),
            _obs("b1", "s-b", "2026-07-01T00:00:00+00:00"),
        ]
        payload = _payload(observations)
        result = audit_coverage(payload, self._two_sensor_schedule(),
                                **self._kwargs())
        # The clustering output itself is untouched.
        self.assertEqual(result["cluster_report"], cluster(payload))
        self.assertEqual(len(result["incidents"]), 1)
        incident = result["incidents"][0]
        self.assertEqual(incident["observation_ids"], ["a1", "a2", "b1"])
        self.assertEqual(incident["incident_id"], "a1")
        self.assertEqual(incident["confidence"],
                         cluster(payload)["incidents"][0]["confidence"])
        self.assertEqual(incident["mean_severity"],
                         cluster(payload)["incidents"][0]["mean_severity"])
        self.assertEqual(
            incident["event_window"],
            {"start": "2026-07-01T00:00:00+00:00",
             "end": "2026-07-01T00:10:00.000001+00:00"})
        verdict = incident["sampling_coverage"]
        self.assertEqual(verdict["status"], "incomplete")
        self.assertEqual(verdict["eligible_slots"], 4)
        self.assertEqual(verdict["covered_slots"], 3)
        self.assertEqual(verdict["missing_slots"], 1)
        self.assertEqual(verdict["coverage_ratio"], 0.75)
        self.assertEqual(verdict["missing_slot_details"],
                         [{"sensor_id": "s-b",
                           "slot_time": "2026-07-01T00:10:00+00:00"}])
        # The sensor-level gap statistics tell the same story over the full
        # audit window: s-b's other 00:20..00:50 slots are unobserved too.
        sensor_b = next(row for row in result["sensors"]
                        if row["sensor_id"] == "s-b")
        self.assertEqual(sensor_b["expected_slots"], 6)
        self.assertEqual(sensor_b["covered_slots"], 1)
        self.assertEqual(sensor_b["missing_slots"], 5)

    def test_filling_the_gap_flips_verdict_and_syncs_sensor_stats(self):
        base = [
            _obs("a1", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("a2", "s-a", "2026-07-01T00:10:00+00:00"),
            _obs("b1", "s-b", "2026-07-01T00:00:00+00:00"),
        ]
        before = audit_coverage(_payload(base),
                                self._two_sensor_schedule(), **self._kwargs())
        self.assertEqual(before["incidents"][0]["sampling_coverage"]["status"],
                         "incomplete")

        filled = base + [_obs("b2", "s-b", "2026-07-01T00:10:00+00:00")]
        after = audit_coverage(_payload(filled),
                               self._two_sensor_schedule(), **self._kwargs())
        # Clustering remains the source of truth for incident content.
        self.assertEqual(after["cluster_report"], cluster(_payload(filled)))
        verdict = after["incidents"][0]["sampling_coverage"]
        self.assertEqual(verdict["status"], "sufficient")
        self.assertEqual(verdict["covered_slots"], 4)
        self.assertEqual(verdict["missing_slots"], 0)
        self.assertEqual(verdict["coverage_ratio"], 1.0)
        self.assertEqual(verdict["missing_slot_details"], [])
        sensor_b = next(row for row in after["sensors"]
                        if row["sensor_id"] == "s-b")
        self.assertEqual(sensor_b["covered_slots"], 2)
        self.assertEqual(sensor_b["missing_slots"], 4)
        # The unchanged run still has its gap (one fewer covered slot).
        sensor_b_before = next(row for row in before["sensors"]
                               if row["sensor_id"] == "s-b")
        self.assertEqual(sensor_b_before["missing_slots"], 5)
        self.assertEqual(sensor_b_before["covered_slots"], 1)

    def test_single_moment_incident_includes_that_moment_slot(self):
        observations = [_obs("a1", "s-a",
                             "2026-07-01T00:00:00+00:00")]
        result = audit_coverage(_payload(observations),
                                self._two_sensor_schedule(), **self._kwargs())
        verdict = result["incidents"][0]["sampling_coverage"]
        self.assertEqual(
            result["incidents"][0]["event_window"]["end"],
            "2026-07-01T00:00:00.000001+00:00")
        # The one-microsecond range still admits both networks' slots at the
        # exact moment; s-a covers its own while s-b stays silent.
        self.assertEqual(verdict["eligible_slots"], 2)
        self.assertEqual(verdict["covered_slots"], 1)
        self.assertEqual(verdict["missing_slots"], 1)
        self.assertEqual(verdict["coverage_ratio"], 0.5)
        self.assertEqual(verdict["status"], "incomplete")
        self.assertEqual(verdict["missing_slot_details"],
                         [{"sensor_id": "s-b",
                           "slot_time": "2026-07-01T00:00:00+00:00"}])

    def test_incident_outside_audit_window_has_no_expected_samples(self):
        observations = [_obs("a1", "s-a",
                             "2026-07-01T00:00:00+00:00")]
        result = audit_coverage(
            _payload(observations), self._two_sensor_schedule(),
            start="2026-08-01T00:00:00+00:00",
            end="2026-08-01T01:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=0.9)
        verdict = result["incidents"][0]["sampling_coverage"]
        self.assertEqual(verdict["status"], "no_expected_samples")
        self.assertIsNone(verdict["coverage_ratio"])

    def test_covered_slot_details_trace_observation_ids(self):
        observations = [
            _obs("a1", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("b1", "s-b", "2026-07-01T00:00:05+00:00"),
        ]
        result = audit_coverage(_payload(observations),
                                self._two_sensor_schedule(), **self._kwargs())
        for sensor in result["sensors"]:
            for slot in sensor["slots"]:
                if slot["status"] == "covered":
                    self.assertIsInstance(slot["observation_id"], str)
                else:
                    self.assertIsNone(slot["observation_id"])
        sensor_b = next(row for row in result["sensors"]
                        if row["sensor_id"] == "s-b")
        covered = [slot for slot in sensor_b["slots"]
                   if slot["status"] == "covered"]
        self.assertEqual(covered[0]["observation_id"], "b1")
        # Every covered slot time is a UTC-aware instant parseable back.
        parsed = datetime.fromisoformat(covered[0]["slot_time"])
        self.assertEqual(parsed.tzinfo.utcoffset(parsed),
                         timezone.utc.utcoffset(parsed))


class ThresholdAndNetworkTests(unittest.TestCase):
    def _schedule(self):
        return _schedule([
            _plan("s-a", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T00:20:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
            _plan("s-b", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T00:20:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
        ])

    def _kwargs(self, minimum):
        return dict(start="2026-07-01T00:00:00+00:00",
                    end="2026-07-01T00:20:00+00:00",
                    tolerance_seconds=TOLERANCE, minimum_coverage=minimum)

    def test_ratio_equal_to_threshold_is_sufficient(self):
        # Event spans to 00:10: three covered slots out of four (s-b's
        # 00:10 slot misses), so the incident ratio is exactly 0.75.
        observations = [
            _obs("a1", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("a2", "s-a", "2026-07-01T00:10:00+00:00"),
            _obs("b1", "s-b", "2026-07-01T00:00:00+00:00"),
        ]
        exact = audit_coverage(_payload(observations), self._schedule(),
                               **self._kwargs(0.75))
        self.assertEqual(
            exact["incidents"][0]["sampling_coverage"]["coverage_ratio"],
            0.75)
        self.assertEqual(
            exact["incidents"][0]["sampling_coverage"]["status"],
            "sufficient")
        below = audit_coverage(_payload(observations), self._schedule(),
                               **self._kwargs(0.750001))
        self.assertEqual(
            below["incidents"][0]["sampling_coverage"]["status"],
            "incomplete")
        # Threshold zero makes any eligible denominator sufficient.
        zero = audit_coverage(_payload(observations), self._schedule(),
                              **self._kwargs(0.0))
        self.assertEqual(
            zero["incidents"][0]["sampling_coverage"]["status"],
            "sufficient")

    def test_two_incidents_get_independent_event_ranges_and_verdicts(self):
        # Far-apart locations form two separate incidents; the 60-minute
        # window keeps the two s-a readings together.
        observations = [
            _obs("a1", "s-a", "2026-07-01T00:00:00+00:00", lat=10.0),
            _obs("a2", "s-a", "2026-07-01T00:10:00+00:00", lat=10.0),
            _obs("b1", "s-b", "2026-07-01T00:00:00+00:00", lat=-10.0),
        ]
        result = audit_coverage(
            _payload(observations, radius=1.0, window_minutes=60),
            self._schedule(), **self._kwargs(0.9))
        self.assertEqual(len(result["incidents"]), 2)
        by_id = {row["incident_id"]: row for row in result["incidents"]}
        # Incident a1/a2 spans [00:00, 00:10:00.000001): four network slots,
        # three covered and s-b's 00:10 slot missing.
        verdict_a = by_id["a1"]["sampling_coverage"]
        self.assertEqual(verdict_a["eligible_slots"], 4)
        self.assertEqual(verdict_a["covered_slots"], 3)
        self.assertEqual(verdict_a["missing_slots"], 1)
        self.assertEqual(verdict_a["status"], "incomplete")
        # Single-moment incident b1 covers only the 00:00 instant range;
        # coverage is network-wide, so both sensors' 00:00 slots count.
        verdict_b = by_id["b1"]["sampling_coverage"]
        self.assertEqual(verdict_b["eligible_slots"], 2)
        self.assertEqual(verdict_b["covered_slots"], 2)
        self.assertEqual(verdict_b["missing_slots"], 0)

    def test_maintenance_slot_inside_event_range_excluded_from_verdict(self):
        windows = [{"sensor_id": "s-b",
                    "start": "2026-07-01T00:10:00+00:00",
                    "end": "2026-07-01T00:20:00+00:00"}]
        observations = [
            _obs("a1", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("a2", "s-a", "2026-07-01T00:10:00+00:00"),
            _obs("b1", "s-b", "2026-07-01T00:00:00+00:00"),
        ]
        result = audit_coverage(
            _payload(observations, windows), self._schedule(),
            **self._kwargs(1.0))
        verdict = result["incidents"][0]["sampling_coverage"]
        # Range [00:00, 00:10:00.000001) covers four slots; s-b's 00:10
        # slot is a planned outage and leaves the denominator, so all three
        # eligible slots are covered.
        self.assertEqual(verdict["expected_slots"], 4)
        self.assertEqual(verdict["planned_outage_slots"], 1)
        self.assertEqual(verdict["eligible_slots"], 3)
        self.assertEqual(verdict["covered_slots"], 3)
        self.assertEqual(verdict["missing_slots"], 0)
        self.assertEqual(verdict["status"], "sufficient")
        self.assertEqual(verdict["missing_slot_details"], [])

    def test_non_utc_offsets_denote_same_instants(self):
        # 05:00+05:00 is the same instant as 00:00Z.
        observations = [
            _obs("a1", "s-a", "2026-07-01T05:00:00+05:00"),
            _obs("b1", "s-b", "2026-07-01T02:00:00+02:00"),
        ]
        schedule = _schedule([
            _plan("s-a", "2026-07-01T00:00:00Z",
                  "2026-07-01T00:20:00Z", CADENCE,
                  "2026-06-30T19:00:00-05:00"),
            _plan("s-b", "2026-07-01T00:00:00+00:00",
                  "2026-07-01T00:20:00+00:00", CADENCE,
                  "2026-07-01T00:00:00+00:00"),
        ])
        result = audit_coverage(
            _payload(observations), schedule,
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T00:20:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        for sensor in result["sensors"]:
            self.assertEqual(sensor["covered_slots"], 1)
            covered = [slot for slot in sensor["slots"]
                       if slot["status"] == "covered"]
            self.assertEqual(covered[0]["slot_time"],
                             "2026-07-01T00:00:00+00:00")

    def test_reading_outside_audit_window_is_off_schedule(self):
        observations = [
            _obs("inside", "s-a", "2026-07-01T00:00:00+00:00"),
            _obs("outside", "s-a", "2026-07-02T00:00:00+00:00"),
        ]
        result = audit_coverage(
            _payload(observations), self._schedule(),
            **self._kwargs(1.0))
        sensor_a = next(row for row in result["sensors"]
                        if row["sensor_id"] == "s-a")
        self.assertEqual(sensor_a["off_schedule_observation_ids"],
                         ["outside"])
        self.assertEqual(
            [row["observation_id"]
             for row in result["off_schedule_observations"]],
            ["outside"])

    def test_odd_cadence_half_period_boundary(self):
        schedule = _schedule([_plan(
            "s-a", "2026-07-01T00:00:00+00:00",
            "2026-07-01T01:00:00+00:00", 599,
            "2026-07-01T00:00:00+00:00")])
        kwargs = dict(start="2026-07-01T00:00:00+00:00",
                      end="2026-07-01T01:00:00+00:00",
                      minimum_coverage=1.0)
        audit_coverage(_payload([]), schedule,
                       tolerance_seconds=299, **kwargs)
        with self.assertRaisesRegex(CoverageError, "half the cadence"):
            audit_coverage(_payload([]), schedule,
                           tolerance_seconds=300, **kwargs)

    def test_empty_plan_without_observations_audits_as_empty(self):
        result = audit_coverage(
            _payload([]), _schedule([]),
            start="2026-07-01T00:00:00+00:00",
            end="2026-07-01T01:00:00+00:00",
            tolerance_seconds=TOLERANCE, minimum_coverage=1.0)
        self.assertEqual(result["sensors"], [])
        self.assertEqual(result["coverage"]["eligible_slots"], 0)
        self.assertIsNone(result["coverage"]["coverage_ratio"])
        self.assertEqual(result["incidents"], [])


if __name__ == "__main__":
    unittest.main()
