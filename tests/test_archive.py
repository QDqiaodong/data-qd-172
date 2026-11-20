import copy
import json
import tempfile
import unittest
from pathlib import Path

from wildfire import archive
from wildfire.archive import ArchiveError


def _obs(oid, sensor=None, severity=0.5, time="2026-07-01T00:00:00+00:00",
         lat=0.0, lon=0.0):
    return {"id": oid, "sensor": sensor or f"s-{oid}", "time": time,
            "lat": lat, "lon": lon, "severity": severity}


def _payload(ids, maintenance_windows=None):
    payload = {"radius_km": 10.0, "window_minutes": 60,
               "observations": [_obs(oid, severity=0.2 if oid == "a" else
                                     0.9 if oid == "b" else 0.4)
                                for oid in ids]}
    if maintenance_windows is not None:
        payload["maintenance_windows"] = maintenance_windows
    return payload


def _suppressing_windows(sensors=("s-a", "s-b", "s-c")):
    return [{"sensor_id": sensor,
             "start": "2026-06-30T23:00:00+00:00",
             "end": "2026-07-01T01:00:00+00:00"} for sensor in sensors]


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "archive.json"

    def _state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _init(self, consumers=("X", "Y")):
        archive.init_archive(self.path, consumers)
        return self.path


class InitTests(ArchiveCase):
    def test_init_registers_consumers_and_empty_history(self):
        archive.init_archive(self.path, ["X", "Y"])
        state = self._state()
        self.assertEqual(state["consumers"], ["X", "Y"])
        self.assertEqual(state["acks"], {"X": [], "Y": []})
        self.assertEqual(state["records"], [])
        self.assertEqual(state["next_version"], 1)
        self.assertEqual(state["next_record"], 1)

    def test_init_rejects_empty_duplicate_or_blank_consumers(self):
        with self.assertRaises(ArchiveError):
            archive.init_archive(self.path, [])
        path2 = self.path.with_name("other.json")
        with self.assertRaises(ArchiveError):
            archive.init_archive(path2, ["X", "X"])
        with self.assertRaises(ArchiveError):
            archive.init_archive(self.path.with_name("third.json"), [""])

    def test_init_refuses_to_overwrite_existing_archive(self):
        self._init()
        with self.assertRaises(ArchiveError):
            archive.init_archive(self.path, ["Z"])


