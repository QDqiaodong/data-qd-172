import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wildfire.cli import main
from wildfire.core import cluster


def _obs(oid, sensor, time, lat=0.0, lon=0.0, severity=0.5,
         uncertainty=None):
    row = {"id": oid, "sensor": sensor, "time": time,
           "lat": lat, "lon": lon, "severity": severity}
    if uncertainty is not None:
        row["location_uncertainty_m"] = uncertainty
    return row


def _payload():
    return {"radius_km": 1.0, "window_minutes": 60,
            "observations": [
                _obs("a", "s-a", "2026-07-01T00:00:00+00:00", lon=0.0),
                _obs("b", "s-b", "2026-07-01T00:05:00+00:00", lon=0.001,
                     uncertainty=300),
                _obs("c", "s-c", "2026-07-01T00:10:00+00:00", lon=0.011,
                     uncertainty=300),
                _obs("d", "s-d", "2026-07-01T00:15:00+00:00", lon=0.012,
                     uncertainty=300),
                _obs("e", "s-e", "2026-07-01T00:20:00+00:00", lon=0.0225,
                     uncertainty=300),
                _obs("outage", "s-b", "2026-07-01T00:30:00+00:00",
                     lon=0.001),
            ],
            "maintenance_windows": [
                {"sensor_id": "s-b",
                 "start": "2026-07-01T00:30:00+00:00",
                 "end": "2026-07-01T00:40:00+00:00"}]}


class LocationAuditCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.input_path = root / "observations.json"
        self.output_path = root / "run" / "location-audit.json"
        self.input_path.write_text(json.dumps(_payload()))

    def _run(self, *extra, argv=None):
        if argv is None:
            argv = ["location-audit",
                    "--input", str(self.input_path),
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
        self.assertEqual(summary, {"observations": 6, "incidents": 3,
                                   "certain_components": 3,
                                   "possible_components": 1})

        report = json.loads(self.output_path.read_text())
        self.assertEqual(report["radius_km"], 1.0)
        self.assertEqual(report["window_minutes"], 60)
        # The cluster report is embedded verbatim, including suppression.
        self.assertEqual(
            report["cluster_report"]["suppressed_observation_ids"],
            ["outage"])
        self.assertEqual([i["observation_ids"]
                          for i in report["cluster_report"]["incidents"]],
                         [["a", "b"], ["c", "d"], ["e"]])
        self.assertEqual(
            report["certain_components"],
            [{"component_id": "a", "observation_ids": ["a", "b"]},
             {"component_id": "c", "observation_ids": ["c", "d"]},
             {"component_id": "e", "observation_ids": ["e"]}])
        self.assertEqual(
            report["possible_components"],
            [{"component_id": "a",
              "observation_ids": ["a", "b", "c", "d", "e"]}])
        # The suppressed observation takes no part in the audit.
        listed = {row["observation_id"] for row in report["observations"]}
        self.assertEqual(listed, {"a", "b", "c", "d", "e"})
        by_id = {row["observation_id"]: row
                 for row in report["observations"]}
        self.assertEqual(by_id["c"],
                         {"observation_id": "c",
                          "incident_id": "c",
                          "location_uncertainty_m": 300.0,
                          "certain_component_id": "c",
                          "possible_component_id": "a"})
        # Keys are emitted in stable sorted order.
        raw = self.output_path.read_text()
        self.assertEqual(raw, json.dumps(json.loads(raw),
                                         indent=2, sort_keys=True) + "\n")

    def test_audit_reuses_exclusive_create_output_behavior(self):
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        code, _, stderr = self._run()
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)

    def test_audit_rejects_invalid_uncertainty_without_output(self):
        payload = _payload()
        payload["observations"][0]["location_uncertainty_m"] = -1
        self.input_path.write_text(json.dumps(payload))
        code, _, stderr = self._run()
        self.assertEqual(code, 2)
        self.assertIn("location_uncertainty_m", stderr)
        self.assertFalse(self.output_path.exists())

    def test_audit_malformed_json_exits_2(self):
        self.input_path.write_text("{not json")
        code, _, stderr = self._run()
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)
        self.assertFalse(self.output_path.exists())

    def test_legacy_cluster_invocation_remains_fully_compatible(self):
        # Uncertainty fields in the input must not change the legacy
        # clustering command, its summary, or its report format.
        legacy_output = Path(self.tmp.name) / "legacy-report.json"
        code, stdout, stderr = self._run(
            argv=["--input", str(self.input_path),
                  "--output", str(legacy_output)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout),
                         {"incidents": 3, "observations": 6})
        report = json.loads(legacy_output.read_text())
        self.assertEqual(set(report),
                         {"incidents", "observation_count",
                          "suppressed_observation_ids"})
        stripped = _payload()
        for row in stripped["observations"]:
            row.pop("location_uncertainty_m", None)
        self.assertEqual(report, cluster(stripped))


if __name__ == "__main__":
    unittest.main()
