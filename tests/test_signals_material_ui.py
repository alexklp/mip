from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SignalsMaterialUIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            ROOT
            / "web"
            / "templates"
            / "signals.html"
        ).read_text(encoding="utf-8")

        cls.css = (
            ROOT
            / "web"
            / "static"
            / "app.css"
        ).read_text(encoding="utf-8")

    def test_material_rows_have_stable_ids(self):
        self.assertIn(
            'data-content-id="{{ evidence.content_id }}"',
            self.template,
        )
        self.assertIn(
            'data-occurrence-id="{{ evidence.occurrence_id }}"',
            self.template,
        )

    def test_material_actions_use_existing_write_api(self):
        self.assertIn(
            '"/materials/"',
            self.template,
        )
        self.assertIn(
            '"/membership"',
            self.template,
        )
        self.assertIn(
            "mutateMaterialContour",
            self.template,
        )
        self.assertIn(
            "mutateSignalMembership",
            self.template,
        )

    def test_material_report_item_is_not_annotation(self):
        self.assertIn(
            'type: "material"',
            self.template,
        )
        self.assertIn(
            '"material:" +',
            self.template,
        )
        self.assertIn(
            "analyst_assessment",
            self.template,
        )

    def test_excluded_materials_have_separate_collapsed_area(self):
        self.assertIn(
            "data-excluded-section",
            self.template,
        )
        self.assertIn(
            "Виключені аналітиком",
            self.template,
        )
        self.assertIn(
            ".signals-excluded",
            self.css,
        )


if __name__ == "__main__":
    unittest.main()
