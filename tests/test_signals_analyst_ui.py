from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SignalsAnalystUIContractTests(unittest.TestCase):
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

    def test_signal_contour_picker_uses_persisted_api(self):
        self.assertIn(
            "data-signal-contour-picker",
            self.template,
        )
        self.assertIn(
            '"/annotations"',
            self.template,
        )
        self.assertIn(
            'method: "POST"',
            self.template,
        )
        self.assertIn(
            'method: "DELETE"',
            self.template,
        )

    def test_report_signal_keeps_stable_membership(self):
        self.assertIn(
            "data-report-content-ids",
            self.template,
        )
        self.assertIn(
            "content_ids:",
            self.template,
        )
        self.assertIn(
            "source_groups:",
            self.template,
        )

    def test_report_cart_is_local_and_not_annotation(self):
        self.assertIn(
            'mip-report-draft-v1',
            self.template,
        )
        self.assertIn(
            'type: "signal"',
            self.template,
        )
        self.assertIn(
            "data-report-signal",
            self.template,
        )
        self.assertNotIn(
            "report_annotation",
            self.template,
        )

    def test_contour_picker_has_operational_styles(self):
        self.assertIn(
            ".signals-contour-chip",
            self.css,
        )
        self.assertIn(
            ".signals-contour-menu",
            self.css,
        )
        self.assertIn(
            '.signals-contour-option[aria-pressed="true"]',
            self.css,
        )


if __name__ == "__main__":
    unittest.main()