class PublishLifecycleTests(ArchiveCase):
    def test_full_acceptance_lifecycle(self):
        self._init()

        # Batch 1: a and b form incident "a" as version v1.
        receipt1 = archive.publish(self.path, "batch-1", _payload(["a", "b"]))
        self.assertEqual(receipt1["batch_id"], "batch-1")
        self.assertEqual(receipt1["record_ids"], ["r1"])
        self.assertEqual(len(receipt1["published"]), 1)
        self.assertEqual(receipt1["withdrawn"], [])
        state = self._state()
        self.assertEqual(state["next_version"], 2)
        self.assertEqual(state["next_record"], 2)
        r1 = state["records"][0]
        self.assertEqual(r1, {"record_id": "r1", "seq": 1, "kind": "publish",
                              "incident_id": "a", "version": "v1",
                              "batch_id": "batch-1"})
        self.assertEqual(state["versions"]["v1"]["observation_ids"], ["a", "b"])
        self.assertEqual(state["current"],
                         {"a": {"version": "v1",
                                "event": state["versions"]["v1"]}})

        # Both consumers can claim the publish record; X acknowledges it.
        for consumer in ("X", "Y"):
            pending = archive.pending(self.path, consumer)
            self.assertEqual([r["record_id"] for r in pending], ["r1"])
            self.assertEqual(pending[0]["event"]["incident_id"], "a")
        archive.acknowledge(self.path, "X", "r1")

        # Batch 2: c joins the same cluster -> withdraw v1, publish v2, in
        # that order.
        receipt2 = archive.publish(self.path, "batch-2",
                                   _payload(["a", "b", "c"]))
        self.assertEqual(receipt2["record_ids"], ["r2", "r3"])
        self.assertEqual([w["version"] for w in receipt2["withdrawn"]], ["v1"])
        self.assertEqual([p["version"] for p in receipt2["published"]], ["v2"])
        state = self._state()
        kinds = [(r["seq"], r["kind"], r["version"]) for r in state["records"]]
        self.assertEqual(kinds, [(1, "publish", "v1"),
                                 (2, "withdraw", "v1"),
                                 (3, "publish", "v2")])
        self.assertEqual(state["current"]["a"]["version"], "v2")
        self.assertEqual(state["versions"]["v2"]["observation_ids"],
                         ["a", "b", "c"])
        # The withdrawn version stays immutable in history.
        self.assertEqual(state["versions"]["v1"]["observation_ids"], ["a", "b"])

        # X sees only the two new records; Y sees the whole ordered history.
        self.assertEqual([r["record_id"] for r in archive.pending(self.path, "X")],
                         ["r2", "r3"])
        self.assertEqual([r["record_id"] for r in archive.pending(self.path, "Y")],
                         ["r1", "r2", "r3"])
        self.assertEqual([r["kind"] for r in archive.pending(self.path, "Y")],
                         ["publish", "withdraw", "publish"])
        # X's old acknowledgement cannot swallow the v2 publish.
        self.assertNotIn("r3", state["acks"]["X"])

        # An identical-content batch creates no records.
        receipt3 = archive.publish(self.path, "batch-2b",
                                   _payload(["a", "b", "c"]))
        self.assertEqual(receipt3["record_ids"], [])
        self.assertEqual(self._state()["next_record"], 4)
        self.assertEqual([r["record_id"] for r in archive.pending(self.path, "X")],
                         ["r2", "r3"])

        # Replaying batch 1 returns the original receipt, no new identities.
        replay = archive.publish(self.path, "batch-1", _payload(["a", "b"]))
        self.assertEqual(replay, receipt1)
        self.assertEqual(self._state()["next_record"], 4)
        self.assertEqual(self._state()["next_version"], 3)

        # Batch 3 restores the original content: a brand new v3 is allocated;
        # v1 is never reused.
        receipt4 = archive.publish(self.path, "batch-3", _payload(["a", "b"]))
        self.assertEqual([r["version"] for r in receipt4["withdrawn"]], ["v2"])
        self.assertEqual([r["version"] for r in receipt4["published"]], ["v3"])
        self.assertEqual(receipt4["record_ids"], ["r4", "r5"])
        state = self._state()
        self.assertEqual(state["current"]["a"]["version"], "v3")
        self.assertEqual(state["versions"]["v3"], state["versions"]["v1"])
        self.assertNotEqual("v3", "v1")

    def test_disappearing_incidents_are_only_withdrawn(self):
        self._init()
        archive.publish(self.path, "b1", _payload(["a", "b"]))
        before = self._state()
        receipt = archive.publish(
            self.path, "b2",
            _payload(["a", "b"], maintenance_windows=_suppressing_windows()))
        self.assertEqual(receipt["published"], [])
        self.assertEqual(len(receipt["withdrawn"]), 1)
        self.assertEqual(receipt["withdrawn"][0]["version"], "v1")
        state = self._state()
        self.assertEqual(state["current"], {})
        self.assertEqual(state["records"][-1]["kind"], "withdraw")
        self.assertEqual(state["records"][-1]["version"], "v1")
        # No new version was allocated.
        self.assertEqual(state["next_version"], before["next_version"])
        # An empty follow-up batch changes nothing.
        receipt3 = archive.publish(
            self.path, "b3",
            _payload([], maintenance_windows=_suppressing_windows()))
        self.assertEqual(receipt3["record_ids"], [])
        self.assertEqual(self._state()["next_record"], state["next_record"])

    def test_reappearing_incident_never_reuses_version_identity(self):
        self._init()
        archive.publish(self.path, "b1", _payload(["a", "b"]))
        archive.publish(self.path, "b2",
                        _payload(["a", "b"],
                                 maintenance_windows=_suppressing_windows()))
        receipt = archive.publish(self.path, "b3", _payload(["a", "b"]))
        self.assertEqual([p["version"] for p in receipt["published"]], ["v2"])
        versions = set(self._state()["versions"])
        self.assertEqual(versions, {"v1", "v2"})

    def test_records_sorted_by_incident_id_with_withdraw_first(self):
        self._init()
        # Roughly 22 km of latitude apart, beyond the 10 km radius.
        positions = {"a": 0.0, "m": 0.2, "z": 0.4}
        payload = {"radius_km": 10.0, "window_minutes": 60,
                   "observations": [_obs(oid, severity=0.3, lat=lat)
                                    for oid, lat in positions.items()]}
        archive.publish(self.path, "b1", payload)
        self.assertEqual(
            [r["incident_id"] for r in self._state()["records"]],
            ["a", "m", "z"])
        # Change all three incidents at once: each withdraw precedes its
        # publish, and incidents stay lexicographically ordered.
        changed = copy.deepcopy(payload)
        for row in changed["observations"]:
            row["severity"] = 0.7
        archive.publish(self.path, "b2", changed)
        records = self._state()["records"]
        self.assertEqual([(r["incident_id"], r["kind"]) for r in records[3:]],
                         [("a", "withdraw"), ("a", "publish"),
                          ("m", "withdraw"), ("m", "publish"),
                          ("z", "withdraw"), ("z", "publish")])

    def test_replay_returns_stored_receipt_without_new_identities(self):
        self._init()
        payload = _payload(["a", "b"])
        receipt = archive.publish(self.path, "same-id", payload)
        state_before = self._state()
        replay = archive.publish(self.path, "same-id",
                                 json.loads(json.dumps(payload)))
        self.assertEqual(replay, receipt)
        self.assertEqual(self._state(), state_before)

    def test_same_batch_id_with_different_input_is_rejected(self):
        self._init()
        archive.publish(self.path, "b1", _payload(["a", "b"]))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ArchiveError, "different input"):
            archive.publish(self.path, "b1", _payload(["a", "b", "c"]))
        # Archive and acknowledgement state are untouched.
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_clustering_input_leaves_archive_unchanged(self):
        self._init()
        good_receipt = archive.publish(self.path, "b1", _payload(["a", "b"]))
        archive.acknowledge(self.path, "X", good_receipt["record_ids"][0])
        before = self.path.read_bytes()
        bad_payloads = [
            {"radius_km": -1, "window_minutes": 60, "observations": [_obs("a")]},
            {"radius_km": 1, "window_minutes": 60,
             "observations": [dict(_obs("a"), lat=95)]},
            {"radius_km": 1, "window_minutes": 60,
             "observations": [_obs("a"), _obs("a")]},
            {"window_minutes": 60, "observations": [_obs("a")]},
        ]
        for bad in bad_payloads:
            with self.assertRaises((ValueError, KeyError, TypeError)):
                archive.publish(self.path, f"bad-{id(bad)}", bad)
            self.assertEqual(self.path.read_bytes(), before)
        # The batch id was never consumed, so reusing it with good input works.
        retry = archive.publish(self.path, "b1", _payload(["a", "b"]))
        self.assertEqual(retry, good_receipt)

    def test_missing_archive_is_rejected(self):
        with self.assertRaises(ArchiveError):
            archive.publish(self.path, "b1", _payload(["a"]))


