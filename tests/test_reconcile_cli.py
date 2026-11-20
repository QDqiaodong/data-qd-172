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


def _primary(row_id, sensor, utc_ms):
    return {"id": row_id, "sensor": sensor, "utc_ms": utc_ms,
            "lat": 10.0, "lon": 20.0, "severity": 0.5}


def _backup(row_id, sensor, clock_ms):
    return {"id": row_id, "sensor": sensor, "clock_ms": clock_ms,
            "lat": 10.0, "lon": 20.0, "severity": 0.5}


def _payload():
    return {"radius_km": 10.0, "window_minutes": 60, "tolerance_ms": 1,
            "primary": [_primary("p1", "S", 100), _primary("p2", "S", 102)],
            "backup": [_backup("b1", "S", 101), _backup("b2", "S", 103),
                       _backup("b3", "W", 500)],
            "anchors": [{"clock_ms": 0, "utc_min_ms": 0, "utc_max_ms": 0}]}


class ReconcileCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src = root / "gateways.json"
        self.reconciled = root / "replay" / "reconciled.json"
        self.report = root / "report.json"
        self.src.write_text(json.dumps(_payload()))

    def _run(self, argv):
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

    def test_reconcile_output_is_direct_cluster_input(self):
        code, stdout, stderr = self._run(
            ["reconcile", "--input", str(self.src),
             "--output", str(self.reconciled)])
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["max_duplicate_pairs"], 2)
        self.assertEqual(summary["included_backup_ids"], ["b3"])
        self.assertEqual(summary["observations"], 3)

        payload = json.loads(self.reconciled.read_text())
        self.assertEqual(
            sorted(row["id"] for row in payload["observations"]),
            ["backup:b3", "primary:p1", "primary:p2"])
        self.assertEqual(payload["radius_km"], 10.0)
        self.assertEqual(payload["window_minutes"], 60)
        self.assertIn("reconciliation", payload)

        # The legacy clustering entry consumes the product unchanged.
        code, _, stderr = self._run(
            ["--input", str(self.reconciled), "--output", str(self.report)])
        self.assertEqual(code, 0, stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["observation_count"], 3)
        self.assertEqual(
            sum(i["observation_count"] for i in report["incidents"]), 3)

    def test_reconcile_reuses_exclusive_create_output_behavior(self):
        code, _, _ = self._run(
            ["reconcile", "--input", str(self.src),
             "--output", str(self.reconciled)])
        self.assertEqual(code, 0)
        code, _, stderr = self._run(
            ["reconcile", "--input", str(self.src),
             "--output", str(self.reconciled)])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)

    def test_reconcile_validation_failure_exits_2_without_output(self):
        bad = self.src.parent / "bad.json"
        broken = _payload()
        broken["anchors"] = [
            {"clock_ms": 0, "utc_min_ms": 9, "utc_max_ms": 8}]
        bad.write_text(json.dumps(broken))
        code, _, stderr = self._run(
            ["reconcile", "--input", str(bad),
             "--output", str(self.reconciled)])
        self.assertEqual(code, 2)
        self.assertIn("inverted", stderr)
        self.assertFalse(self.reconciled.exists())

    def test_reconcile_malformed_json_exits_2(self):
        broken = self.src.parent / "broken.json"
        broken.write_text("{not json")
        code, _, stderr = self._run(
            ["reconcile", "--input", str(broken),
             "--output", str(self.reconciled)])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)
        self.assertFalse(self.reconciled.exists())

    def test_reconciled_payload_matches_library_cluster_call(self):
        code, _, stderr = self._run(
            ["reconcile", "--input", str(self.src),
             "--output", str(self.reconciled)])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(self.reconciled.read_text())
        # Clustering the product must succeed with the existing rules.
        report = cluster(payload)
        self.assertEqual(report["observation_count"], 3)


if __name__ == "__main__":
    unittest.main()
