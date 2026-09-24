"""Регрес-тест: C1 supported-merge не повинен зливати через transitive-only bridge.

Дзеркалить test_supported_merge_rejects_transitive_chain (tests/test_signals.py)
для глобальної _merge_supported_groups, але проти реального build_snapshot()
з reporting/signals_c1_30d.py — коду, який патчили 24.09.2026.

Без complete_support+coverage (стара v5-логіка) саме такий A-B-C bridge
дав живий інцидент 21.09.2026: giant-компонент ~1726 матеріалів, що
транзитивно змішав непов'язані сюжети.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from reporting.signals_c1_30d import (
    CORE_DISTANCE,
    MERGE_DISTANCE,
    MERGE_MIN_CROSS_LINKS,
    build_snapshot,
)


AS_OF = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
COLLECTED = AS_OF - timedelta(hours=2)


def content_row(content_id: str, title: str) -> dict:
    return {
        "content_id": content_id,
        "first_seen": COLLECTED,
        "last_seen": COLLECTED,
        "title": title,
        "source_count": 1,
        "last_collected": COLLECTED,
    }


def occurrence_row(content_id: str, source_id: str) -> dict:
    return {
        "occurrence_id": f"occ-{content_id}",
        "content_id": content_id,
        "source_id": source_id,
        "source_name": f"Джерело {source_id}",
        "source_type": "rss",
        "source_group": "ua_space",
        "title": f"Заголовок {content_id}",
        "text": f"Текст {content_id}",
        "text_truncated": False,
        "external_ref": None,
        "published_at": COLLECTED,
        "collected_at": COLLECTED,
    }


class C1SupportedMergeTransitiveChainTest(unittest.TestCase):
    """A-B і B-C підтримані окремо (по 2 cross-links); A-C — жодного зв'язку.

    Очікування: A+B зливаються, C лишається окремим сигналом.
    Giant [[a,b,c]] означав би регресію до старої v5-поведінки.
    """

    def test_transitive_bridge_is_rejected(self):
        self.assertGreaterEqual(MERGE_MIN_CROSS_LINKS, 2)
        self.assertGreater(MERGE_DISTANCE, CORE_DISTANCE)

        cross_link_distance = (CORE_DISTANCE + MERGE_DISTANCE) / 2
        within_core = CORE_DISTANCE / 4

        contents = [
            content_row("a1", "A1"), content_row("a2", "A2"),
            content_row("b1", "B1"), content_row("b2", "B2"),
            content_row("c1", "C1"), content_row("c2", "C2"),
        ]

        occurrences = [
            occurrence_row("a1", "src-a1"), occurrence_row("a2", "src-a2"),
            occurrence_row("b1", "src-b1"), occurrence_row("b2", "src-b2"),
            occurrence_row("c1", "src-c1"), occurrence_row("c2", "src-c2"),
        ]

        pair_rows = [
            {"left_id": "a1", "right_id": "a2", "distance": within_core},
            {"left_id": "b1", "right_id": "b2", "distance": within_core},
            {"left_id": "c1", "right_id": "c2", "distance": within_core},
            {"left_id": "a1", "right_id": "b1", "distance": cross_link_distance},
            {"left_id": "a2", "right_id": "b2", "distance": cross_link_distance},
            {"left_id": "b1", "right_id": "c1", "distance": cross_link_distance},
            {"left_id": "b2", "right_id": "c2", "distance": cross_link_distance},
        ]

        snapshot = build_snapshot(
            as_of=AS_OF,
            start=AS_OF - timedelta(days=30),
            contents=contents,
            pair_rows=pair_rows,
            occurrences=occurrences,
            object_id=None,
        )

        self.assertEqual(snapshot["summary"]["strict_group_count"], 3)
        self.assertEqual(snapshot["summary"]["accepted_supported_merges"], 1)
        self.assertEqual(snapshot["summary"]["final_group_count"], 2)

        content_id_groups = sorted(
            sorted(candidate["content_ids"])
            for candidate in snapshot["candidates"]
        )

        self.assertEqual(
            content_id_groups,
            [["a1", "a2", "b1", "b2"], ["c1", "c2"]],
        )


if __name__ == "__main__":
    unittest.main()
