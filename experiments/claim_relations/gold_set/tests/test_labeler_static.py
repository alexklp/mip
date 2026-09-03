from pathlib import Path
import unittest

HTML_PATH = Path(__file__).resolve().parents[1] / "relation_gold_set_labeler.html"


class TestLabelerStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML_PATH.read_text(encoding="utf-8")

    def test_dataset_identity_is_scoped(self):
        for token in (
            "datasetFingerprint",
            "DATASET_ID",
            "dataset_id",
            "pair_key",
            "STORAGE_PREFIX + DATASET_ID",
        ):
            self.assertIn(token, self.html)

    def test_no_diagnostics_ui_during_annotation(self):
        for token in (
            'id="bdiag"',
            "toggleDiag",
            "показати діагностику",
        ):
            self.assertNotIn(token, self.html)

    def test_confidence_does_not_autocommit(self):
        self.assertNotIn("setTimeout(()=>commit", self.html)
        self.assertIn('if(ev.key==="Enter"){ ev.preventDefault(); commit(true); return; }',
                      self.html)

    def test_export_filename_is_dataset_scoped(self):
        self.assertIn(
            'a.download = `gold_set_v1_annotations_${DATASET_ID}.jsonl`;',
            self.html,
        )

    def test_human_identity_contract_is_explicit(self):
        self.assertIn("один і той самий конкретний епізод / факт", self.html)
        self.assertIn("Спільний об'єкт, місто, організація", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
