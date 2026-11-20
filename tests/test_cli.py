import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from wildfire.cli import main


def _bridge_payload():
    return {"radius_km": 1.0, "window_minutes": 0,
            "observations": [
                {"id": "a", "sensor": "s-a", "time": "2026-07-01T00:00:00+00:00", "lat": 0.0, "lon": 0.0, "severity": 0.2},
                {"id": "b", "sensor": "s-b", "time": "2026-07-01T00:00:00+00:00", "lat": 0.0, "lon": 0.006, "severity": 0.9},
                {"id": "c", "sensor": "s-c", "time": "2026-07-01T00:00:00+00:00", "lat": 0.0, "lon": 0.012, "severity": 0.4}],
            "maintenance_windows": [{"sensor_id": "s-b",
                                     "start": "2026-06-30T23:00:00+00:00",
                                     "end": "2026-07-01T01:00:00+00:00"}]}


def _run_cli(payload):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    src, out = root / "input.json", root / "run" / "report.json"
    src.write_text(json.dumps(payload))
    argv = ["fire-cluster", "--input", str(src), "--output", str(out)]
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            main()
        except SystemExit as exc:
            return tmp, out, exc.code, stdout.getvalue(), stderr.getvalue()
    return tmp, out, 0, stdout.getvalue(), stderr.getvalue()


class CliTests(unittest.TestCase):
    def test_cli_report_and_summary_with_suppression(self):
        tmp, out, code, stdout, _ = _run_cli(_bridge_payload())
        self.addCleanup(tmp.cleanup)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"incidents": 2, "observations": 3})
        report = json.loads(out.read_text())
        self.assertEqual(report["observation_count"], 3)
        self.assertEqual(report["suppressed_observation_ids"], ["b"])
        self.assertEqual([i["observation_ids"] for i in report["incidents"]], [["a"], ["c"]])

    def test_cli_without_windows_keeps_legacy_output(self):
        payload = _bridge_payload()
        del payload["maintenance_windows"]
        tmp, out, code, stdout, _ = _run_cli(payload)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"incidents": 1, "observations": 3})
        report = json.loads(out.read_text())
        self.assertEqual(report["suppressed_observation_ids"], [])
        self.assertEqual([i["observation_ids"] for i in report["incidents"]], [["a", "b", "c"]])

    def test_cli_rejects_invalid_window_with_exit_2(self):
        payload = _bridge_payload()
        payload["maintenance_windows"] = [{"sensor_id": "s-b",
                                           "start": "2026-07-01T01:00:00+00:00",
                                           "end": "2026-07-01T01:00:00+00:00"}]
        tmp, out, code, _, stderr = _run_cli(payload)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
