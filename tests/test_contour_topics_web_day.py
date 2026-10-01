import json
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from web.contour_topics import (
    load_c1_contour_topic,
    load_c1_contour_topics,
)


def snapshot_payload():
    phrase = {
        "marker_id": "p1",
        "label": "тема дня",
        "publications": 2,
        "materials": 2,
        "sources": 2,
        "share": 100.0,
    }

    marker = {
        "marker_id": "p1",
        "unit": "phrases",
        "label": "тема дня",
        "publications": 2,
        "materials": 2,
        "sources": 2,
        "source_ranking": [],
        "evidence_total": 2,
        "evidence_limit_reached": False,
        "occurrence_refs": ["o1", "o2"],
    }

    return {
        "schema_version": "contour-topics-c1/1",
        "contour": {
            "monitoring_contour_id": 1,
            "object_id": None,
        },
        "window": {
            "days": 30,
        },
        "generated_at": "2026-09-26T12:00:00+00:00",
        "summary": {
            "all": {
                "current": {
                    "publications": 3,
                    "materials": 3,
                    "sources": 3,
                },
            },
        },
        "views": {
            "all": {
                "themes": {
                    "phrases": [phrase],
                },
            },
        },
        "clouds": {
            "all": {
                "phrases": [],
            },
        },
        "markers": {
            "p1": marker,
        },
        "comparison": {
            "all": {
                "phrases": {
                    "days": [
                        "2026-09-25",
                        "2026-09-26",
                    ],
                    "series": {
                        "p1": [0, 2],
                    },
                },
            },
        },
        "daily": {
            "2026-09-25": {
                "summary": {
                    "publications": 1,
                    "materials": 1,
                    "sources": 1,
                },
                "phrases": [],
                "markers": {},
            },
            "2026-09-26": {
                "summary": {
                    "publications": 2,
                    "materials": 2,
                    "sources": 2,
                },
                "phrases": [phrase],
                "markers": {
                    "p1": marker,
                },
            },
        },
        "occurrences": {
            "o1": {
                "occurrence_id": "o1",
                "published_at":
                    "2026-09-26T08:00:00+03:00",
                "collected_at":
                    "2026-09-26T08:01:00+03:00",
            },
            "o2": {
                "occurrence_id": "o2",
                "published_at": None,
                "collected_at":
                    "2026-09-26T09:00:00+03:00",
            },
        },
    }


class C1TopicsWebDayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = (
            Path(self.tmp.name)
            / "contour_topics_c1.latest.json"
        )
        self.write(snapshot_payload())

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload):
        self.path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_different_days_project_different_topics(self):
        empty = load_c1_contour_topics(
            self.path,
            object_id=None,
            day=date(2026, 9, 25),
        )

        populated = load_c1_contour_topics(
            self.path,
            object_id=None,
            day=date(2026, 9, 26),
        )

        self.assertEqual(empty["state"], "ready")
        self.assertEqual(
            empty["summary"]["materials"],
            1,
        )
        self.assertEqual(empty["phrases"], [])
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["cloud"], [])

        self.assertEqual(
            populated["state"],
            "ready",
        )
        self.assertEqual(
            populated["summary"]["materials"],
            2,
        )
        self.assertEqual(
            [row["marker_id"]
             for row in populated["phrases"]],
            ["p1"],
        )
        self.assertEqual(
            [row["marker_id"]
             for row in populated["items"]],
            ["p1"],
        )
        self.assertEqual(
            [row["marker_id"]
             for row in populated["cloud"]],
            ["p1"],
        )
        self.assertEqual(
            set(
                populated["comparison"]["series"]
            ),
            {"p1"},
        )

    def test_day_topic_detail_uses_day_evidence(self):
        detail = load_c1_contour_topic(
            self.path,
            marker_id="p1",
            object_id=None,
            day=date(2026, 9, 26),
        )

        self.assertEqual(
            detail["selected_day"],
            "2026-09-26",
        )
        self.assertEqual(
            detail["publications"],
            2,
        )

        kyiv = ZoneInfo("Europe/Kyiv")

        for row in detail["occurrences"]:
            observed = (
                row.get("published_at")
                or row["collected_at"]
            )

            self.assertEqual(
                datetime.fromisoformat(
                    observed
                ).astimezone(
                    kyiv
                ).date(),
                date(2026, 9, 26),
            )

    def test_invalid_schema_keeps_invalid_state(self):
        payload = snapshot_payload()
        payload["schema_version"] = "broken"
        self.write(payload)

        result = load_c1_contour_topics(
            self.path,
            object_id=None,
        )

        self.assertEqual(
            result,
            {
                "state": "invalid",
                "items": [],
            },
        )

    def test_scope_mismatch_keeps_invalid_state(self):
        payload = snapshot_payload()
        payload["contour"]["object_id"] = 99
        self.write(payload)

        result = load_c1_contour_topics(
            self.path,
            object_id=None,
        )

        self.assertEqual(
            result["state"],
            "invalid",
        )


if __name__ == "__main__":
    unittest.main()
