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
        self.assertIn("Чи це той самий конкретний епізод або факт?", self.html)
        self.assertIn("Спільна тема, місто, організація чи об'єкт", self.html)

    def test_interface_is_ukrainian_and_self_explanatory(self):
        for token in (
            "Завантажити набір",
            "Продовжити з файлу",
            "Зберегти розмітку у файл",
            "Твердження A",
            "Фрагмент, з якого взято твердження",
            "Який зв'язок між твердженнями?",
            "Наскільки ви впевнені у своєму рішенні?",
        ):
            self.assertIn(token, self.html)
        for legacy in ("load dataset", "resume from export", "export annotations", "handbook (?)"):
            self.assertNotIn(legacy, self.html)

    def test_conflict_reason_is_multiselect(self):
        self.assertIn("conflict_types:[]", self.html)
        self.assertIn("toggleConflict(value)", self.html)
        self.assertIn("Можна вибрати кілька причин одночасно", self.html)
        self.assertIn('value==="not_obvious"', self.html)
        # Legacy scalar remains only as compatibility projection for analyzer v1.
        self.assertIn("draft.conflict_type = values[0] || null", self.html)

    def test_confidence_meaning_is_explained(self):
        self.assertIn("Це оцінка <b>вашої впевненості в розмітці</b>", self.html)
        self.assertIn("майже однозначно", self.html)
        self.assertIn("дуже неоднозначно", self.html)


    def test_annotation_view_hides_source_and_time(self):
        for token in (
            'id="a_src"',
            'id="b_src"',
            'A.source',
            'B.source',
            'first_seen',
        ):
            self.assertNotIn(token, self.html)

    def test_visible_save_button_commits_and_advances(self):
        self.assertIn('id="bsave"', self.html)
        self.assertIn('Зберегти й перейти далі', self.html)
        self.assertIn('$("bsave").onclick = ()=>commit(true);', self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
