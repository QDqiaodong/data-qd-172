import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wildfire import archive, review
from wildfire.review import ReviewError

TIME = "2026-07-01T00:00:00+00:00"


def _obs(oid, sensor=None, time=TIME, lat=0.0, lon=0.0, severity=0.5):
    return {"id": oid, "sensor": sensor or f"s-{oid}", "time": time,
            "lat": lat, "lon": lon, "severity": severity}


def _payload(observations, radius_km=10.0, window_minutes=60,
             maintenance_windows=None):
    payload = {"radius_km": radius_km, "window_minutes": window_minutes,
               "observations": observations}
    if maintenance_windows is not None:
        payload["maintenance_windows"] = maintenance_windows
    return payload


def _bridge_observations():
    # a and c are ~1.33 km apart (beyond a 1 km radius); b at 0.006 bridges
    # them (~0.67 km from each) whenever it is not suppressed.
    return [_obs("a", lon=0.0, severity=0.2),
            _obs("b", lon=0.006, severity=0.9),
            _obs("c", lon=0.012, severity=0.4)]


def _suppressing_window(sensor):
    return [{"sensor_id": sensor,
             "start": "2026-06-30T23:00:00+00:00",
             "end": "2026-07-01T01:00:00+00:00"}]


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "workbench.json"
        review.init_workbench(self.path)

    def _state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _import(self, payload):
        return review.import_observations(self.path, payload)

    def _assign(self, case_id, revision, owner, actor="lead", reason="triage"):
        return review.assign_owner(self.path, case_id, revision, actor,
                                   owner, reason)

    def _conclude(self, case_id, revision, actor, verdict="confirmed",
                  note="reviewed"):
        return review.submit_conclusion(self.path, case_id, revision, actor,
                                        verdict, note)

    def _todo_ids(self, **filters):
        return [case["case_id"]
                for case in review.todo_cases(self.path, **filters)]

    def _current(self):
        return {case["case_id"]: case
                for case in review.current_cases(self.path)}


class InitTests(ReviewCase):
    def test_init_creates_empty_workbench(self):
        state = self._state()
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["cases"], {})
        self.assertEqual(state["events"], [])
        self.assertEqual(state["next_case"], 1)

    def test_init_refuses_to_overwrite(self):
        with self.assertRaises(ReviewError):
            review.init_workbench(self.path)

    def test_operations_on_missing_workbench_are_rejected(self):
        missing = self.path.with_name("missing.json")
        with self.assertRaisesRegex(ReviewError, "not found"):
            review.import_observations(missing, _payload([_obs("a")]))
        with self.assertRaisesRegex(ReviewError, "not found"):
            review.current_cases(missing)
        with self.assertRaisesRegex(ReviewError, "not found"):
            review.todo_cases(missing)
        with self.assertRaisesRegex(ReviewError, "not found"):
            review.case_history(missing, "c1")


