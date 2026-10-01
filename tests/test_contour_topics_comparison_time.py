from datetime import datetime, timezone
import unittest

from reporting.contour_topics_c1 import (
    build_daily_comparison,
)


class C1ComparisonTimeTests(unittest.TestCase):
    def test_collected_fallback_uses_kyiv_calendar_day(self):
        marker_id = "phrases:test"

        rows = [
            {
                "content_id": "c1",
                "published_at": None,
                "collected_at": datetime(
                    2026, 9, 24, 21, 30,
                    tzinfo=timezone.utc,
                ),
                "observed_at": datetime(
                    2026, 9, 24, 21, 30,
                    tzinfo=timezone.utc,
                ),
            }
        ]

        content_markers = {
            "c1": {
                "phrases": {marker_id},
            }
        }

        result = build_daily_comparison(
            rows,
            content_markers,
            {marker_id},
            as_of=datetime(
                2026, 9, 25, 22, 0,
                tzinfo=timezone.utc,
            ),
            days=30,
        )

        index = result["days"].index(
            "2026-09-25"
        )

        self.assertEqual(
            result["series"][marker_id][index],
            1,
        )

        self.assertEqual(
            result["missing_published_at"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
