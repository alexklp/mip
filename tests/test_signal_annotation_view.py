import unittest
from unittest.mock import MagicMock

from web.signal_annotation_view import (
    fetch_signal_annotation_view,
)


CONTENT_A = "00000000-0000-0000-0000-000000000001"
CONTENT_B = "00000000-0000-0000-0000-000000000002"
CONTEXT = "10000000-0000-0000-0000-000000000001"


def fake_connection(*, fetchone, fetchall):
    conn = MagicMock()
    cursor = MagicMock()

    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False

    cursor.fetchone.side_effect = fetchone
    cursor.fetchall.side_effect = fetchall

    return conn


class SignalAnnotationViewTests(unittest.TestCase):
    def test_no_context_uses_system_and_material_override(self):
        conn = fake_connection(
            fetchone=[
                None,
            ],
            fetchall=[
                [
                    {
                        "monitoring_contour_id": 1,
                        "code": "dshv_objects",
                        "name": "Об'єкти ДШВ ЗСУ",
                    },
                    {
                        "monitoring_contour_id": 4,
                        "code": "enemy_media",
                        "name": "Моніторинг ворожих медіа",
                    },
                ],
                [
                    {
                        "content_id": CONTENT_A,
                        "monitoring_contour_id": 1,
                        "included": False,
                    },
                ],
                [
                    {
                        "content_id": CONTENT_A,
                        "monitoring_contour_id": 1,
                    },
                    {
                        "content_id": CONTENT_B,
                        "monitoring_contour_id": 4,
                    },
                ],
            ],
        )

        result = fetch_signal_annotation_view(
            conn,
            {
                "content_ids": [
                    CONTENT_A,
                    CONTENT_B,
                ],
            },
        )

        self.assertIsNone(
            result["signal_context_id"]
        )

        self.assertEqual(
            result["materials"][CONTENT_A]["contours"],
            [
                {
                    "monitoring_contour_id": 1,
                    "included": False,
                    "source": "manual_content",
                },
            ],
        )

        self.assertEqual(
            result["materials"][CONTENT_B]["contours"],
            [
                {
                    "monitoring_contour_id": 4,
                    "included": True,
                    "source": "system",
                },
            ],
        )

    def test_context_applies_signal_and_membership_correction(self):
        conn = fake_connection(
            fetchone=[
                {
                    "signal_context_id":
                        CONTEXT,
                },
            ],
            fetchall=[
                [
                    {
                        "monitoring_contour_id": 1,
                        "code": "dshv_objects",
                        "name": "Об'єкти ДШВ ЗСУ",
                    },
                ],
                [
                    {"content_id": CONTENT_A},
                    {"content_id": CONTENT_B},
                ],
                [
                    {
                        "monitoring_contour_id": 1,
                        "included": True,
                    },
                ],
                [
                    {
                        "content_id": CONTENT_B,
                        "included": False,
                    },
                ],
                [],
                [],
            ],
        )

        result = fetch_signal_annotation_view(
            conn,
            {
                "content_ids": [
                    CONTENT_A,
                    CONTENT_B,
                ],
            },
        )

        self.assertEqual(
            result["signal_context_id"],
            CONTEXT,
        )

        self.assertEqual(
            result["excluded_content_ids"],
            [CONTENT_B],
        )

        self.assertEqual(
            result["materials"][CONTENT_A]["contours"],
            [
                {
                    "monitoring_contour_id": 1,
                    "included": True,
                    "source": "manual_signal",
                },
            ],
        )

        self.assertTrue(
            result["materials"][CONTENT_B][
                "excluded_from_signal"
            ]
        )


if __name__ == "__main__":
    unittest.main()
