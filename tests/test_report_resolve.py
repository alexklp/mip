import unittest

from web.report_resolve import (
    choose_example_publication,
    strip_repeated_title,
)


class ReportResolvePureTests(
    unittest.TestCase
):
    def test_representative_linked_publication_wins(self):
        rows = [
            {
                "content_id": "a",
                "url": "https://example.com/a",
                "title": "A",
                "text": "Text A",
            },
            {
                "content_id": "b",
                "url": "https://example.com/b",
                "title": "B",
                "text": "Text B",
            },
        ]

        selected = (
            choose_example_publication(
                rows,
                representative_content_id="b",
            )
        )

        self.assertEqual(
            selected["content_id"],
            "b",
        )

    def test_linked_publication_is_preferred(self):
        rows = [
            {
                "content_id": "a",
                "url": "",
                "title": "A",
                "text": "Text A",
            },
            {
                "content_id": "b",
                "url": "https://example.com/b",
                "title": "B",
                "text": "Text B",
            },
        ]

        selected = (
            choose_example_publication(
                rows,
                representative_content_id=None,
            )
        )

        self.assertEqual(
            selected["content_id"],
            "b",
        )

    def test_repeated_title_is_removed(self):
        result = strip_repeated_title(
            (
                "Заголовок матеріалу\n"
                "Основний текст публікації."
            ),
            "Заголовок матеріалу",
        )

        self.assertEqual(
            result,
            "Основний текст публікації.",
        )

    def test_unrelated_text_is_preserved(self):
        result = strip_repeated_title(
            "Інший текст.",
            "Заголовок",
        )

        self.assertEqual(
            result,
            "Інший текст.",
        )


if __name__ == "__main__":
    unittest.main()


class LegacySignalIdentityTests(
    unittest.TestCase
):
    def test_old_candidate_id_can_be_content_anchor(self):
        from web.report_resolve import (
            _resolve_legacy_candidate,
        )

        old_id = (
            "11111111-1111-1111-1111-111111111111"
        )

        new_id = (
            "22222222-2222-2222-2222-222222222222"
        )

        snapshot = {
            "candidates": [
                {
                    "candidate_id":
                        new_id,
                    "content_ids": [
                        old_id,
                        "33333333-3333-3333-3333-333333333333",
                    ],
                    "representative_title":
                        "Нова репрезентативна назва",
                    "chronology": [],
                    "ai_theses": None,
                }
            ]
        }

        candidate = (
            _resolve_legacy_candidate(
                snapshot,
                {
                    "candidate_id":
                        old_id,
                    "title":
                        "Стара назва",
                    "factual_summary":
                        "",
                },
            )
        )

        self.assertEqual(
            candidate[
                "candidate_id"
            ],
            new_id,
        )

    def test_legacy_exact_title_is_allowed(self):
        from web.report_resolve import (
            _resolve_legacy_candidate,
        )

        snapshot = {
            "candidates": [
                {
                    "candidate_id":
                        "22222222-2222-2222-2222-222222222222",
                    "content_ids": [
                        "33333333-3333-3333-3333-333333333333",
                    ],
                    "representative_title":
                        "Точна назва",
                    "chronology": [],
                    "ai_theses": None,
                }
            ]
        }

        candidate = (
            _resolve_legacy_candidate(
                snapshot,
                {
                    "candidate_id":
                        "11111111-1111-1111-1111-111111111111",
                    "title":
                        "Точна назва",
                    "factual_summary":
                        "",
                },
            )
        )

        self.assertEqual(
            candidate[
                "representative_title"
            ],
            "Точна назва",
        )



class SignalSummaryProvenanceTests(
    unittest.TestCase
):
    def test_durable_signal_uses_current_ai_summary(self):
        from web.report_resolve import (
            _select_signal_summary,
        )

        self.assertEqual(
            _select_signal_summary(
                durable_membership=True,
                factual_summary="Старий snapshot",
                factual_summary_override=None,
                ai_summary="Актуальне коротко",
            ),
            "Актуальне коротко",
        )

    def test_durable_signal_uses_analyst_override(self):
        from web.report_resolve import (
            _select_signal_summary,
        )

        self.assertEqual(
            _select_signal_summary(
                durable_membership=True,
                factual_summary="Старий snapshot",
                factual_summary_override=
                    "Текст аналітика",
                ai_summary="Актуальне коротко",
            ),
            "Текст аналітика",
        )

    def test_empty_override_suppresses_summary(self):
        from web.report_resolve import (
            _select_signal_summary,
        )

        self.assertEqual(
            _select_signal_summary(
                durable_membership=True,
                factual_summary="Старий snapshot",
                factual_summary_override="",
                ai_summary="Актуальне коротко",
            ),
            "",
        )

    def test_legacy_signal_preserves_old_behavior(self):
        from web.report_resolve import (
            _select_signal_summary,
        )

        self.assertEqual(
            _select_signal_summary(
                durable_membership=False,
                factual_summary="Legacy текст",
                factual_summary_override=None,
                ai_summary="Актуальне коротко",
            ),
            "Legacy текст",
        )
