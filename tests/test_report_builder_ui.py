from pathlib import Path
import unittest

from web.app import app, templates


ROOT = Path(__file__).resolve().parents[1]


class ReportBuilderUIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            ROOT
            / "web"
            / "templates"
            / "report.html"
        ).read_text(encoding="utf-8")

        cls.base = (
            ROOT
            / "web"
            / "templates"
            / "base.html"
        ).read_text(encoding="utf-8")

        cls.css = (
            ROOT
            / "web"
            / "static"
            / "app.css"
        ).read_text(encoding="utf-8")

    def test_report_route_exists(self):
        routes = {
            route.path:
                getattr(route, "methods", set())
            for route in app.routes
        }

        self.assertIn(
            "/report",
            routes,
        )
        self.assertIn(
            "GET",
            routes["/report"],
        )

    def test_report_uses_existing_local_draft(self):
        self.assertIn(
            "mip-report-draft-v1",
            self.template,
        )
        self.assertIn(
            "mip-report-draft/1",
            self.template,
        )

    def test_fact_and_assessment_are_separate(self):
        self.assertIn(
            "Фактичний виклад",
            self.template,
        )
        self.assertIn(
            "Оцінка аналітика",
            self.template,
        )

    def test_report_has_four_default_sections(self):
        for title in (
            "Міжнародний та загальнодержавний контекст",
            "Інформація про діяльність ЗСУ та ДШВ",
            "Негативна інформація, пов’язана із ЗСУ та ДШВ",
            "Інформація з ворожого інформаційного простору",
        ):
            self.assertIn(
                title,
                self.template,
            )

    def test_sidebar_has_report_counter(self):
        self.assertIn(
            'href="/report"',
            self.base,
        )
        self.assertIn(
            "data-report-count",
            self.base,
        )

    def test_report_styles_exist(self):
        self.assertIn(
            ".report-section",
            self.css,
        )
        self.assertIn(
            ".report-item",
            self.css,
        )


if __name__ == "__main__":
    unittest.main()


class ReportExportUIContractTests(
    unittest.TestCase
):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            ROOT
            / "web"
            / "templates"
            / "report.html"
        ).read_text(encoding="utf-8")

    def test_word_export_is_posted_from_local_draft(self):
        self.assertIn(
            'data-report-export',
            self.template,
        )
        self.assertIn(
            '"/report/export.docx"',
            self.template,
        )
        self.assertIn(
            'method: "POST"',
            self.template,
        )
        self.assertIn(
            "JSON.stringify",
            self.template,
        )

    def test_export_requires_all_items_assigned(self):
        self.assertIn(
            "unassigned === 0",
            self.template,
        )
        self.assertIn(
            "Спочатку розподіли всі елементи",
            self.template,
        )



class ReportSummaryOverrideUIContractTests(
    unittest.TestCase
):
    def test_signal_fact_edits_use_override(self):
        from pathlib import Path

        template = Path(
            "web/templates/report.html"
        ).read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "factual_summary_override",
            template,
        )

        self.assertIn(
            'current.items[index].type ===',
            template,
        )