class CorrectionReopensReviewTests(ReviewCase):
    """A correction that leaves the cluster report identical still reopens
    the review; pure reordering and unrelated incidents never do."""

    def test_coordinate_correction_reopens_review(self):
        receipt = self._import(_payload([_obs("a", severity=0.2),
                                         _obs("b", severity=0.9)]))
        self.assertEqual([c["case_id"] for c in receipt["created"]], ["c1"])
        self.assertEqual(self._todo_ids(), ["c1"])

        self._assign("c1", 1, "甲")
        result = self._conclude("c1", 2, "甲", "confirmed", "looks real")
        self.assertEqual(result["revision"], 3)
        self.assertEqual(self._todo_ids(), [])
        current = self._current()
        self.assertEqual(current["c1"]["conclusion"]["verdict"], "confirmed")
        self.assertFalse(current["c1"]["todo"])

        # Correct b's coordinates slightly: the cluster report (incident_id,
        # members, counts, confidence, mean severity) stays identical.
        corrected = _payload([_obs("a", severity=0.2),
                              _obs("b", lat=0.0001, lon=0.0002,
                                   severity=0.9)])
        receipt = self._import(corrected)
        self.assertTrue(receipt["changed"])
        self.assertEqual([c["case_id"] for c in receipt["rebased"]], ["c1"])
        self.assertEqual(receipt["created"], [])
        self.assertEqual(receipt["retired"], [])

        # Same case, same owner, but the conclusion is cleared and the case
        # is back on 甲's todo list with a fresh revision.
        current = self._current()
        self.assertEqual(current["c1"]["revision"], 4)
        self.assertEqual(current["c1"]["owner"], "甲")
        self.assertIsNone(current["c1"]["conclusion"])
        self.assertEqual(self._todo_ids(owner="甲"), ["c1"])
        self.assertEqual(self._todo_ids(unassigned=True), [])

        # The historical conclusion keeps the old coordinates and is clearly
        # marked as no longer effective.
        history = review.case_history(self.path, "c1")
        conclusions = [e for e in history["events"]
                       if e["kind"] == "conclusion"]
        self.assertEqual(len(conclusions), 1)
        old = conclusions[0]
        self.assertFalse(old["currently_effective"])
        member_b = [m for m in old["basis"]["members"] if m["id"] == "b"][0]
        self.assertEqual((member_b["lat"], member_b["lon"]), (0.0, 0.0))
        self.assertEqual(old["basis"]["radius_km"], 10.0)
        self.assertEqual(old["basis"]["window_minutes"], 60)
        self.assertEqual(old["basis"]["incident"]["observation_ids"],
                         ["a", "b"])
        # The current basis carries the corrected coordinates.
        member_b_now = [m for m in history["basis"]["members"]
                        if m["id"] == "b"][0]
        self.assertEqual((member_b_now["lat"], member_b_now["lon"]),
                         (0.0001, 0.0002))

        # The old revision cannot confirm anymore.
        with self.assertRaisesRegex(ReviewError, "stale revision"):
            self._conclude("c1", 3, "甲", "confirmed", "replay")
        self.assertEqual(self._state()["events"][-1]["kind"],
                         "basis_changed")

        # Re-importing the identical corrected state (even reordered) is a
        # no-op: no new revision, no new events, no new todo entries.
        reordered = _payload([_obs("b", lat=0.0001, lon=0.0002,
                                   severity=0.9),
                              _obs("a", severity=0.2)])
        receipt = self._import(reordered)
        self.assertFalse(receipt["changed"])
        self.assertEqual(receipt["created"], [])
        self.assertEqual(self._current()["c1"]["revision"], 4)
        self.assertEqual(self._todo_ids(), ["c1"])

        # An unrelated new incident elsewhere does not touch c1's revision.
        with_z = _payload([_obs("a", severity=0.2),
                           _obs("b", lat=0.0001, lon=0.0002, severity=0.9),
                           _obs("z", lat=5.0, lon=5.0)])
        receipt = self._import(with_z)
        self.assertEqual([c["case_id"] for c in receipt["created"]], ["c2"])
        self.assertEqual(self._current()["c1"]["revision"], 4)

        # The credential read before that import is still valid.
        result = self._conclude("c1", 4, "甲", "dismissed",
                                "corrected reading is benign")
        self.assertEqual(result["revision"], 5)
        # c2 (the unrelated new incident) is unassigned and still pending.
        self.assertEqual(self._todo_ids(owner="甲"), [])
        self.assertEqual(self._todo_ids(), ["c2"])
        self.assertEqual(self._todo_ids(unassigned=True), ["c2"])

    def test_time_correction_reopens_even_with_same_cluster(self):
        self._import(_payload([_obs("a"), _obs("b")]))
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲")
        shifted = _payload([_obs("a"),
                            _obs("b", time="2026-07-01T00:05:00+00:00")])
        receipt = self._import(shifted)
        self.assertEqual([c["case_id"] for c in receipt["rebased"]], ["c1"])
        self.assertEqual(self._todo_ids(), ["c1"])

    def test_unrelated_change_preserves_conclusion_and_credential(self):
        self._import(_payload([_obs("a"), _obs("b"),
                               _obs("z", lat=5.0, lon=5.0)]))
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲")
        # Change only the other incident's membership.
        receipt = self._import(_payload([_obs("a"), _obs("b"),
                                         _obs("z", lat=5.0, lon=5.0),
                                         _obs("y", lat=5.0, lon=5.0001)]))
        self.assertEqual([c["case_id"] for c in receipt["rebased"]], ["c2"])
        current = self._current()
        self.assertEqual(current["c1"]["revision"], 3)
        self.assertEqual(current["c1"]["conclusion"]["verdict"], "confirmed")
        self.assertEqual(self._todo_ids(), ["c2"])


