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

        self.assertNotIn(
            'id="c1-signals-work-state"',
            template,
        )
        self.assertIn(
            "data-c1-signal-item",
            template,
        )
        self.assertIn(
            "signals-actions-export-only",
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

    def test_signals_day_cross_filter_is_enabled(self):
        self.assertNotIn(
            '{% if analysis_layer == "signals" and not selected_day %}',
            self.template,
        )
        self.assertNotIn(
            'aria-disabled="true"',
            self.template,
        )
        self.assertIn(
            'analysis={{ analysis_layer }}',
            self.template,
        )
        self.assertIn(
            'contour_signals.view_candidates',
            self.template,
        )
        self.assertIn(
            'day={{ row.day.isoformat() }}',
            self.template,
        )
        self.assertIn(
            'id="c1-signals-visible-count"',
            self.template,
        )
        self.assertIn(
            "visibleCount.textContent",
            self.template,
        )

    def test_object_selector_has_30d_and_24h_scales(self):
        self.assertIn(
            "contour-rank-metric is-30d",
            self.template,
        )
        self.assertIn(
            "contour-rank-metric is-24h",
            self.template,
        )
        self.assertIn(
            "row.contents_30d",
            self.template,
        )
        self.assertIn(
            "row.contents_24h",
            self.template,
        )
        self.assertIn(
            ".contour-rank-metric.is-24h",
            self.css,
        )
        self.assertNotIn(
            "contour-quiet-list",
            self.template,
        )
        self.assertIn(
            "dashboard_active_24h_count",
            self.template,
        )

    def test_filtered_topics_keeps_full_workspace(self):
        self.assertNotIn(
            'and not selected_day',
            self.template,
        )
        self.assertNotIn(
            "contour-analysis-toolbar-filtered",
            self.template,
        )
        self.assertIn(
            'Теми за {{ selected_day.strftime("%d.%m.%Y") }}',
            self.template,
        )
        self.assertIn(
            'Теми за {{ selected_day.strftime("%d.%m.%Y") }}',
            self.template,
        )
        self.assertIn(
            'Публікації про вибраний об\'єкт за',
            self.template,
        )
        self.assertIn(
            "c1-topics-cloud",
            self.template,
        )
        self.assertIn(
            "c1-topics-ranking",
            self.template,
        )
        self.assertIn(
            "analysis=signals",
            self.template,
        )
        self.assertIn(
            ".contour-trend-column:hover",
            self.css,
        )
        self.assertIn(
            "background: transparent;",
            self.css,
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
