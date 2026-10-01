from datetime import date
import unittest

from web.contour_signals import (
    filter_c1_signal_candidates_by_day,
)


class C1SignalDayFilterTests(unittest.TestCase):
    def test_published_at_uses_kyiv_calendar_day(self):
        candidate = {
            "candidate_id": "s1",
            "chronology": [
                {
                    "published_at":
                        "2026-09-20T21:30:00+00:00",
                    "collected_at":
                        "2026-09-20T22:00:00+00:00",
                },
                {
                    "published_at":
                        "2026-09-22T08:00:00+00:00",
                    "collected_at":
                        "2026-09-22T08:05:00+00:00",
                },
            ],
        }

        result = filter_c1_signal_candidates_by_day(
            [candidate],
            day=date(2026, 9, 21),
        )

        self.assertEqual(result, [candidate])
        self.assertIs(result[0], candidate)
        self.assertEqual(
            len(result[0]["chronology"]),
            2,
        )

    def test_missing_publication_falls_back_to_collected_at(self):
        candidate = {
            "candidate_id": "s2",
            "chronology": [
                {
                    "published_at": None,
                    "collected_at":
                        "2026-09-25T22:15:00+00:00",
                }
            ],
        }

        result = filter_c1_signal_candidates_by_day(
            [candidate],
            day=date(2026, 9, 26),
        )

        self.assertEqual(result, [candidate])

    def test_nonmatching_signal_is_excluded(self):
        candidate = {
            "candidate_id": "s3",
            "chronology": [
                {
                    "published_at":
                        "2026-09-10T10:00:00+00:00",
                    "collected_at":
                        "2026-09-10T10:05:00+00:00",
                }
            ],
        }

        result = filter_c1_signal_candidates_by_day(
            [candidate],
            day=date(2026, 9, 26),
        )

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
