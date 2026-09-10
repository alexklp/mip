import unittest
from pathlib import Path
from unittest.mock import patch

from web.topics import load_topic_marker


def slice_row(publications, *, occurrence_refs=None):
    return {
        "publications": publications,
        "materials": publications,
        "sources": 1 if publications else 0,
        "share": float(publications),
        "hourly": [publications] + [0] * 23,
        "hourly_sources": (
            [[{"name": "Source", "publications": publications}]]
            + [[] for _ in range(23)]
            if publications
            else [[] for _ in range(24)]
        ),
        "source_ranking": (
            [{
                "source_id": "s1",
                "name": "Source",
                "source_type": "rss",
                "url_or_handle": "https://example.invalid/rss",
                "publications": publications,
            }]
            if publications
            else []
        ),
        "evidence_total": publications,
        "evidence_limit_reached": False,
        "occurrence_refs": occurrence_refs or [],
    }


def snapshot():
    current_all = slice_row(
        3,
        occurrence_refs=["o1", "o2", "o3"],
    )
    previous_all = slice_row(1)

    return {
        "windows": {
            "previous": {
                "start": "2026-09-08T05:00:00+00:00",
                "end": "2026-09-09T05:00:00+00:00",
            },
            "current": {
                "start": "2026-09-09T05:00:00+00:00",
                "end": "2026-09-10T05:00:00+00:00",
            },
        },
        "markers": {
            "phrases:test": {
                "marker_id": "phrases:test",
                "unit": "phrases",
                "label": "test marker",
                "views": {
                    "all": {
                        "current": current_all,
                        "previous": previous_all,
                    },
                    "ru_space": {
                        "current": slice_row(2),
                        "previous": slice_row(1),
                    },
                    "ua_space": {
                        "current": slice_row(1),
                        "previous": slice_row(0),
                    },
                },
            },
        },
        "occurrences": {
            "o1": {"occurrence_id": "o1", "source_name": "Source"},
            "o2": {"occurrence_id": "o2", "source_name": "Source"},
            "o3": {"occurrence_id": "o3", "source_name": "Source"},
        },
    }


class TopicMarkerTests(unittest.TestCase):
    @patch("web.topics._read_snapshot")
    def test_all_view_contract(self, read_snapshot):
        read_snapshot.return_value = snapshot()

        result = load_topic_marker(
            Path("ignored.json"),
            marker_id="phrases:test",
            view="all",
        )

        self.assertEqual(
            result["marker"]["label"],
            "test marker",
        )
        self.assertEqual(
            result["current"]["publications"],
            3,
        )
        self.assertEqual(
            len(result["current"]["hourly"]),
            24,
        )
        self.assertEqual(
            len(result["current"]["hourly_sources"]),
            24,
        )
        self.assertEqual(
            len(result["current"]["occurrences"]),
            3,
        )

        breakdown = result["current"]["space_breakdown"]

        self.assertEqual(
            breakdown["ru_space"]["publications"],
            2,
        )
        self.assertEqual(
            breakdown["ua_space"]["publications"],
            1,
        )
        self.assertEqual(
            breakdown["ru_space"]["publications"]
            + breakdown["ua_space"]["publications"],
            result["current"]["publications"],
        )

    @patch("web.topics._read_snapshot")
    def test_single_space_has_no_breakdown(
        self,
        read_snapshot,
    ):
        read_snapshot.return_value = snapshot()

        result = load_topic_marker(
            Path("ignored.json"),
            marker_id="phrases:test",
            view="ru_space",
        )

        self.assertNotIn(
            "space_breakdown",
            result["current"],
        )

    def test_invalid_view_rejected(self):
        with self.assertRaises(ValueError):
            load_topic_marker(
                Path("ignored.json"),
                marker_id="phrases:test",
                view="invalid",
            )

    @patch("web.topics._read_snapshot")
    def test_missing_marker_rejected(
        self,
        read_snapshot,
    ):
        read_snapshot.return_value = snapshot()

        with self.assertRaises(KeyError):
            load_topic_marker(
                Path("ignored.json"),
                marker_id="missing",
                view="all",
            )


if __name__ == "__main__":
    unittest.main()
