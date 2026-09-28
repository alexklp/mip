from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reporting.signal_theses import (
    MODEL_NAME,
    PROMPT_VERSION,
    SignalThesesContractError,
    build_cache_record,
    canonical_materials,
    make_input_hash,
    read_cache_record,
    validate_model_result,
    write_cache_record,
)


class SignalThesesTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {
                "content_id": "b",
                "title": "Другий",
                "text_content": "Текст Б",
            },
            {
                "content_id": "a",
                "title": "Перший",
                "text_content": "Текст А",
            },
        ]

        self.materials = canonical_materials(
            self.rows,
            expected_content_ids=["a", "b"],
        )

        self.model_result = {
            "summary": "Коротке узагальнення сигналу.",
            "theses": [
                {
                    "text": "Перша теза.",
                    "evidence_material_ids": [1],
                },
                {
                    "text": "Друга теза.",
                    "evidence_material_ids": [2, 1],
                },
            ],
        }

    def test_canonical_order_and_exact_membership(self):
        self.assertEqual(
            [row["content_id"] for row in self.materials],
            ["a", "b"],
        )

        with self.assertRaises(
            SignalThesesContractError
        ):
            canonical_materials(
                self.rows[:1],
                expected_content_ids=["a", "b"],
            )

    def test_hash_is_order_and_scope_independent(self):
        first = make_input_hash(
            self.materials
        )

        reversed_materials = list(
            reversed(self.materials)
        )

        second = make_input_hash(
            reversed_materials
        )

        self.assertEqual(first, second)

    def test_hash_changes_with_input_or_contract(self):
        baseline = make_input_hash(
            self.materials
        )

        changed = [
            dict(row)
            for row in self.materials
        ]
        changed[0]["text"] += " змінено"

        self.assertNotEqual(
            baseline,
            make_input_hash(changed),
        )

        self.assertNotEqual(
            baseline,
            make_input_hash(
                self.materials,
                prompt_version=PROMPT_VERSION + "-next",
            ),
        )

        self.assertNotEqual(
            baseline,
            make_input_hash(
                self.materials,
                model_name=MODEL_NAME + "-next",
            ),
        )

    def test_model_result_contract(self):
        result = validate_model_result(
            self.model_result,
            material_count=2,
        )

        self.assertEqual(
            len(result["theses"]),
            2,
        )

        bad = {
            "summary": "X",
            "theses": [
                {
                    "text": "A",
                    "evidence_material_ids": [1, 2, 3, 4],
                },
                {
                    "text": "B",
                    "evidence_material_ids": [1],
                },
            ],
        }

        with self.assertRaises(
            SignalThesesContractError
        ):
            validate_model_result(
                bad,
                material_count=4,
            )

    def test_cache_maps_material_ids_to_content_ids(self):
        input_hash = make_input_hash(
            self.materials
        )

        record = build_cache_record(
            input_hash=input_hash,
            materials=self.materials,
            model_result=self.model_result,
            generated_at=datetime(
                2026,
                9,
                28,
                12,
                0,
                tzinfo=timezone.utc,
            ),
        )

        self.assertEqual(
            record["theses"][0][
                "evidence_content_ids"
            ],
            ["a"],
        )

        self.assertEqual(
            record["theses"][1][
                "evidence_content_ids"
            ],
            ["b", "a"],
        )

    def test_cache_roundtrip_is_atomic_and_validated(self):
        input_hash = make_input_hash(
            self.materials
        )

        record = build_cache_record(
            input_hash=input_hash,
            materials=self.materials,
            model_result=self.model_result,
            generated_at=datetime(
                2026,
                9,
                28,
                12,
                0,
                tzinfo=timezone.utc,
            ),
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            self.assertIsNone(
                read_cache_record(
                    input_hash,
                    state_root=root,
                )
            )

            path = write_cache_record(
                record,
                state_root=root,
            )

            self.assertTrue(path.exists())

            loaded = read_cache_record(
                input_hash,
                state_root=root,
            )

            self.assertEqual(
                loaded,
                record,
            )


if __name__ == "__main__":
    unittest.main()