class IncidentIdChangeContinuationTests(ReviewCase):
    """Deleting a member can rename the incident without breaking the
    one-to-one case continuation."""

    def test_incident_id_rename_still_continues_case(self):
        self._import(_payload([_obs("a", severity=0.2),
                               _obs("c", severity=0.4)]))
        self.assertEqual(self._current()["c1"]["incident_id"], "a")
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲")

        # Delete a, keep c: incident_id moves from "a" to "c", but the link
        # is mutually unique, so c1 continues and is reopened by the change.
        receipt = self._import(_payload([_obs("c", severity=0.4)]))
        self.assertEqual(receipt["created"], [])
        self.assertEqual(receipt["retired"], [])
        self.assertEqual([c["case_id"] for c in receipt["rebased"]], ["c1"])
        current = self._current()
        self.assertEqual(list(current), ["c1"])
        self.assertEqual(current["c1"]["incident_id"], "c")
        self.assertEqual(current["c1"]["observation_ids"] if
                         "observation_ids" in current["c1"] else
                         current["c1"]["basis"]["incident"]
                         ["observation_ids"], ["c"])
        self.assertEqual(current["c1"]["owner"], "甲")
        self.assertIsNone(current["c1"]["conclusion"])
        self.assertEqual(self._todo_ids(), ["c1"])

        # History shows the rename and the invalidated conclusion.
        history = review.case_history(self.path, "c1")
        kinds = [e["kind"] for e in history["events"]]
        self.assertEqual(kinds, ["created", "owner_assigned", "conclusion",
                                 "basis_changed"])
        self.assertEqual(history["events"][-1]["incident_id"], "c")
        self.assertEqual(history["events"][-1]["invalidated_conclusion"]
                         ["verdict"], "confirmed")


class MergeSplitLineageTests(ReviewCase):
    """The a/b/c bridge scenario: merge retires both parents, the split
    creates a third generation, and every link stays queryable."""

    def _import_bridge(self, ids, **kwargs):
        observations = [row for row in _bridge_observations()
                        if row["id"] in ids]
        return self._import(_payload(observations, radius_km=1.0,
                                     window_minutes=0, **kwargs))

    def test_merge_then_split_keeps_three_generations(self):
        # Generation 1: a and c are separate incidents and separate cases.
        receipt = self._import_bridge(["a", "c"])
        self.assertEqual([c["case_id"] for c in receipt["created"]],
                         ["c1", "c2"])
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲", "confirmed", "a verified")
        self._assign("c2", 1, "乙")
        self._conclude("c2", 2, "乙", "dismissed", "c is noise")

        # Generation 2: b bridges them into one incident. Both old cases
        # retire; the merged case is new, unassigned (owners differ), and
        # carries no conclusion.
        receipt = self._import_bridge(["a", "b", "c"])
        self.assertEqual([c["case_id"] for c in receipt["created"]], ["c3"])
        self.assertEqual(sorted(c["case_id"] for c in receipt["retired"]),
                         ["c1", "c2"])
        merged = self._current()["c3"]
        self.assertIsNone(merged["owner"])
        self.assertIsNone(merged["conclusion"])
        self.assertEqual(merged["basis"]["incident"]["observation_ids"],
                         ["a", "b", "c"])
        self.assertEqual(self._todo_ids(), ["c3"])
        self.assertEqual(self._todo_ids(unassigned=True), ["c3"])
        links = {(l["predecessor"], l["successor"]):
                 l["shared_observation_ids"] for l in receipt["links"]}
        self.assertEqual(links, {("c1", "c3"): ["a"], ("c2", "c3"): ["c"]})

        # Neither old conclusion leaked into the merged case.
        history = review.case_history(self.path, "c3")
        self.assertEqual([e["kind"] for e in history["events"]], ["created"])
        self.assertEqual(history["predecessors"],
                         [{"case_id": "c1", "shared_observation_ids": ["a"]},
                          {"case_id": "c2", "shared_observation_ids": ["c"]}])

        # Assign 丙 and confirm the merged case.
        self._assign("c3", 1, "丙")
        self._conclude("c3", 2, "丙", "confirmed", "merged event verified")

        # Generation 3: removing b splits the incident again. Both children
        # are new cases; both inherit 丙 (their single predecessor's owner)
        # but neither inherits the conclusion.
        receipt = self._import_bridge(["a", "c"])
        self.assertEqual([c["case_id"] for c in receipt["created"]],
                         ["c4", "c5"])
        self.assertEqual([c["case_id"] for c in receipt["retired"]], ["c3"])
        current = self._current()
        self.assertEqual(current["c4"]["owner"], "丙")
        self.assertEqual(current["c5"]["owner"], "丙")
        self.assertIsNone(current["c4"]["conclusion"])
        self.assertIsNone(current["c5"]["conclusion"])
        self.assertEqual(current["c4"]["basis"]["incident"]
                         ["observation_ids"], ["a"])
        self.assertEqual(current["c5"]["basis"]["incident"]
                         ["observation_ids"], ["c"])
        self.assertEqual(self._todo_ids(owner="丙"), ["c4", "c5"])

        # The whole three-generation chain stays queryable.
        h1 = review.case_history(self.path, "c1")
        self.assertEqual(h1["status"], "retired")
        self.assertEqual(h1["successors"],
                         [{"case_id": "c3", "shared_observation_ids": ["a"]}])
        h3 = review.case_history(self.path, "c3")
        self.assertEqual(h3["status"], "retired")
        self.assertEqual([p["case_id"] for p in h3["predecessors"]],
                         ["c1", "c2"])
        self.assertEqual([s["case_id"] for s in h3["successors"]],
                         ["c4", "c5"])
        self.assertIsNone(h3["current_conclusion"])
        conclusion = [e for e in h3["events"] if e["kind"] == "conclusion"][0]
        self.assertFalse(conclusion["currently_effective"])
        self.assertEqual(conclusion["actor"], "丙")
        h4 = review.case_history(self.path, "c4")
        self.assertEqual(h4["predecessors"],
                         [{"case_id": "c3", "shared_observation_ids": ["a"]}])

        # Retired cases can no longer be claimed or concluded.
        with self.assertRaisesRegex(ReviewError, "retired"):
            self._assign("c1", h1["revision"], "丁")
        with self.assertRaisesRegex(ReviewError, "retired"):
            self._conclude("c3", h3["revision"], "丙")

    def test_merge_with_same_owner_inherits_owner_but_not_conclusion(self):
        receipt = self._import_bridge(["a", "c"])
        for cid in ("c1", "c2"):
            self._assign(cid, 1, "甲")
            self._conclude(cid, 2, "甲")
        receipt = self._import_bridge(["a", "b", "c"])
        merged = receipt["created"][0]
        self.assertEqual(merged["owner"], "甲")
        current = self._current()[merged["case_id"]]
        self.assertIsNone(current["conclusion"])
        self.assertEqual(self._todo_ids(owner="甲"),
                         [merged["case_id"]])


