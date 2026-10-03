import unittest

from web.analyst_annotations import (
    SOURCE_CONTENT,
    SOURCE_SIGNAL,
    SOURCE_SYSTEM,
    canonical_signal_members,
    resolve_effective_contours,
    signal_fingerprint,
    validate_signal_context,
)


CONTENT_A = "00000000-0000-0000-0000-000000000001"
CONTENT_B = "00000000-0000-0000-0000-000000000002"
CONTENT_C = "00000000-0000-0000-0000-000000000003"


class SignalFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_order_independent(self):
        left = signal_fingerprint(
            [CONTENT_A, CONTENT_B, CONTENT_C]
        )
        right = signal_fingerprint(
            [CONTENT_C, CONTENT_A, CONTENT_B]
        )

        self.assertEqual(left, right)
        self.assertEqual(len(left), 64)

    def test_single_content_signal_is_valid(self):
        members = canonical_signal_members(
            [CONTENT_A]
        )

        self.assertEqual(
            members,
            (CONTENT_A,),
        )

    def test_duplicate_content_is_rejected(self):
        with self.assertRaises(ValueError):
            canonical_signal_members(
                [CONTENT_A, CONTENT_A]
            )

    def test_representative_must_be_member(self):
        with self.assertRaises(ValueError):
            validate_signal_context(
                content_ids=[
                    CONTENT_A,
                    CONTENT_B,
                ],
                representative_content_id=CONTENT_C,
            )


class EffectiveContourTests(unittest.TestCase):
    def test_system_assignment_is_used_without_human_override(self):
        result = resolve_effective_contours(
            system_contour_ids=[1],
            signal_annotations={},
            content_annotations={},
        )

        self.assertEqual(
            result[1],
            {
                "included": True,
                "source": SOURCE_SYSTEM,
            },
        )

    def test_signal_annotation_overrides_system(self):
        result = resolve_effective_contours(
            system_contour_ids=[1],
            signal_annotations={1: False},
            content_annotations={},
        )

        self.assertEqual(
            result[1],
            {
                "included": False,
                "source": SOURCE_SIGNAL,
            },
        )

    def test_signal_annotation_can_add_contour(self):
        result = resolve_effective_contours(
            system_contour_ids=[],
            signal_annotations={4: True},
            content_annotations={},
        )

        self.assertEqual(
            result[4],
            {
                "included": True,
                "source": SOURCE_SIGNAL,
            },
        )

    def test_content_annotation_overrides_signal(self):
        result = resolve_effective_contours(
            system_contour_ids=[1],
            signal_annotations={1: True},
            content_annotations={1: False},
        )

        self.assertEqual(
            result[1],
            {
                "included": False,
                "source": SOURCE_CONTENT,
            },
        )

    def test_content_include_can_override_signal_exclude(self):
        result = resolve_effective_contours(
            system_contour_ids=[1],
            signal_annotations={1: False},
            content_annotations={1: True},
        )

        self.assertEqual(
            result[1],
            {
                "included": True,
                "source": SOURCE_CONTENT,
            },
        )

    def test_unrelated_system_contours_are_preserved(self):
        result = resolve_effective_contours(
            system_contour_ids=[1, 2],
            signal_annotations={1: False},
            content_annotations={},
        )

        self.assertEqual(
            result[2],
            {
                "included": True,
                "source": SOURCE_SYSTEM,
            },
        )


if __name__ == "__main__":
    unittest.main()
