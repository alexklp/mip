from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class C1UIContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            ROOT
            / "web"
            / "templates"
            / "contours.html"
        ).read_text(encoding="utf-8")

        cls.css = (
            ROOT
            / "web"
            / "static"
            / "app.css"
        ).read_text(encoding="utf-8")

        cls.runner = (
            ROOT
            / "ops"
            / "run_topics_snapshot_once.sh"
        ).read_text(encoding="utf-8")

        cls.topics_builder = (
            ROOT
            / "reporting"
            / "contour_topics_c1.py"
        ).read_text(encoding="utf-8")

    def test_signals_workspace_contract(self):
        template = self.template

        signals_if = template.index(
            '{% if analysis_layer == "signals"'
        )

        workspace = template.index(
            'id="c1-signals-workspace"',
            signals_if,
        )

        script = template.index(
            "<script>",
            workspace,
        )

        self.assertLess(
            signals_if,
            workspace,
        )
        self.assertLess(
            workspace,
            script,
        )

        self.assertIn(
            'id="c1-signals-work-state"',
            template,
        )
        self.assertIn(
            "data-c1-signal-item",
            template,
        )
        self.assertIn(
            "data-c1-signal-actions",
            template,
        )

        self.assertNotIn(
            "snapshot.selection",
            template,
        )
        self.assertNotIn(
            'id="c1-signals-activity"',
            template,
        )

    def test_signals_filter_render_contract(self):
        self.assertIn(
            "#c1-signals-workspace [hidden]",
            self.css,
        )

        self.assertIn(
            "if (!visible.length)",
            self.template,
        )

        self.assertIn(
            "active = visible[0]",
            self.template,
        )

    def test_signals_day_drilldown_is_disabled(self):
        self.assertIn(
            'analysis_layer != "signals"',
            self.template,
        )
        self.assertIn(
            'aria-disabled="true"',
            self.template,
        )

    def test_c1_topics_refreshes_all_active_objects(self):
        self.assertIn(
            "reporting/contour_topics_c1.py",
            self.runner,
        )
        self.assertIn(
            "--all-objects",
            self.runner,
        )

        self.assertIn(
            "FROM contour_reference_objects",
            self.topics_builder,
        )
        self.assertIn(
            "AND active",
            self.topics_builder,
        )

        self.assertIn(
            "if total and coverage_pct <",
            self.topics_builder,
        )

    def test_populations_are_labeled_separately(self):
        self.assertIn(
            "матеріалів для тем",
            self.template,
        )
        self.assertIn(
            "матеріалів для сигналів",
            self.template,
        )


if __name__ == "__main__":
    unittest.main()
