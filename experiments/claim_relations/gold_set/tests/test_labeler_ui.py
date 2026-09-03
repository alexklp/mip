#!/usr/bin/env python3
"""
UI-тести relation_gold_set_labeler.html у headless Chromium (Playwright).

Перевіряють методологічні інваріанти, а не косметику:

  1. Scores/stratum/hub diagnostics не показуються анотатору взагалі під час
     сесії, тому наступні рішення лишаються blind.
  2. Referent gate відповідає validate_annotation.
  3. Приховані повтори не мають маркера в UI.
  4. Вибір confidence НЕ автозберігає: потрібен явний Enter.
  5. Експорт містить dataset_id + pair_key і проходить контракт.
  6. localStorage та resume працюють лише в межах конкретного dataset.

Якщо Playwright/Chromium недоступні — тести пропускаються, а не падають.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[3]
STUBS = HERE / "stubs"
for p in (str(STUBS), str(PKG_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

LABELER = HERE.parent / "relation_gold_set_labeler.html"

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:  # pragma: no cover
    HAVE_PW = False

from experiments.claim_relations.gold_set import gold_set_common as common  # noqa: E402
from experiments.claim_relations.gold_set import relation_gold_set_analyze as analyze  # noqa: E402


def _build_dataset(tmpdir: Path) -> Path:
    from experiments.claim_relations.gold_set import relation_gold_set_sampler as sampler
    out = tmpdir / "ds"
    rc = sampler.main([
        "--out-dir", str(out), "--seed", "31337", "--chunk-size", "11",
        "--hub-degree-threshold", "5", "--repeat-rate", "0.2",
        "--min-repeat-separation", "5",
        "--n-s1", "5", "--n-s2", "5", "--n-s3", "5", "--n-s4", "5",
        "--n-s5", "3", "--n-s6", "3", "--n-s7", "3", "--n-s8", "3", "--n-s9", "3",
    ])
    assert rc == 0
    return out / "gold_set_v1_dataset.jsonl"


@unittest.skipUnless(HAVE_PW, "playwright not installed")
@unittest.skipUnless(LABELER.exists(), "labeler html missing")
class TestLabelerUI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmpdir = Path(cls.tmp.name)
        cls.dataset_path = _build_dataset(tmpdir)
        cls.rows = [json.loads(l) for l in
                    cls.dataset_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        cls.dataset_id = analyze.dataset_fingerprint(cls.rows)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(
            executable_path="/opt/pw-browsers/chromium/chrome-linux/chrome"
            if Path("/opt/pw-browsers/chromium/chrome-linux/chrome").exists() else None,
            args=["--allow-file-access-from-files", "--no-sandbox"],
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.tmp.cleanup()

    def _page(self):
        ctx = self.browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.goto(LABELER.resolve().as_uri())
        page.set_input_files("#fdata", str(self.dataset_path))
        page.wait_for_selector("#work", state="visible", timeout=8000)
        return ctx, page

    @staticmethod
    def _save_positive(page, relation="same_event", confidence="high"):
        key_rel = {"same_fact": "q", "same_event": "w", "related": "r"}[relation]
        key_cnf = {"high": "a", "medium": "s", "low": "d"}[confidence]
        page.keyboard.press("1")
        page.keyboard.press(key_rel)
        page.keyboard.press(key_cnf)
        page.keyboard.press("Enter")
        page.wait_for_timeout(120)

    def test_dataset_loads_and_shows_first_pair(self):
        ctx, page = self._page()
        try:
            self.assertNotEqual(page.inner_text("#a_claim").strip(), "")
            self.assertNotEqual(page.inner_text("#b_claim").strip(), "")
            self.assertIn("/", page.inner_text("#prog"))
        finally:
            ctx.close()

    def test_strict_blindness_no_diagnostics_control_or_visible_scores(self):
        """Diagnostics не можна відкрити навіть після збереження попередніх пар."""
        ctx, page = self._page()
        try:
            body = page.inner_text("body")
            for token in ("claim_score", "content_score", "stratum", "S1_", "S4_",
                          "S7_", "hub_a", "hub_b", "first_seen"):
                self.assertNotIn(token, body, f"'{token}' visible in annotation UI")
            self.assertIsNone(page.query_selector("#bdiag"))
            self._save_positive(page)
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(100)
            body = page.inner_text("body")
            self.assertNotIn("claim_score", body)
            self.assertNotIn("content_score", body)
            self.assertIsNone(page.query_selector("#bdiag"))
        finally:
            ctx.close()

    def test_confidence_does_not_autosave(self):
        ctx, page = self._page()
        try:
            before = page.inner_text("#prog")
            page.keyboard.press("1")
            page.keyboard.press("w")
            page.keyboard.press("a")
            page.wait_for_timeout(350)
            self.assertEqual(page.inner_text("#prog"), before,
                             "confidence selection must not auto-save/advance")
            page.keyboard.press("Enter")
            page.wait_for_timeout(120)
            self.assertNotEqual(page.inner_text("#prog"), before)
        finally:
            ctx.close()

    def test_referent_gate_blocks_strong_labels_when_referent_no(self):
        ctx, page = self._page()
        try:
            page.keyboard.press("2")
            for key, value in (("q", "same_fact"), ("w", "same_event"), ("e", "contradiction")):
                page.keyboard.press(key)
                page.wait_for_timeout(50)
                on = page.eval_on_selector_all(
                    ".opt[data-g='rel'].on", "els => els.map(e => e.dataset.v)")
                self.assertNotIn(value, on)
            page.keyboard.press("t")
            on = page.eval_on_selector_all(
                ".opt[data-g='rel'].on", "els => els.map(e => e.dataset.v)")
            self.assertEqual(on, ["unrelated"])
        finally:
            ctx.close()

    def test_negative_requires_conflict_type_and_enter(self):
        ctx, page = self._page()
        try:
            before = page.inner_text("#prog")
            page.keyboard.press("2")
            page.keyboard.press("t")
            page.keyboard.press("a")
            page.keyboard.press("Enter")
            page.wait_for_timeout(120)
            self.assertEqual(page.inner_text("#prog"), before)
            page.keyboard.press("l")
            page.keyboard.press("Enter")
            page.wait_for_timeout(120)
            self.assertNotEqual(page.inner_text("#prog"), before)
        finally:
            ctx.close()

    def test_hidden_repeats_are_indistinguishable(self):
        repeat_positions = [i for i, r in enumerate(self.rows) if r["is_repeat"]]
        self.assertTrue(repeat_positions, "fixture has no repeats")
        ctx, page = self._page()
        try:
            for _ in range(repeat_positions[0]):
                page.keyboard.press("ArrowRight")
            page.wait_for_timeout(100)
            body = page.inner_text("body").lower()
            for token in ("repeat", "повтор", "is_repeat", "swapped"):
                self.assertNotIn(token, body)
        finally:
            ctx.close()

    def test_export_contains_dataset_identity_and_valid_contract(self):
        ctx, page = self._page()
        try:
            self._save_positive(page, "same_fact", "high")

            page.keyboard.press("2"); page.keyboard.press("l")
            page.keyboard.press("t"); page.keyboard.press("s"); page.keyboard.press("Enter")
            page.wait_for_timeout(100)

            page.keyboard.press("3"); page.keyboard.press("r")
            page.keyboard.press("d"); page.keyboard.press("Enter")
            page.wait_for_timeout(100)

            with page.expect_download() as dl:
                page.click("#bexport")
            content = Path(dl.value.path()).read_text(encoding="utf-8")
            records = [json.loads(l) for l in content.splitlines() if l.strip()]
            self.assertEqual(len(records), 3)
            by_iid = {r["interaction_id"]: r for r in self.rows}
            for rec in records:
                self.assertEqual(rec["dataset_id"], self.dataset_id)
                self.assertEqual(rec["pair_key"], by_iid[rec["interaction_id"]]["pair_key"])
                self.assertEqual(common.validate_annotation(rec), [])
        finally:
            ctx.close()

    def test_progress_survives_page_reload_for_same_dataset(self):
        ctx = self.browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(LABELER.resolve().as_uri())
            page.set_input_files("#fdata", str(self.dataset_path))
            page.wait_for_selector("#work", state="visible", timeout=8000)
            self._save_positive(page)
            self._save_positive(page, "related", "medium")
            self.assertIn("розмічено 2", page.inner_text("#prog"))

            page.reload()
            page.set_input_files("#fdata", str(self.dataset_path))
            page.wait_for_selector("#work", state="visible", timeout=8000)
            page.wait_for_timeout(150)
            self.assertIn("розмічено 2", page.inner_text("#prog"))
        finally:
            ctx.close()

    def test_resume_from_exported_file(self):
        ctx = self.browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(LABELER.resolve().as_uri())
            page.set_input_files("#fdata", str(self.dataset_path))
            page.wait_for_selector("#work", state="visible", timeout=8000)
            page.evaluate("localStorage.clear()")
            self._save_positive(page, "same_fact", "high")
            with page.expect_download() as dl:
                page.click("#bexport")
            saved = Path(self.tmp.name) / "resume.jsonl"
            dl.value.save_as(str(saved))

            ctx2 = self.browser.new_context()
            page2 = ctx2.new_page()
            page2.goto(LABELER.resolve().as_uri())
            page2.evaluate("localStorage.clear()")
            page2.set_input_files("#fdata", str(self.dataset_path))
            page2.wait_for_selector("#work", state="visible", timeout=8000)
            page2.set_input_files("#fann", str(saved))
            page2.wait_for_timeout(150)
            self.assertIn("розмічено 1", page2.inner_text("#prog"))
            ctx2.close()
        finally:
            ctx.close()

    def test_progress_and_back_navigation(self):
        ctx, page = self._page()
        try:
            self._save_positive(page, "related", "high")
            self.assertIn("розмічено 1", page.inner_text("#prog"))
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(100)
            on = page.eval_on_selector_all(
                ".opt[data-g='ref'].on", "els => els.map(e => e.dataset.v)")
            self.assertEqual(on, ["yes"])
        finally:
            ctx.close()

    def test_export_filename_is_dataset_scoped(self):
        ctx, page = self._page()
        try:
            self._save_positive(page)
            with page.expect_download() as dl:
                page.click("#bexport")
            self.assertIn(self.dataset_id, dl.value.suggested_filename)
        finally:
            ctx.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
