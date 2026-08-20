"""
test_run_eval.py — unit tests adapter-шару (fence-stripping, offset
resolution, ізоляція HTTP-помилки одного evidence) БЕЗ звернення до живого
LLM-сервера. call_model мокається — жодного реального мережевого виклику.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run_eval
from run_eval import strip_markdown_fence, resolve_offsets, process_response, main


class TestStripMarkdownFence(unittest.TestCase):
    def test_full_fence_json_stripped(self):
        raw = '```json\n{"a": 1}\n```'
        content, stripped = strip_markdown_fence(raw)
        self.assertTrue(stripped)
        self.assertEqual(content, '{"a": 1}')

    def test_full_fence_no_lang_tag_stripped(self):
        raw = '```\n{"a": 1}\n```'
        content, stripped = strip_markdown_fence(raw)
        self.assertTrue(stripped)
        self.assertEqual(content, '{"a": 1}')

    def test_no_fence_untouched(self):
        raw = '{"a": 1}'
        content, stripped = strip_markdown_fence(raw)
        self.assertFalse(stripped)
        self.assertEqual(content, raw)

    def test_partial_fence_not_touched(self):
        """Огорожа не покриває ВЕСЬ рядок (є текст до/після) — НЕ чіпаємо,
        це вже інша проблема, ховати її не можна."""
        raw = 'ось відповідь:\n```json\n{"a": 1}\n```\nдякую'
        content, stripped = strip_markdown_fence(raw)
        self.assertFalse(stripped)
        self.assertEqual(content, raw)


class TestResolveOffsets(unittest.TestCase):
    def setUp(self):
        self.text = "Перше речення тут. Друге речення тут. Перше речення тут знову."

    def test_unique_span_resolved(self):
        parsed = {"claims": [{"evidence_span": "Друге речення тут.", "evidence_start": 999, "evidence_end": 999}]}
        parsed, stats = resolve_offsets(parsed, self.text)
        self.assertEqual(stats, {"resolved": 1, "not_found": 0, "ambiguous": 0})
        c = parsed["claims"][0]
        self.assertEqual(self.text[c["evidence_start"]:c["evidence_end"]], "Друге речення тут.")

    def test_span_not_found(self):
        parsed = {"claims": [{"evidence_span": "цього тут немає", "evidence_start": 0, "evidence_end": 0}]}
        parsed, stats = resolve_offsets(parsed, self.text)
        self.assertEqual(stats["not_found"], 1)
        self.assertEqual(stats["resolved"], 0)

    def test_ambiguous_span_not_silently_resolved(self):
        """'Перше речення тут' зустрічається двічі — має бути ambiguous і
        НЕ підмінено мовчки офсетом першого входження (це та сама зміна
        методології, яку явно попросили в ТЗ)."""
        parsed = {"claims": [{"evidence_span": "Перше речення тут", "evidence_start": -1, "evidence_end": -1}]}
        parsed, stats = resolve_offsets(parsed, self.text)
        self.assertEqual(stats["ambiguous"], 1)
        self.assertEqual(stats["resolved"], 0)
        c = parsed["claims"][0]
        self.assertEqual((c["evidence_start"], c["evidence_end"]), (-1, -1))


class TestProcessResponseIntegration(unittest.TestCase):
    """fence-strip + offset-resolve + validate разом, без моделі —
    raw_response тут просто рядок, який ми самі конструюємо."""

    def setUp(self):
        self.text = "Ситуація критична. Місцева влада це підтвердила."

    def _valid_payload(self):
        return {
            "schema_version": "claim-extraction/1",
            "evidence_id": "EX",
            "claims": [{
                "claim_local_id": "C1",
                "claim_text": "Ситуація критична",
                "evidence_span": "Ситуація критична",
                "evidence_start": 999,
                "evidence_end": 999,
                "epistemic_status": "asserted",
                "claim_time_text": None,
                "attribution_text": None,
            }],
        }

    def test_fenced_response_with_wrong_offsets_still_validates(self):
        raw = "```json\n" + json.dumps(self._valid_payload(), ensure_ascii=False) + "\n```"
        validation, fence_stripped, offset_stats = process_response(raw, "EX", self.text)
        self.assertTrue(fence_stripped)
        self.assertEqual(offset_stats["resolved"], 1)
        self.assertTrue(validation.valid, validation.errors)

    def test_ambiguous_span_fails_validation(self):
        text = "Раз два три. Раз два три знову."
        payload = self._valid_payload()
        payload["claims"][0]["evidence_span"] = "Раз два три"
        raw = json.dumps(payload, ensure_ascii=False)
        validation, fence_stripped, offset_stats = process_response(raw, "EX", text)
        self.assertEqual(offset_stats["ambiguous"], 1)
        self.assertFalse(validation.valid)


class TestHttpFailureIsolatesPerEvidence(unittest.TestCase):
    """HTTP-помилка на ОДНОМУ evidence не повинна знищувати підсумковий
    звіт — інший evidence обробляється як зазвичай. call_model мокається."""

    def test_one_transport_error_does_not_kill_report(self):
        fixture = [
            {"evidence_id": "E1", "source": "src1", "text": "Текст перший."},
            {"evidence_id": "E2", "source": "src2", "text": "Текст другий."},
        ]
        good_payload = {
            "schema_version": "claim-extraction/1",
            "evidence_id": "E2",
            "claims": [{
                "claim_local_id": "C1",
                "claim_text": "Текст другий",
                "evidence_span": "Текст другий",
                "evidence_start": 0,
                "evidence_end": 12,
                "epistemic_status": "asserted",
                "claim_time_text": None,
                "attribution_text": None,
            }],
        }

        def fake_call_model(endpoint, model, prompt, timeout, max_tokens):
            if "перший" in prompt:
                raise TimeoutError("simulated transport failure")
            return json.dumps(good_payload, ensure_ascii=False), 1.23, "stop", 42

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            (tmp / "prompt.txt").write_text("evidence_id=<<EVIDENCE_ID>> text=<<EVIDENCE_TEXT>>", encoding="utf-8")
            out_path = tmp / "report.json"

            argv = [
                "run_eval.py",
                "--fixture", str(tmp / "fixture.json"),
                "--prompt", str(tmp / "prompt.txt"),
                "--out", str(out_path),
            ]
            with mock.patch.object(run_eval, "call_model", side_effect=fake_call_model), \
                 mock.patch("sys.argv", argv):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            report = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["n_total"], 2)
            by_id = {r["evidence_id"]: r for r in report["results"]}
            self.assertIn("transport_error", by_id["E1"])
            self.assertNotIn("transport_error", by_id["E2"])
            self.assertTrue(by_id["E2"]["valid"], by_id["E2"].get("errors"))


if __name__ == "__main__":
    unittest.main()