class ConsumerTests(ArchiveCase):
    def test_consumer_isolation_and_independent_ack_order(self):
        self._init()
        r1 = archive.publish(self.path, "b1", _payload(["a", "b"]))["record_ids"]
        archive.publish(self.path, "b2", _payload(["a", "b", "c"]))

        # Y works through its queue in order; X still sees everything Y acks.
        for record_id in r1:
            result = archive.acknowledge(self.path, "Y", record_id)
            self.assertTrue(result["acknowledged"])
            self.assertFalse(result["duplicate"])
        self.assertEqual(
            [r["record_id"] for r in archive.pending(self.path, "Y")],
            ["r2", "r3"])
        self.assertEqual(
            [r["record_id"] for r in archive.pending(self.path, "X")],
            ["r1", "r2", "r3"])

    def test_acknowledging_publish_does_not_ack_related_records(self):
        self._init()
        archive.publish(self.path, "b1", _payload(["a", "b"]))
        archive.publish(self.path, "b2", _payload(["a", "b", "c"]))
        archive.acknowledge(self.path, "X", "r1")
        acks = self._state()["acks"]["X"]
        self.assertEqual(acks, ["r1"])
        self.assertEqual(
            [r["record_id"] for r in archive.pending(self.path, "X")],
            ["r2", "r3"])

    def test_duplicate_acknowledgement_is_idempotent(self):
        self._init()
        receipt = archive.publish(self.path, "b1", _payload(["a", "b"]))
        record_id = receipt["record_ids"][0]
        first = archive.acknowledge(self.path, "X", record_id)
        second = archive.acknowledge(self.path, "X", record_id)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self._state()["acks"]["X"], [record_id])

    def test_pending_returns_immutable_event_copy(self):
        self._init()
        archive.publish(self.path, "b1", _payload(["a", "b"]))
        pending = archive.pending(self.path, "X")
        pending[0]["event"]["observation_ids"].append("tampered")
        again = archive.pending(self.path, "X")
        self.assertEqual(again[0]["event"]["observation_ids"], ["a", "b"])

    def test_unknown_consumer_and_record_are_rejected_without_state_change(self):
        self._init()
        archive.publish(self.path, "b1", _payload(["a", "b"]))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ArchiveError, "unknown consumer"):
            archive.pending(self.path, "nobody")
        with self.assertRaisesRegex(ArchiveError, "unknown consumer"):
            archive.acknowledge(self.path, "nobody", "r1")
        with self.assertRaisesRegex(ArchiveError, "unknown record"):
            archive.acknowledge(self.path, "X", "r999")
        self.assertEqual(self.path.read_bytes(), before)
        # Rejected ack cannot remove anything from history.
        self.assertEqual(len(self._state()["records"]), 1)


class PersistenceTests(ArchiveCase):
    def test_reopen_preserves_identities_order_and_acknowledgements(self):
        self._init()
        receipt1 = archive.publish(self.path, "b1", _payload(["a", "b"]))
        archive.acknowledge(self.path, "X", receipt1["record_ids"][0])
        archive.publish(self.path, "b2", _payload(["a", "b", "c"]))
        snapshot = self._state()

        # Reopen as a fresh process would: reload everything from disk.
        reloaded = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded, snapshot)
        self.assertEqual(
            [r["record_id"] for r in archive.pending(self.path, "X")],
            ["r2", "r3"])
        self.assertEqual(
            [r["record_id"] for r in archive.pending(self.path, "Y")],
            ["r1", "r2", "r3"])
        next_receipt = archive.publish(self.path, "b3", _payload(["a", "b"]))
        self.assertEqual(next_receipt["record_ids"], ["r4", "r5"])
        self.assertEqual(
            [p["version"] for p in next_receipt["published"]], ["v3"])


if __name__ == "__main__":
    unittest.main()
