import unittest
from unittest.mock import MagicMock, patch

from web.annotation_store import (
    annotation_write_connection,
    ensure_signal_context,
    set_content_contour_annotation,
    set_signal_contour_annotation,
    set_signal_membership_annotation,
)


CONTENT_A = "00000000-0000-0000-0000-000000000001"
CONTENT_B = "00000000-0000-0000-0000-000000000002"
CONTEXT = "10000000-0000-0000-0000-000000000001"


def fake_connection():
    conn = MagicMock()
    cursor = MagicMock()

    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False

    return conn, cursor


class SignalContextStoreTests(unittest.TestCase):
    def test_new_context_writes_exact_members(self):
        conn, cursor = fake_connection()

        cursor.fetchone.return_value = {
            "signal_context_id": CONTEXT,
        }
        cursor.fetchall.return_value = [
            {"content_id": CONTENT_A},
            {"content_id": CONTENT_B},
        ]

        result = ensure_signal_context(
            conn,
            content_ids=[
                CONTENT_B,
                CONTENT_A,
            ],
            representative_content_id=CONTENT_A,
        )

        self.assertEqual(result, CONTEXT)
        cursor.executemany.assert_called_once()

        params = cursor.executemany.call_args.args[1]

        self.assertEqual(
            params,
            [
                (CONTEXT, CONTENT_A),
                (CONTEXT, CONTENT_B),
            ],
        )

    def test_existing_context_is_not_rewritten(self):
        conn, cursor = fake_connection()

        cursor.fetchone.side_effect = [
            None,
            {
                "signal_context_id":
                    CONTEXT,
            },
        ]
        cursor.fetchall.return_value = [
            {"content_id": CONTENT_A},
            {"content_id": CONTENT_B},
        ]

        result = ensure_signal_context(
            conn,
            content_ids=[
                CONTENT_A,
                CONTENT_B,
            ],
            representative_content_id=CONTENT_B,
        )

        self.assertEqual(result, CONTEXT)
        cursor.executemany.assert_not_called()

    def test_membership_mismatch_is_rejected(self):
        conn, cursor = fake_connection()

        cursor.fetchone.return_value = {
            "signal_context_id": CONTEXT,
        }
        cursor.fetchall.return_value = [
            {"content_id": CONTENT_A},
        ]

        with self.assertRaises(ValueError):
            ensure_signal_context(
                conn,
                content_ids=[
                    CONTENT_A,
                    CONTENT_B,
                ],
                representative_content_id=CONTENT_A,
            )


class AnnotationUpsertTests(unittest.TestCase):
    def test_content_contour_upsert(self):
        conn, cursor = fake_connection()

        set_content_contour_annotation(
            conn,
            content_id=CONTENT_A,
            monitoring_contour_id=4,
            included=True,
        )

        sql = cursor.execute.call_args.args[0]

        self.assertIn(
            "analyst_content_contour_annotations",
            sql,
        )
        self.assertEqual(
            cursor.execute.call_args.args[1],
            (
                CONTENT_A,
                4,
                True,
            ),
        )

    def test_signal_contour_upsert(self):
        conn, cursor = fake_connection()

        set_signal_contour_annotation(
            conn,
            signal_context_id=CONTEXT,
            monitoring_contour_id=1,
            included=False,
        )

        sql = cursor.execute.call_args.args[0]

        self.assertIn(
            "analyst_signal_contour_annotations",
            sql,
        )

    def test_membership_requires_boolean(self):
        conn, _ = fake_connection()

        with self.assertRaises(ValueError):
            set_signal_membership_annotation(
                conn,
                signal_context_id=CONTEXT,
                content_id=CONTENT_A,
                included=1,
            )


class AnnotationTransactionTests(unittest.TestCase):
    @patch(
        "web.annotation_store.psycopg.connect"
    )
    def test_success_commits(self, connect):
        conn = MagicMock()
        connect.return_value = conn

        with annotation_write_connection() as yielded:
            self.assertIs(yielded, conn)

        conn.commit.assert_called_once_with()
        conn.rollback.assert_not_called()
        conn.close.assert_called_once_with()

    @patch(
        "web.annotation_store.psycopg.connect"
    )
    def test_failure_rolls_back(self, connect):
        conn = MagicMock()
        connect.return_value = conn

        with self.assertRaises(RuntimeError):
            with annotation_write_connection():
                raise RuntimeError("boom")

        conn.commit.assert_not_called()
        conn.rollback.assert_called_once_with()
        conn.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()


class AnnotationClearTests(unittest.TestCase):
    def test_clear_signal_contour(self):
        from web.annotation_store import (
            clear_signal_contour_annotation,
        )

        conn, cursor = fake_connection()

        clear_signal_contour_annotation(
            conn,
            signal_context_id=CONTEXT,
            monitoring_contour_id=1,
        )

        sql = cursor.execute.call_args.args[0]

        self.assertIn(
            "DELETE FROM analyst_signal_contour_annotations",
            sql,
        )

    def test_clear_content_contour(self):
        from web.annotation_store import (
            clear_content_contour_annotation,
        )

        conn, cursor = fake_connection()

        clear_content_contour_annotation(
            conn,
            content_id=CONTENT_A,
            monitoring_contour_id=2,
        )

        sql = cursor.execute.call_args.args[0]

        self.assertIn(
            "DELETE FROM analyst_content_contour_annotations",
            sql,
        )

    def test_require_active_contour_rejects_missing(self):
        from web.annotation_store import (
            require_active_contour,
        )

        conn, cursor = fake_connection()
        cursor.fetchone.return_value = None

        with self.assertRaises(ValueError):
            require_active_contour(
                conn,
                99,
            )


if __name__ == "__main__":
    unittest.main()
