from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reporting.signal_theses import (
    build_cache_record,
    canonical_materials,
    make_input_hash,
    write_cache_record,
)
from web.signal_theses import (
    attach_ready_theses,
    load_ready_theses_index,
)


class SignalThesesWebTests(
    unittest.TestCase
):
    def setUp(self):
        self.materials = canonical_materials(
            [
                {
                    "content_id": "b",
                    "title": "Б",
                    "text": "Текст Б",
                },
                {
                    "content_id": "a",
                    "title": "А",
                    "text": "Текст А",
                },
            ],
            expected_content_ids=[
                "b",
                "a",
            ],
        )

        self.result = {
            "summary":
                "Коротке узагальнення.",
            "theses": [
                {
                    "text": "Перша теза.",
                    "evidence_material_ids": [1],
                },
                {
                    "text": "Друга теза.",
                    "evidence_material_ids": [2],
                },
            ],
        }

    def _record(self):
        input_hash = make_input_hash(
            self.materials
        )

        return build_cache_record(
            input_hash=input_hash,
            materials=self.materials,
            model_result=self.result,
            generated_at=datetime(
                2026,
                9,
                28,
                12,
                0,
                tzinfo=timezone.utc,
            ),
        )

    def test_ready_cache_matches_by_exact_membership(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            write_cache_record(
                self._record(),
                state_root=root,
            )

            candidates = [
                {
                    "candidate_id": "global-id",
                    "content_ids": ["b", "a"],
                },
                {
                    "candidate_id": "c1-id",
                    "content_ids": ["a", "b"],
                },
                {
                    "candidate_id": "different",
                    "content_ids": ["a"],
                },
            ]

            attach_ready_theses(
                candidates,
                state_root=root,
            )

            self.assertIsNotNone(
                candidates[0]["ai_theses"]
            )

            self.assertEqual(
                candidates[0]["ai_theses"],
                candidates[1]["ai_theses"],
            )

            self.assertIsNone(
                candidates[2]["ai_theses"]
            )

    def test_corrupt_cache_is_ignored(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir(parents=True)

            (
                cache
                / ("a" * 64 + ".json")
            ).write_text(
                "{broken",
                encoding="utf-8",
            )

            self.assertEqual(
                load_ready_theses_index(
                    state_root=root
                ),
                {},
            )


if __name__ == "__main__":
    unittest.main()
