import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wildfire.cli import main


def _obs(oid, sensor, time, lat=0.0, lon=0.0, severity=0.5):
    return {"id": oid, "sensor": sensor, "time": time,
            "lat": lat, "lon": lon, "severity": severity}


def _payload():
    return {"radius_km": 1.0, "window_minutes": 60,
            "observations": [
                _obs("a1", "s-a", "2026-07-01T00:00:00+00:00"),
                _obs("a2", "s-a", "2026-07-01T00:10:00+00:00"),
                _obs("b1", "s-b", "2026-07-01T00:00:00+00:00"),
                _obs("b-outage", "s-b",
                     "2026-07-01T00:30:00+00:00"),
            ],
            "maintenance_windows": [
                {"sensor_id": "s-b",
                 "start": "2026-07-01T00:30:00+00:00",
                 "end": "2026-07-01T00:40:00+00:00"}]}


def _schedule(cadence=600):
    return {"sensors": [
        {"sensor_id": "s-a",
         "active_start": "2026-07-01T00:00:00+00:00",
         "active_end": "2026-07-01T01:00:00+00:00",
         "cadence_seconds": cadence,
         "phase": "2026-07-01T00:00:00+00:00"},
        {"sensor_id": "s-b",
         "active_start": "2026-07-01T00:00:00+00:00",
         "active_end": "2026-07-01T01:00:00+00:00",
         "cadence_seconds": cadence,
         "phase": "2026-07-01T00:00:00+00:00"},
    ]}


class CoverageAuditCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.input_path = root / "observations.json"
        self.schedule_path = root / "schedule.json"
        self.output_path = root / "run" / "coverage.json"
        self.input_path.write_text(json.dumps(_payload()))
        self.schedule_path.write_text(json.dumps(_schedule()))

    def _run(self, *extra):
        argv = ["coverage-audit",
                "--input", str(self.input_path),
                "--schedule", str(self.schedule_path),
                "--start", "2026-07-01T00:00:00+00:00",
                "--end", "2026-07-01T01:00:00+00:00",
                "--tolerance-seconds", "60",
                "--minimum-coverage", "0.9",
                "--output", str(self.output_path), *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["fire-cluster", *argv]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            try:
                main()
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        return code, stdout.getvalue(), stderr.getvalue()

    def test_audit_writes_full_report_and_summary(self):
        code, stdout, stderr = self._run()
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["observations"], 4)
        self.assertEqual(summary["incidents"], 1)
        self.assertEqual(summary["eligible_slots"], 11)
        self.assertGreater(summary["missing_slots"], 0)

        report = json.loads(self.output_path.read_text())
        self.assertEqual(report["audit_window"],
                         {"start": "2026-07-01T00:00:00+00:00",
                          "end": "2026-07-01T01:00:00+00:00"})
        self.assertEqual(report["tolerance_seconds"], 60)
        self.assertEqual(report["minimum_coverage"], 0.9)
        self.assertEqual([row["sensor_id"] for row in report["sensors"]],
                         ["s-a", "s-b"])
        # The cluster report is embedded verbatim, including suppression.
        self.assertEqual(
            report["cluster_report"]["suppressed_observation_ids"],
            ["b-outage"])
        self.assertEqual(
            report["cluster_report"]["incidents"][0]["observation_ids"],
            ["a1", "a2", "b1"])
        # Incident content is untouched while the verdict is attached.
        incident = report["incidents"][0]
        self.assertEqual(incident["observation_ids"], ["a1", "a2", "b1"])
        self.assertIn("sampling_coverage", incident)
        self.assertEqual(incident["sampling_coverage"]["status"],
                         "incomplete")
        # The maintenance slot of s-b is an outage, not a missing slot.
        sensor_b = next(row for row in report["sensors"]
                        if row["sensor_id"] == "s-b")
        outage = [slot for slot in sensor_b["slots"]
                  if slot["status"] == "planned_outage"]
        self.assertEqual([slot["slot_time"] for slot in outage],
                         ["2026-07-01T00:30:00+00:00"])
        missing = [slot for slot in sensor_b["slots"]
                   if slot["status"] == "missing"]
        self.assertNotIn("2026-07-01T00:30:00+00:00",
                         [slot["slot_time"] for slot in missing])
        # The maintenance-period reading neither covers a slot nor appears
        # in the off-schedule list.
        off_schedule = [row["observation_id"]
                        for row in report["off_schedule_observations"]]
        self.assertNotIn("b-outage", off_schedule)
        self.assertNotIn("b-outage",
                         sensor_b["off_schedule_observation_ids"])
        for slot in sensor_b["slots"]:
            self.assertNotEqual(slot["observation_id"], "b-outage")

    def test_audit_reuses_exclusive_create_output_behavior(self):
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        code, _, stderr = self._run()
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)

    def test_audit_half_cadence_violation_exits_2_without_output(self):
        self.schedule_path.write_text(json.dumps(_schedule(cadence=100)))
        code, _, stderr = self._run()
        self.assertEqual(code, 2)
        self.assertIn("half the cadence", stderr)
        self.assertFalse(self.output_path.exists())

    def test_audit_rejects_unplanned_sensor_without_output(self):
        payload = _payload()
        payload["observations"].append(
            _obs("ghost", "s-mystery", "2026-07-01T00:00:00+00:00"))
        self.input_path.write_text(json.dumps(payload))
        code, _, stderr = self._run()
        self.assertEqual(code, 2)
        self.assertIn("no sampling plan", stderr)
        self.assertFalse(self.output_path.exists())

    def test_audit_malformed_json_exits_2(self):
        self.schedule_path.write_text("{not json")
        code, _, stderr = self._run()
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)
        self.assertFalse(self.output_path.exists())

    def test_legacy_cluster_invocation_remains_fully_compatible(self):
        # No coverage parameters: the legacy command shape and file format
        # are unchanged when maintenance windows are absent.
        payload = _payload()
        del payload["maintenance_windows"]
        legacy_input = Path(self.tmp.name) / "legacy.json"
        legacy_output = Path(self.tmp.name) / "legacy-report.json"
        legacy_input.write_text(json.dumps(payload))
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["fire-cluster", "--input", str(legacy_input),
                "--output", str(legacy_output)]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            try:
                main()
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()),
                         {"incidents": 1, "observations": 4})
        report = json.loads(legacy_output.read_text())
        self.assertEqual(set(report),
                         {"incidents", "observation_count",
                          "suppressed_observation_ids"})
        self.assertEqual(report["suppressed_observation_ids"], [])


if __name__ == "__main__":
    unittest.main()
