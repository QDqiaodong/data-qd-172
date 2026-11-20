import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wildfire import review
from wildfire.cli import main


def _payload(ids, **overrides):
    observations = []
    for oid in ids:
        row = {"id": oid, "sensor": f"s-{oid}",
               "time": "2026-07-01T00:00:00+00:00",
               "lat": 0.0, "lon": 0.0, "severity": 0.5}
        row.update(overrides.get(oid, {}))
        observations.append(row)
    return {"radius_km": 10.0, "window_minutes": 60,
            "observations": observations}


class ReviewCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workbench = root / "workbench.json"
        self.input1 = root / "input1.json"
        self.input2 = root / "input2.json"
        self.input1.write_text(json.dumps(_payload(["a", "b"])))
        self.input2.write_text(json.dumps(
            _payload(["a", "b"], b={"lat": 0.0001})))

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

    def _init(self):
        code, stdout, stderr = self._run(
            ["review-init", "--workbench", str(self.workbench)])
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def _import(self, path):
        code, stdout, stderr = self._run(
            ["review-import", "--workbench", str(self.workbench),
             "--input", str(path)])
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def test_full_review_workflow(self):
        result = self._init()
        self.assertTrue(result["initialized"])

        receipt = self._import(self.input1)
        self.assertTrue(receipt["changed"])
        self.assertEqual(receipt["import_seq"], 1)
        self.assertEqual([c["case_id"] for c in receipt["created"]], ["c1"])
        self.assertEqual(receipt["todo"], ["c1"])

        # The new case is on the todo list, unassigned.
        code, stdout, _ = self._run(
            ["review-todo", "--workbench", str(self.workbench)])
        self.assertEqual(code, 0)
        todo = json.loads(stdout)
        self.assertEqual([c["case_id"] for c in todo], ["c1"])
        self.assertIsNone(todo[0]["owner"])
        self.assertEqual(todo[0]["revision"], 1)
        self.assertEqual(todo[0]["basis"]["incident"]["observation_ids"],
                         ["a", "b"])
        code, stdout, _ = self._run(
            ["review-todo", "--workbench", str(self.workbench),
             "--unassigned"])
        self.assertEqual([c["case_id"] for c in json.loads(stdout)], ["c1"])
        code, stdout, _ = self._run(
            ["review-todo", "--workbench", str(self.workbench),
             "--owner", "analyst-a"])
        self.assertEqual(json.loads(stdout), [])

        # Assign and conclude; each step bumps the revision.
        code, stdout, stderr = self._run(
            ["review-assign", "--workbench", str(self.workbench),
             "--case", "c1", "--expected-revision", "1", "--actor", "lead",
             "--owner", "analyst-a", "--reason", "triage rotation"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["revision"], 2)

        code, stdout, stderr = self._run(
            ["review-conclude", "--workbench", str(self.workbench),
             "--case", "c1", "--expected-revision", "2",
             "--actor", "analyst-a", "--verdict", "confirmed",
             "--note", "two sensors concur"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["revision"], 3)

        # Concluded cases leave the todo list but stay in the current view.
        code, stdout, _ = self._run(
            ["review-todo", "--workbench", str(self.workbench)])
        self.assertEqual(json.loads(stdout), [])
        code, stdout, _ = self._run(
            ["review-current", "--workbench", str(self.workbench)])
        current = json.loads(stdout)
        self.assertEqual([c["case_id"] for c in current], ["c1"])
        self.assertEqual(current[0]["owner"], "analyst-a")
        self.assertEqual(current[0]["conclusion"]["verdict"], "confirmed")
        self.assertEqual(current[0]["revision"], 3)
        self.assertFalse(current[0]["todo"])

        # A coordinate correction reopens the case for the same owner.
        receipt = self._import(self.input2)
        self.assertEqual([c["case_id"] for c in receipt["rebased"]], ["c1"])
        code, stdout, _ = self._run(
            ["review-todo", "--workbench", str(self.workbench),
             "--owner", "analyst-a"])
        todo = json.loads(stdout)
        self.assertEqual([c["case_id"] for c in todo], ["c1"])
        self.assertEqual(todo[0]["revision"], 4)

        # History shows the assignment, the old conclusion (now marked as
        # not effective), and the basis change.
        code, stdout, stderr = self._run(
            ["review-history", "--workbench", str(self.workbench),
             "--case", "c1"])
        self.assertEqual(code, 0, stderr)
        history = json.loads(stdout)
        self.assertEqual(history["status"], "active")
        self.assertEqual([e["kind"] for e in history["events"]],
                         ["created", "owner_assigned", "conclusion",
                          "basis_changed"])
        conclusion = history["events"][2]
        self.assertEqual(conclusion["actor"], "analyst-a")
        self.assertEqual(conclusion["note"], "two sensors concur")
        self.assertFalse(conclusion["currently_effective"])
        member_b = [m for m in conclusion["basis"]["members"]
                    if m["id"] == "b"][0]
        self.assertEqual(member_b["lat"], 0.0)

    def test_reimport_same_state_is_noop_via_cli(self):
        self._init()
        first = self._import(self.input1)
        again = self._import(self.input1)
        self.assertFalse(again["changed"])
        self.assertEqual(again["created"], [])
        before = self.workbench.read_bytes()
        self._import(self.input1)
        self.assertEqual(self.workbench.read_bytes(), before)
        self.assertEqual(first["todo"], again["todo"])

    def test_invalid_input_and_operations_exit_2_without_mutation(self):
        self._init()
        self._import(self.input1)
        before = self.workbench.read_bytes()

        bad = self.workbench.parent / "bad.json"
        bad.write_text(json.dumps(_payload(["a"], a={"sensor": ""})))
        code, _, stderr = self._run(
            ["review-import", "--workbench", str(self.workbench),
             "--input", str(bad)])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)

        broken = self.workbench.parent / "broken.json"
        broken.write_text("{not json")
        code, _, stderr = self._run(
            ["review-import", "--workbench", str(self.workbench),
             "--input", str(broken)])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)

        code, _, stderr = self._run(
            ["review-assign", "--workbench", str(self.workbench),
             "--case", "c999", "--expected-revision", "1", "--actor", "x",
             "--owner", "y", "--reason", "z"])
        self.assertEqual(code, 2)
        self.assertIn("unknown case", stderr)

        code, _, stderr = self._run(
            ["review-conclude", "--workbench", str(self.workbench),
             "--case", "c1", "--expected-revision", "99",
             "--actor", "analyst-a", "--verdict", "confirmed",
             "--note", "stale"])
        self.assertEqual(code, 2)
        self.assertIn("stale revision", stderr)

        code, _, stderr = self._run(
            ["review-conclude", "--workbench", str(self.workbench),
             "--case", "c1", "--expected-revision", "1",
             "--actor", "analyst-a", "--verdict", "confirmed",
             "--note", "unassigned"])
        self.assertEqual(code, 2)
        self.assertIn("unassigned", stderr)

        code, _, stderr = self._run(
            ["review-history", "--workbench", str(self.workbench),
             "--case", "c999"])
        self.assertEqual(code, 2)
        self.assertIn("unknown case", stderr)

        self.assertEqual(self.workbench.read_bytes(), before)

    def test_missing_workbench_exits_2(self):
        code, _, stderr = self._run(
            ["review-current", "--workbench", str(self.workbench)])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)
        code, _, stderr = self._run(
            ["review-import", "--workbench", str(self.workbench),
             "--input", str(self.input1)])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)

    def test_init_refuses_overwrite_and_bad_verdict_is_rejected(self):
        self._init()
        code, _, stderr = self._run(
            ["review-init", "--workbench", str(self.workbench)])
        self.assertEqual(code, 2)
        self.assertIn("already exists", stderr)
        self._import(self.input1)
        code, _, stderr = self._run(
            ["review-conclude", "--workbench", str(self.workbench),
             "--case", "c1", "--expected-revision", "1", "--actor", "a",
             "--verdict", "maybe", "--note", "x"])
        self.assertEqual(code, 2)

    def test_failed_write_recovers_and_cli_still_sees_everything(self):
        self._init()
        self._import(self.input1)
        before = self.workbench.read_bytes()

        # A failing write leaves the previous workbench fully intact.
        with mock.patch.object(review, "_atomic_write",
                               side_effect=OSError("disk full")):
            code, _, stderr = self._run(
                ["review-import", "--workbench", str(self.workbench),
                 "--input", str(self.input2)])
        self.assertEqual(code, 2)
        self.assertIn("fire-cluster:", stderr)
        self.assertEqual(self.workbench.read_bytes(), before)

        # The retry commits, and a freshly reopened CLI sees the full state.
        receipt = self._import(self.input2)
        self.assertEqual([c["case_id"] for c in receipt["rebased"]], ["c1"])
        code, stdout, _ = self._run(
            ["review-current", "--workbench", str(self.workbench)])
        current = json.loads(stdout)
        self.assertEqual(current[0]["revision"], 2)
        code, stdout, _ = self._run(
            ["review-history", "--workbench", str(self.workbench),
             "--case", "c1"])
        history = json.loads(stdout)
        self.assertEqual([e["kind"] for e in history["events"]],
                         ["created", "basis_changed"])


if __name__ == "__main__":
    unittest.main()