class CrossReorganizationTests(ReviewCase):
    """Cross-reorganization keeps every direct link; vanished and restored
    observations never resurrect retired identities."""

    def _spread_payload(self):
        # Two tight pairs 11 km apart, plus a loner another 11 km away.
        return _payload([_obs("a", lon=0.0), _obs("b", lon=0.0),
                         _obs("c", lon=0.1), _obs("d", lon=0.1),
                         _obs("e", lon=0.2)], radius_km=1.0, window_minutes=0)

    def test_cross_reorganization_preserves_all_links(self):
        receipt = self._import(self._spread_payload())
        self.assertEqual([c["case_id"] for c in receipt["created"]],
                         ["c1", "c2", "c3"])
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲")

        # Regroup: {a,c}, {b,d}, new loner {f}; e vanishes.
        regrouped = _payload([_obs("a", lon=0.0), _obs("c", lon=0.0),
                              _obs("b", lon=0.1), _obs("d", lon=0.1),
                              _obs("f", lon=0.2)],
                             radius_km=1.0, window_minutes=0)
        receipt = self._import(regrouped)
        self.assertEqual([c["case_id"] for c in receipt["created"]],
                         ["c4", "c5", "c6"])
        self.assertEqual([c["case_id"] for c in receipt["retired"]],
                         ["c1", "c2", "c3"])
        # No old case is continued by any child: every old case retired.
        self.assertEqual(receipt["continued"], [])
        self.assertEqual(receipt["rebased"], [])
        links = {(l["predecessor"], l["successor"])
                 for l in receipt["links"]}
        self.assertEqual(links, {("c1", "c4"), ("c1", "c5"),
                                 ("c2", "c4"), ("c2", "c5")})
        h1 = review.case_history(self.path, "c1")
        self.assertEqual([(s["case_id"], s["shared_observation_ids"])
                          for s in h1["successors"]],
                         [("c4", ["a"]), ("c5", ["b"])])
        h2 = review.case_history(self.path, "c2")
        self.assertEqual([(s["case_id"], s["shared_observation_ids"])
                          for s in h2["successors"]],
                         [("c4", ["c"]), ("c5", ["d"])])
        h3 = review.case_history(self.path, "c3")
        self.assertEqual(h3["successors"], [])
        # The vanished loner's case retired without successors; the brand
        # new loner is a fresh, unassigned case.
        current = self._current()
        self.assertEqual(current["c6"]["owner"], None)
        self.assertEqual(current["c6"]["basis"]["incident"]
                         ["observation_ids"], ["f"])
        # c4 and c5 have two predecessors each with different owners
        # (甲 and unassigned), so both stay unassigned and unconcluded.
        self.assertIsNone(current["c4"]["owner"])
        self.assertIsNone(current["c5"]["owner"])
        self.assertEqual(sorted(self._todo_ids()),
                         ["c4", "c5", "c6"])

    def test_clear_and_restore_never_recycles_identity(self):
        self._import(self._spread_payload())
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲", "confirmed", "verified")
        before = self._state()

        # Wipe the observation set: everything retires.
        receipt = self._import(_payload([], radius_km=1.0, window_minutes=0))
        self.assertEqual(sorted(c["case_id"] for c in receipt["retired"]),
                         ["c1", "c2", "c3"])
        self.assertEqual(review.current_cases(self.path), [])
        self.assertEqual(self._todo_ids(), [])

        # Restore the exact original observations: brand new cases, no
        # recycled identities, no restored conclusions or owners.
        receipt = self._import(self._spread_payload())
        new_ids = [c["case_id"] for c in receipt["created"]]
        self.assertEqual(new_ids, ["c4", "c5", "c6"])
        self.assertTrue(set(new_ids).isdisjoint({"c1", "c2", "c3"}))
        for cid in new_ids:
            view = self._current()[cid]
            self.assertIsNone(view["owner"])
            self.assertIsNone(view["conclusion"])
            self.assertEqual(review.case_history(self.path, cid)
                             ["predecessors"], [])
        self.assertEqual(sorted(self._todo_ids()), new_ids)

        # The retired history, including the old confirmed conclusion, is
        # still intact and clearly marked as not effective.
        h1 = review.case_history(self.path, "c1")
        self.assertEqual(h1["status"], "retired")
        conclusion = [e for e in h1["events"] if e["kind"] == "conclusion"][0]
        self.assertEqual(conclusion["verdict"], "confirmed")
        self.assertFalse(conclusion["currently_effective"])
        # Retired cases cannot be claimed anymore.
        with self.assertRaisesRegex(ReviewError, "retired"):
            self._assign("c1", h1["revision"], "乙")
        # Old events were never rewritten by the wipe/restore cycle.
        self.assertGreater(len(self._state()["events"]),
                           len(before["events"]))


