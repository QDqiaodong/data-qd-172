import json
import unittest
from pathlib import Path
from wildfire.core import cluster


class ClusterTests(unittest.TestCase):
    def test_fixture_forms_two_incidents(self):
        result = cluster(json.loads(Path("fixtures/input.json").read_text()))
        self.assertEqual(len(result["incidents"]), 2)
        self.assertEqual(result["incidents"][0]["observation_ids"], ["o1", "o2"])

    def test_rejects_invalid_coordinates(self):
        with self.assertRaisesRegex(ValueError, "coordinates"):
            cluster({"radius_km": 1, "window_minutes": 1, "observations": [{"id": "x", "sensor": "s", "time": "2026-01-01T00:00:00+00:00", "lat": 91, "lon": 0, "severity": 0.2}]})


if __name__ == "__main__":
    unittest.main()
