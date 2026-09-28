from __future__ import annotations

import unittest

from reporting.signal_theses_queue import (
    SignalReference,
    SignalTask,
)
from reporting.signal_theses_worker import (
    build_prompt,
    parse_model_response,
    select_pending_task,
)


def task(
    hash_char: str,
    *,
    refs: int,
    rank: int,
    chars: int,
    ready: bool = False,
) -> SignalTask:
    references = tuple(
        SignalReference(
            scope=f"scope:{index}",
            candidate_id=f"candidate:{index}",
            content_ids=("a",),
            rank=rank,
        )
        for index in range(refs)
    )

    return SignalTask(
        input_hash=hash_char * 64,
        content_ids=("a",),
        content_count=1,
        chars=chars,
        references=references,
        ready=ready,
    )


class SignalThesesWorkerTests(
    unittest.TestCase
):
    def test_select_prefers_reused_input(self):
        selected = select_pending_task([
            task(
                "a",
                refs=1,
                rank=0,
                chars=100,
            ),
            task(
                "b",
                refs=3,
                rank=10,
                chars=1000,
            ),
        ])

        self.assertEqual(
            selected.input_hash,
            "b" * 64,
        )

    def test_ready_task_is_not_selected(self):
        selected = select_pending_task([
            task(
                "a",
                refs=10,
                rank=0,
                chars=10,
                ready=True,
            ),
            task(
                "b",
                refs=1,
                rank=1,
                chars=20,
            ),
        ])

        self.assertEqual(
            selected.input_hash,
            "b" * 64,
        )

    def test_prompt_has_all_materials_without_scope(self):
        prompt = build_prompt([
            {
                "content_id": "a",
                "title": "Заголовок А",
                "text": "Текст А",
            },
            {
                "content_id": "b",
                "title": "Заголовок Б",
                "text": "Текст Б",
            },
        ])

        self.assertIn(
            "material_id=1",
            prompt,
        )
        self.assertIn(
            "material_id=2",
            prompt,
        )
        self.assertIn(
            "Текст А",
            prompt,
        )
        self.assertIn(
            "Текст Б",
            prompt,
        )
        self.assertNotIn(
            "candidate_id",
            prompt,
        )
        self.assertNotIn(
            "c1:object",
            prompt,
        )

    def test_response_fence_and_evidence_contract(self):
        raw = """```json
{
  "summary": "Коротке узагальнення.",
  "theses": [
    {
      "text": "Перша теза.",
      "evidence_material_ids": [1]
    },
    {
      "text": "Друга теза.",
      "evidence_material_ids": [2, 3]
    }
  ]
}
```"""

        result, fenced = (
            parse_model_response(
                raw,
                material_count=3,
            )
        )

        self.assertTrue(fenced)
        self.assertEqual(
            len(result["theses"]),
            2,
        )

    def test_four_evidence_ids_rejected(self):
        raw = """{
  "summary": "Коротке узагальнення.",
  "theses": [
    {
      "text": "Перша теза.",
      "evidence_material_ids": [1, 2, 3, 4]
    },
    {
      "text": "Друга теза.",
      "evidence_material_ids": [1]
    }
  ]
}"""

        with self.assertRaises(
            ValueError
        ):
            parse_model_response(
                raw,
                material_count=4,
            )


if __name__ == "__main__":
    unittest.main()

class SignalThesesFailureTests(unittest.TestCase):
    def test_deferred_task_does_not_block_queue(self):
        from reporting.signal_theses_worker import (
            select_pending_task,
        )

        first = task(
            "a",
            refs=3,
            rank=0,
            chars=10,
        )

        second = task(
            "b",
            refs=1,
            rank=1,
            chars=20,
        )

        selected = select_pending_task(
            [first, second],
            excluded_hashes={
                first.input_hash
            },
        )

        self.assertEqual(
            selected.input_hash,
            second.input_hash,
        )

    def test_failure_cooldown_and_clear(self):
        from datetime import (
            datetime,
            timedelta,
            timezone,
        )
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from reporting.signal_theses_worker import (
            clear_failure,
            deferred_hashes,
            record_failure,
        )

        now = datetime(
            2026,
            9,
            28,
            14,
            0,
            tzinfo=timezone.utc,
        )

        pending = task(
            "c",
            refs=1,
            rank=0,
            chars=10,
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            failure = record_failure(
                pending.input_hash,
                ValueError("bad output"),
                state_root=root,
                now=now,
            )

            self.assertEqual(
                failure["attempts"],
                1,
            )

            self.assertIn(
                pending.input_hash,
                deferred_hashes(
                    [pending],
                    state_root=root,
                    now=(
                        now
                        + timedelta(minutes=30)
                    ),
                ),
            )

            self.assertNotIn(
                pending.input_hash,
                deferred_hashes(
                    [pending],
                    state_root=root,
                    now=(
                        now
                        + timedelta(hours=2)
                    ),
                ),
            )

            clear_failure(
                pending.input_hash,
                state_root=root,
            )

            self.assertEqual(
                deferred_hashes(
                    [pending],
                    state_root=root,
                    now=now,
                ),
                set(),
            )