class MaintenanceWindowTests(ReviewCase):
    """Suppression-driven splits and merges follow the same case rules and
    reuse the existing clustering results unchanged."""

    def test_suppression_bridge_split_and_merge(self):
        window = _suppressing_window("s-b")
        payload = _payload(_bridge_observations(), radius_km=1.0,
                           window_minutes=0, maintenance_windows=window)

        # b is suppressed: it belongs to no current incident, and a, c form
        # two separate cases.
        receipt = self._import(payload)
        self.assertEqual(receipt["suppressed_observation_ids"], ["b"])
        self.assertEqual(receipt["observation_count"], 3)
        self.assertEqual([c["case_id"] for c in receipt["created"]],
                         ["c1", "c2"])
        current = self._current()
        self.assertEqual(current["c1"]["basis"]["incident"]
                         ["observation_ids"], ["a"])
        self.assertEqual(current["c2"]["basis"]["incident"]
                         ["observation_ids"], ["c"])
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲")
        self._assign("c2", 1, "甲")
        self._conclude("c2", 2, "甲")

        # Lifting the window lets b bridge the cluster: one merged case.
        lifted = _payload(_bridge_observations(), radius_km=1.0,
                          window_minutes=0)
        receipt = self._import(lifted)
        self.assertEqual(receipt["suppressed_observation_ids"], [])
        self.assertEqual([c["case_id"] for c in receipt["created"]], ["c3"])
        self.assertEqual(sorted(c["case_id"] for c in receipt["retired"]),
                         ["c1", "c2"])
        merged = self._current()["c3"]
        self.assertEqual(merged["owner"], "甲")  # both predecessors owned by 甲
        self.assertIsNone(merged["conclusion"])
        self.assertEqual(self._todo_ids(), ["c3"])
        self._assign("c3", 1, "甲")  # no-op: already inherited
        self._conclude("c3", 1, "甲", "confirmed", "bridge verified")

        # Re-applying the window splits again into two fresh cases.
        receipt = self._import(payload)
        self.assertEqual([c["case_id"] for c in receipt["created"]],
                         ["c4", "c5"])
        self.assertEqual([c["case_id"] for c in receipt["retired"]], ["c3"])
        current = self._current()
        self.assertEqual(current["c4"]["owner"], "甲")
        self.assertEqual(current["c5"]["owner"], "甲")
        self.assertIsNone(current["c4"]["conclusion"])
        self.assertIsNone(current["c5"]["conclusion"])
        self.assertEqual(self._todo_ids(), ["c4", "c5"])
        # b never appears in any case basis.
        for cid in ("c4", "c5"):
            member_ids = [m["id"] for m in
                          current[cid]["basis"]["members"]]
            self.assertNotIn("b", member_ids)

    def test_window_change_unrelated_to_case_keeps_conclusion(self):
        self._import(_payload([_obs("a"), _obs("b")]))
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲")
        # A maintenance window for a sensor that does not exist changes
        # nothing about the case basis: no re-review, no new revision.
        payload = _payload([_obs("a"), _obs("b")],
                           maintenance_windows=_suppressing_window("s-ghost"))
        receipt = self._import(payload)
        self.assertFalse(receipt["changed"])
        current = self._current()
        self.assertEqual(current["c1"]["revision"], 3)
        self.assertEqual(current["c1"]["conclusion"]["verdict"],
                         "confirmed")
        self.assertEqual(self._todo_ids(), [])


