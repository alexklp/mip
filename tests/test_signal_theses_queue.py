from __future__ import annotations

import unittest

from reporting.signal_theses_queue import (
    SignalReference,
    SignalThesesQueueError,
    candidate_reference,
    canonical_content_ids,
    group_references,
)


class SignalThesesQueueTests(
    unittest.TestCase
):
    def test_content_ids_are_canonical(self):
        self.assertEqual(
            canonical_content_ids(
                ["b", "a"]
            ),
            ("a", "b"),
        )

    def test_duplicate_content_id_rejected(self):
        with self.assertRaises(
            SignalThesesQueueError
        ):
            canonical_content_ids(
                ["a", "a"]
            )

    def test_candidate_identity_not_part_of_group(self):
        first = candidate_reference(
            scope="global",
            rank=0,
            candidate={
                "candidate_id": "candidate-global",
                "content_ids": ["b", "a"],
            },
        )

        second = candidate_reference(
            scope="c1:object:7",
            rank=3,
            candidate={
                "candidate_id": "candidate-c1",
                "content_ids": ["a", "b"],
            },
        )

        grouped = group_references(
            [first, second]
        )

        self.assertEqual(
            len(grouped),
            1,
        )

        refs = grouped[("a", "b")]

        self.assertEqual(
            {
                ref.scope
                for ref in refs
            },
            {
                "global",
                "c1:object:7",
            },
        )

    def test_different_membership_is_different_task(self):
        refs = [
            SignalReference(
                scope="global",
                candidate_id="one",
                content_ids=("a", "b"),
                rank=0,
            ),
            SignalReference(
                scope="c1:all",
                candidate_id="two",
                content_ids=("a",),
                rank=0,
            ),
        ]

        grouped = group_references(
            refs
        )

        self.assertEqual(
            set(grouped),
            {
                ("a", "b"),
                ("a",),
            },
        )


if __name__ == "__main__":
    unittest.main()
