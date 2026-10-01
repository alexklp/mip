from datetime import datetime, timezone
import unittest
from contour_topics_c1 import (
    build_daily_phrase_views,
)


def row(
    occurrence_id,
    content_id,
    source_id,
    observed_at,
):
    return {
        "occurrence_id": occurrence_id,
        "content_id": content_id,
        "source_id": source_id,
        "source_name": source_id,
        "source_type": "rss",
        "source_url_or_handle": None,
        "source_group": "ua_space",
        "external_ref": None,
        "published_at": observed_at,
        "collected_at": observed_at,
        "observed_at": observed_at,
        "text_content": "",
        "routing_version": "test",
    }


class C1TopicsDayViewsTests(unittest.TestCase):
    def test_day_view_is_exact_and_scoped(
        self,
    ):
        d24a = datetime(
            2026, 9, 24, 8,
            tzinfo=timezone.utc,
        )
        d24b = datetime(
            2026, 9, 24, 18,
            tzinfo=timezone.utc,
        )
        d25 = datetime(
            2026, 9, 25, 10,
            tzinfo=timezone.utc,
        )

        rows = [
            row("o1", "c1", "s1", d24a),
            row("o2", "c2", "s2", d24b),
            row("o3", "c3", "s3", d25),
        ]

        content_markers = {
            "c1": {
                "phrases": {"p1"},
            },
            "c2": {
                "phrases": {"p1"},
            },
            "c3": {
                "phrases": {"p2"},
            },
        }

        marker_meta = {
            "p1": {
                "unit": "phrases",
                "label": "тема дня",
            },
            "p2": {
                "unit": "phrases",
                "label": "інша тема",
            },
        }

        daily, marker_ids, refs = (
            build_daily_phrase_views(
                rows,
                content_markers,
                marker_meta,
                as_of=datetime(
                    2026, 9, 25, 12,
                    tzinfo=timezone.utc,
                ),
                days=2,
            )
        )

        day24 = daily["2026-09-24"]
        day25 = daily["2026-09-25"]

        self.assertEqual(
            day24["summary"],
            {
                "publications": 2,
                "materials": 2,
                "sources": 2,
            },
        )

        self.assertEqual(
            len(day24["phrases"]),
            1,
        )
        self.assertNotIn(
            "cloud",
            day24,
        )
        self.assertEqual(
            day24["phrases"][0][
                "marker_id"
            ],
            "p1",
        )
        self.assertEqual(
            day24["phrases"][0][
                "publications"
            ],
            2,
        )

        self.assertEqual(
            day25["phrases"],
            [],
        )

        self.assertEqual(
            marker_ids,
            {"p1"},
        )
        self.assertEqual(
            refs,
            {"o1", "o2"},
        )

        detail = day24[
            "markers"
        ]["p1"]

        self.assertEqual(
            detail["materials"],
            2,
        )
        self.assertEqual(
            detail["sources"],
            2,
        )
        self.assertEqual(
            detail["occurrence_refs"],
            ["o2", "o1"],
        )


if __name__ == "__main__":
    unittest.main()