class ImportValidationTests(ReviewCase):
    def test_invalid_imports_leave_workbench_untouched(self):
        self._import(_payload([_obs("a"), _obs("b")]))
        before = self.path.read_bytes()
        bad_payloads = [
            "not a dict",
            {"observations": "not a list"},
            _payload(["not a dict"]),
            _payload([dict(_obs("a"), id="")]),
            _payload([dict(_obs("a"), id=7)]),
            _payload([dict(_obs("a"), sensor="")]),
            _payload([{k: v for k, v in _obs("a").items() if k != "sensor"}]),
            _payload([dict(_obs("a"), lat=95)]),
            _payload([dict(_obs("a"), severity=2)]),
            _payload([_obs("a"), _obs("a")]),
            _payload([_obs("a")], radius_km=-1),
            _payload([_obs("a")], maintenance_windows=[
                {"sensor_id": "s-a", "start": TIME, "end": TIME}]),
            {"window_minutes": 60, "observations": [_obs("a")]},
        ]
        for bad in bad_payloads:
            with self.assertRaises((ReviewError, ValueError, KeyError,
                                    TypeError)):
                self._import(bad)
            self.assertEqual(self.path.read_bytes(), before)
        # No identity was consumed by the failures.
        receipt = self._import(_payload([_obs("a"), _obs("b"),
                                         _obs("z", lat=5.0, lon=5.0)]))
        self.assertEqual([c["case_id"] for c in receipt["created"]], ["c2"])

    def test_extra_observation_fields_do_not_enter_basis(self):
        row = dict(_obs("a"), note="ignored", tags=["x"])
        receipt = self._import(_payload([row]))
        self.assertTrue(receipt["changed"])
        members = self._current()["c1"]["basis"]["members"]
        self.assertEqual(sorted(members[0]),
                         ["id", "lat", "lon", "sensor", "severity", "time"])
        # Changing only the extra field is a no-op import.
        row2 = dict(_obs("a"), note="different", tags=["y"])
        receipt = self._import(_payload([row2]))
        self.assertFalse(receipt["changed"])

    def test_numeric_and_string_basis_comparison(self):
        # 2 and 2.0 are the same number: no spurious invalidation.
        self._import(_payload([_obs("a")], radius_km=2, window_minutes=0))
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲")
        receipt = self._import(_payload([_obs("a")], radius_km=2.0,
                                         window_minutes=0))
        self.assertFalse(receipt["changed"])
        # A changed parameter is a basis change and reopens the review.
        receipt = self._import(_payload([_obs("a")], radius_km=3.0,
                                         window_minutes=0))
        self.assertEqual([c["case_id"] for c in receipt["rebased"]], ["c1"])
        self.assertEqual(self._todo_ids(), ["c1"])
        # Timestamps compare as strings: an equivalent but differently
        # spelled offset is a basis change too.
        receipt = self._import(_payload(
            [_obs("a", time="2026-07-01T02:00:00+02:00")], radius_km=3.0,
            window_minutes=0))
        self.assertEqual([c["case_id"] for c in receipt["rebased"]], ["c1"])


