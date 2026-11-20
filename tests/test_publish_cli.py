import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wildfire.cli import main


def _payload(ids):
    return {"radius_km": 10.0, "window_minutes": 60,
            "observations": [{"id": oid, "sensor": f"s-{oid}",
                              "time": "2026-07-01T00:00:00+00:00",
                              "lat": 0.0, "lon": 0.0, "severity": 0.5}
                             for oid in ids]}


class PublishingCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.archive_path = root / "archive.json"
        self.input1 = root / "input1.json"
        self.input2 = root / "input2.json"
        self.input1.write_text(json.dumps(_payload(["a", "b"])))
        self.input2.write_text(json.dumps(_payload(["a", "b", "c"])))

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

    def test_init_publish_pending_ack_workflow(self):
        code, stdout, stderr = self._run(
            ["archive-init", "--archive", str(self.archive_path),
             "--consumer", "X", "--consumer", "Y"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["consumers"], ["X", "Y"])

        code, stdout, _ = self._run(
            ["publish", "--archive", str(self.archive_path),
             "--input", str(self.input1), "--batch-id", "b1"])
        self.assertEqual(code, 0)
        receipt = json.loads(stdout)
        self.assertEqual(receipt["record_ids"], ["r1"])

        code, stdout, _ = self._run(
            ["publish", "--archive", str(self.archive_path),
             "--input", str(self.input2), "--batch-id", "b2"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["record_ids"], ["r2", "r3"])

        code, stdout, _ = self._run(
            ["pending", "--archive", str(self.archive_path),
             "--consumer", "Y"])
        self.assertEqual(code, 0)
        records = json.loads(stdout)
        self.assertEqual([r["record_id"] for r in records], ["r1", "r2", "r3"])
        self.assertEqual(records[0]["event"]["observation_ids"], ["a", "b"])

        code, stdout, _ = self._run(
            ["ack", "--archive", str(self.archive_path),
             "--consumer", "Y", "--record", "r1"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["acknowledged"])

        code, stdout, _ = self._run(
            ["pending", "--archive", str(self.archive_path),
             "--consumer", "Y"])
        self.assertEqual([r["record_id"] for r in json.loads(stdout)],
                         ["r2", "r3"])
        # Consumer X is unaffected by Y's acknowledgement.
        code, stdout, _ = self._run(
            ["pending", "--archive", str(self.archive_path),
             "--consumer", "X"])
        self.assertEqual([r["record_id"] for r in json.loads(stdout)],
                         ["r1", "r2", "r3"])

    def test_replay_and_conflict_via_cli(self):
        self._run(["archive-init", "--archive", str(self.archive_path),
                   "--consumer", "X"])
        _, first_out, _ = self._run(
            ["publish", "--archive", str(self.archive_path),
             "--input", str(self.input1), "--batch-id", "b1"])
        _, replay_out, _ = self._run(
            ["publish", "--archive", str(self.archive_path),
             "--input", str(self.input1), "--batch-id", "b1"])
        self.assertEqual(json.loads(first_out), json.loads(replay_out))

        code, _, stderr = self._run(
            ["publish", "--archive", str(self.archive_path),
             "--input", str(self.input2), "--batch-id", "b1"])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)

    def test_invalid_input_and_unknown_consumer_exit_2_without_mutation(self):
        self._run(["archive-init", "--archive", str(self.archive_path),
                   "--consumer", "X"])
        before = self.archive_path.read_bytes()
        bad = self.archive_path.parent / "bad.json"
        bad.write_text(json.dumps({"radius_km": -1, "window_minutes": 1,
                                   "observations": []}))
        code, _, stderr = self._run(
            ["publish", "--archive", str(self.archive_path),
             "--input", str(bad), "--batch-id", "bad-batch"])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)
        self.assertEqual(self.archive_path.read_bytes(), before)

        code, _, stderr = self._run(
            ["pending", "--archive", str(self.archive_path),
             "--consumer", "ghost"])
        self.assertEqual(code, 2)
        self.assertIn("unknown consumer", stderr)

    def test_pending_on_missing_archive_exits_2(self):
        code, _, stderr = self._run(
            ["pending", "--archive", str(self.archive_path),
             "--consumer", "X"])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)

    def test_malformed_input_json_exits_2_without_mutation(self):
        self._run(["archive-init", "--archive", str(self.archive_path),
                   "--consumer", "X"])
        before = self.archive_path.read_bytes()
        broken = self.archive_path.parent / "broken.json"
        broken.write_text("{not json")
        code, _, stderr = self._run(
            ["publish", "--archive", str(self.archive_path),
             "--input", str(broken), "--batch-id", "broken"])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)
        self.assertEqual(self.archive_path.read_bytes(), before)


class LegacyInvocationTests(unittest.TestCase):
    def test_legacy_fire_cluster_invocation_unchanged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        src = root / "input.json"
        out = root / "run" / "report.json"
        src.write_text(json.dumps(_payload(["a", "b"])))

        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["fire-cluster", "--input", str(src), "--output", str(out)]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            main()
        self.assertEqual(json.loads(stdout.getvalue()),
                         {"incidents": 1, "observations": 2})
        report = json.loads(out.read_text())
        self.assertEqual(report["incidents"][0]["observation_ids"], ["a", "b"])
        self.assertEqual(report["suppressed_observation_ids"], [])

        # Legacy exclusive-create behaviour is preserved.
        argv = ["fire-cluster", "--input", str(src), "--output", str(out)]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
