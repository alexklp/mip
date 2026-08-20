"""
test_validator.py — unit tests контракту claim-extraction/1 на raw рівні
(validate_claim_extraction_response), БЕЗ звернення до LLM-сервера.
"""
import json
import unittest

from validator import validate_claim_extraction_response

EX_TEXT = (
    "Місцева влада повідомила про відключення світла у трьох районах. "
    "За словами очевидців, ситуація критична."
)


def make_response(**overrides):
    base = {
        "schema_version": "claim-extraction/1",
        "evidence_id": "EX",
        "claims": [
            {
                "claim_local_id": "C1",
                "claim_text": "Місцева влада повідомила про відключення світла у трьох районах",
                "evidence_span": "Місцева влада повідомила про відключення світла у трьох районах",
                "evidence_start": 0,
                "evidence_end": 63,
                "epistemic_status": "reported",
                "claim_time_text": None,
                "attribution_text": None,
            }
        ],
    }
    base.update(overrides)
    return base


class TestValidNormalizedOutput(unittest.TestCase):
    def test_valid_response_passes(self):
        resp = json.dumps(make_response(), ensure_ascii=False)
        r = validate_claim_extraction_response(resp, "EX", EX_TEXT)
        self.assertTrue(r.valid, r.errors)
        self.assertEqual(r.claim_count, 1)
        self.assertEqual(r.errors, [])


class TestNullStringRejected(unittest.TestCase):
    def test_null_string_in_claim_time_text_rejected(self):
        data = make_response()
        data["claims"][0]["claim_time_text"] = "null"
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)
        self.assertTrue(any("null" in e for e in r.errors))

    def test_null_string_in_attribution_text_rejected(self):
        data = make_response()
        data["claims"][0]["attribution_text"] = "null"
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)

    def test_real_json_null_still_allowed(self):
        r = validate_claim_extraction_response(json.dumps(make_response(), ensure_ascii=False), "EX", EX_TEXT)
        self.assertTrue(r.valid, r.errors)


class TestUnknownFieldRejected(unittest.TestCase):
    def test_unknown_top_level_field_rejected(self):
        data = make_response()
        data["relations"] = []
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)

    def test_unknown_claim_field_rejected(self):
        data = make_response()
        data["claims"][0]["confidence"] = 0.9
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)


class TestForbiddenTopLevelKeys(unittest.TestCase):
    def test_relations_forbidden(self):
        data = make_response()
        data["relations"] = [{"from": "C1", "to": "C1", "type": "supports"}]
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)

    def test_event_forbidden(self):
        data = make_response()
        data["event"] = {"type": "shelling"}
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)

    def test_summary_forbidden(self):
        data = make_response()
        data["summary"] = "коротко про все"
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)


class TestMalformedFencedRaw(unittest.TestCase):
    """validator.py — raw рівень: НЕ знімає markdown-огорожу сам (це робота
    adapter-шару run_eval.strip_markdown_fence, перевіряється окремо в
    test_run_eval.py)."""

    def test_fenced_json_rejected_at_raw_level(self):
        raw = "```json\n" + json.dumps(make_response(), ensure_ascii=False) + "\n```"
        r = validate_claim_extraction_response(raw, "EX", EX_TEXT)
        self.assertFalse(r.valid)

    def test_empty_string_rejected(self):
        r = validate_claim_extraction_response("", "EX", EX_TEXT)
        self.assertFalse(r.valid)

    def test_none_rejected(self):
        r = validate_claim_extraction_response(None, "EX", EX_TEXT)
        self.assertFalse(r.valid)


class TestWrongEvidenceId(unittest.TestCase):
    def test_evidence_id_mismatch_rejected(self):
        resp = json.dumps(make_response(), ensure_ascii=False)
        r = validate_claim_extraction_response(resp, "OTHER_ID", EX_TEXT)
        self.assertFalse(r.valid)
        self.assertTrue(any("evidence_id" in e for e in r.errors))


class TestSpanMissing(unittest.TestCase):
    def test_missing_evidence_span_key_rejected(self):
        data = make_response()
        del data["claims"][0]["evidence_span"]
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)

    def test_empty_evidence_span_rejected(self):
        data = make_response()
        data["claims"][0]["evidence_span"] = ""
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)

    def test_span_not_found_in_text_rejected(self):
        data = make_response()
        data["claims"][0]["evidence_span"] = "цього тексту тут немає"
        data["claims"][0]["evidence_start"] = 0
        data["claims"][0]["evidence_end"] = 23
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)


class TestDuplicateClaimLocalId(unittest.TestCase):
    def test_duplicate_claim_local_id_rejected(self):
        data = make_response()
        second = dict(data["claims"][0])
        second["evidence_span"] = "ситуація критична"
        second["evidence_start"] = 87
        second["evidence_end"] = 104
        data["claims"].append(second)
        r = validate_claim_extraction_response(json.dumps(data, ensure_ascii=False), "EX", EX_TEXT)
        self.assertFalse(r.valid)
        self.assertTrue(any("дублюється" in e for e in r.errors))


if __name__ == "__main__":
    unittest.main()