class OperationValidationTests(ReviewCase):
    def setUp(self):
        super().setUp()
        self._import(_payload([_obs("a"), _obs("b")]))

    def _assert_untouched(self):
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(len(self._state()["events"]), self.event_count)

    def test_unknown_case_is_rejected(self):
        self.before = self.path.read_bytes()
        self.event_count = len(self._state()["events"])
        with self.assertRaisesRegex(ReviewError, "unknown case"):
            self._assign("c999", 1, "甲")
        with self.assertRaisesRegex(ReviewError, "unknown case"):
            self._conclude("c999", 1, "甲")
        with self.assertRaisesRegex(ReviewError, "unknown case"):
            review.case_history(self.path, "c999")
        self._assert_untouched()

    def test_empty_actor_owner_reason_note_are_rejected(self):
        self.before = self.path.read_bytes()
        self.event_count = len(self._state()["events"])
        with self.assertRaisesRegex(ReviewError, "actor"):
            self._assign("c1", 1, "甲", actor="")
        with self.assertRaisesRegex(ReviewError, "owner"):
            self._assign("c1", 1, "")
        with self.assertRaisesRegex(ReviewError, "reason"):
            self._assign("c1", 1, "甲", reason="")
        self._assert_untouched()
        # Empty notes are rejected too; the successful assignment before
        # them is part of the setup, not of the assertion.
        self._assign("c1", 1, "甲")
        self.before = self.path.read_bytes()
        self.event_count = len(self._state()["events"])
        with self.assertRaisesRegex(ReviewError, "note"):
            self._conclude("c1", 2, "甲", note="")
        with self.assertRaisesRegex(ReviewError, "actor"):
            self._conclude("c1", 2, "")
        self._assert_untouched()

    def test_conclusion_rules(self):
        # Unassigned cases accept no conclusion.
        with self.assertRaisesRegex(ReviewError, "unassigned"):
            self._conclude("c1", 1, "甲")
        self._assign("c1", 1, "甲")
        # Only the current owner may conclude.
        with self.assertRaisesRegex(ReviewError, "not the current owner"):
            self._conclude("c1", 2, "乙")
        # Verdict is restricted.
        with self.assertRaises(ReviewError):
            self._conclude("c1", 2, "甲", verdict="maybe")
        self._conclude("c1", 2, "甲", "confirmed", "verified")
        self.before = self.path.read_bytes()
        self.event_count = len(self._state()["events"])
        # Already-concluded cases reject further conclusions and assignments.
        with self.assertRaisesRegex(ReviewError, "already has a conclusion"):
            self._conclude("c1", 3, "甲", "dismissed", "changed my mind")
        with self.assertRaisesRegex(ReviewError, "already has a conclusion"):
            self._assign("c1", 3, "乙")
        self._assert_untouched()
        current = self._current()
        self.assertEqual(current["c1"]["conclusion"]["verdict"], "confirmed")

    def test_one_conclusion_per_revision(self):
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲", "confirmed", "first wins")
        self.before = self.path.read_bytes()
        self.event_count = len(self._state()["events"])
        # Same-revision replays, same or opposite verdict, are rejected as
        # stale and never overwrite the original conclusion.
        with self.assertRaisesRegex(ReviewError, "stale revision"):
            self._conclude("c1", 2, "甲", "confirmed", "first wins")
        with self.assertRaisesRegex(ReviewError, "stale revision"):
            self._conclude("c1", 2, "甲", "dismissed", "flip")
        self._assert_untouched()
        history = review.case_history(self.path, "c1")
        conclusions = [e for e in history["events"]
                       if e["kind"] == "conclusion"]
        self.assertEqual(len(conclusions), 1)
        self.assertEqual(conclusions[0]["note"], "first wins")
        self.assertTrue(conclusions[0]["currently_effective"])

    def test_stale_revision_checked_before_noop(self):
        self._assign("c1", 1, "甲")
        self._assign("c1", 2, "乙", reason="rebalance")
        # Re-setting the current owner with the current revision is a no-op.
        result = self._assign("c1", 3, "乙", reason="duplicate")
        self.assertEqual(result, {"case_id": "c1", "changed": False,
                                  "revision": 3, "owner": "乙"})
        # The same request with an old revision is rejected, not a no-op.
        with self.assertRaisesRegex(ReviewError, "stale revision"):
            self._assign("c1", 2, "乙", reason="duplicate")
        with self.assertRaisesRegex(ReviewError, "stale revision"):
            self._assign("c1", 1, "甲", reason="old credential")

    def test_expected_revision_must_be_an_integer(self):
        with self.assertRaisesRegex(ReviewError, "integer"):
            self._assign("c1", "1", "甲")
        with self.assertRaisesRegex(ReviewError, "integer"):
            self._assign("c1", True, "甲")

    def test_owner_may_hold_multiple_cases(self):
        self._import(_payload([_obs("a"), _obs("b"),
                               _obs("z", lat=5.0, lon=5.0)]))
        self._assign("c1", 1, "甲")
        self._assign("c2", 1, "甲")
        self.assertEqual(sorted(self._todo_ids(owner="甲")), ["c1", "c2"])


class AtomicityTests(ReviewCase):
    def test_failed_import_write_leaves_original_state(self):
        self._import(_payload([_obs("a"), _obs("b")]))
        before = self.path.read_bytes()
        with mock.patch.object(review, "_atomic_write",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._import(_payload([_obs("a"), _obs("b"),
                                       _obs("z", lat=5.0, lon=5.0)]))
        self.assertEqual(self.path.read_bytes(), before)
        # No temp file is left behind.
        self.assertEqual([p.name for p in self.path.parent.iterdir()],
                         [self.path.name])
        # The workbench continues from the original state: the retry gets
        # the very identities the failed attempt would have used.
        receipt = self._import(_payload([_obs("a"), _obs("b"),
                                         _obs("z", lat=5.0, lon=5.0)]))
        self.assertEqual([c["case_id"] for c in receipt["created"]], ["c2"])
        self.assertEqual(receipt["import_seq"], 2)

    def test_failed_conclusion_write_leaves_original_state(self):
        self._import(_payload([_obs("a"), _obs("b")]))
        self._assign("c1", 1, "甲")
        before = self.path.read_bytes()
        with mock.patch.object(review, "_atomic_write",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._conclude("c1", 2, "甲")
        self.assertEqual(self.path.read_bytes(), before)
        # The case is still pending and the retry succeeds with the same
        # revision.
        self.assertEqual(self._todo_ids(), ["c1"])
        result = self._conclude("c1", 2, "甲")
        self.assertEqual(result["revision"], 3)

    def test_failed_assignment_write_leaves_original_state(self):
        self._import(_payload([_obs("a"), _obs("b")]))
        before = self.path.read_bytes()
        with mock.patch.object(review, "_atomic_write",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._assign("c1", 1, "甲")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self._current()["c1"]["owner"], None)
        self._assign("c1", 1, "甲")
        self.assertEqual(self._current()["c1"]["owner"], "甲")


class PersistenceTests(ReviewCase):
    def test_reopen_preserves_full_state_and_history(self):
        self._import(_payload([_obs("a"), _obs("b")]))
        self._assign("c1", 1, "甲")
        self._conclude("c1", 2, "甲", "confirmed", "verified")
        self._import(_payload([_obs("a"), _obs("b", lat=0.0001)]))
        snapshot = self.path.read_bytes()

        # Every entry point reloads from disk, so a fresh process observes
        # exactly this state.
        self.assertEqual(self.path.read_bytes(), snapshot)
        current = self._current()
        self.assertEqual(current["c1"]["revision"], 4)
        self.assertEqual(current["c1"]["owner"], "甲")
        self.assertIsNone(current["c1"]["conclusion"])
        self.assertEqual(self._todo_ids(), ["c1"])
        history = review.case_history(self.path, "c1")
        self.assertEqual([e["kind"] for e in history["events"]],
                         ["created", "owner_assigned", "conclusion",
                          "basis_changed"])
        # The next identities continue the monotonic counters.
        receipt = self._import(_payload([_obs("a"), _obs("b", lat=0.0001),
                                         _obs("z", lat=5.0, lon=5.0)]))
        self.assertEqual([c["case_id"] for c in receipt["created"]], ["c2"])
        self.assertEqual(receipt["import_seq"], 3)


class ArchiveIndependenceTests(ReviewCase):
    """Dashboard acknowledgements are not review conclusions."""

    def test_archive_ack_does_not_conclude_workbench_case(self):
        archive_path = self.path.with_name("archive.json")
        archive.init_archive(archive_path, ["X"])
        payload = _payload([_obs("a"), _obs("b")])
        receipt = archive.publish(archive_path, "b1", payload)
        archive.acknowledge(archive_path, "X", receipt["record_ids"][0])
        self.assertEqual(archive.pending(archive_path, "X"), [])

        # The workbench reads the same observation JSON directly and still
        # requires a human conclusion of its own.
        self.assertFalse(review.current_cases(self.path))
        self._import(payload)
        self.assertEqual(self._todo_ids(), ["c1"])
        current = self._current()
        self.assertIsNone(current["c1"]["conclusion"])


if __name__ == "__main__":
    unittest.main()
