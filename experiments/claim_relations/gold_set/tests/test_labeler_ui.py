#!/usr/bin/env python3
"""
UI-тести relation_gold_set_labeler.html у headless Chromium (Playwright).

Ці тести перевіряють САМЕ ті властивості, які є методологічними інваріантами,
а не косметику:

  1. Діагностика (scores/страта/hub/групи) НЕ присутня в DOM до збереження
     мітки. Це головне анти-leakage правило: якщо анотатор бачить score,
     він починає погоджуватись зі score.
  2. Referent gate працює в UI так само, як у validate_annotation:
     same_fact/same_event/contradiction недоступні при "референт = ні".
  3. Приховані повтори виглядають як звичайні пари (жодного маркера в DOM).
  4. Клавіатурний потік реально зберігає запис і рухає лічильник.
  5. Експорт дає валідний JSONL, який проходить контракт.

Якщо Playwright/Chromium недоступні — тести пропускаються (skip), а не падають.

Запуск:
    python3 experiments/claim_relations/gold_set/tests/test_labeler_ui.py
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


def _build_dataset(tmpdir: Path) -> Path:
    """Генерує невеликий реальний датасет семплером (через stubs)."""
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

    def test_dataset_loads_and_shows_first_pair(self):
        ctx, page = self._page()
        try:
            self.assertNotEqual(page.inner_text("#a_claim").strip(), "")
            self.assertNotEqual(page.inner_text("#b_claim").strip(), "")
            self.assertIn("/", page.inner_text("#prog"))
        finally:
            ctx.close()

    def test_no_diagnostics_in_dom_before_labeling(self):
        """ГОЛОВНИЙ анти-leakage тест: у видимому DOM не має бути ані score,
        ані назви страти, ані hub-прапорців, ані source-group."""
        ctx, page = self._page()
        try:
            body = page.inner_text("body")
            for token in ("claim_score", "content_score", "stratum",
                          "S1_", "S4_", "S7_", "hub", "first_seen"):
                self.assertNotIn(token, body, f"'{token}' visible before labeling")
            # також перевіряємо, що числових score немає у розмітці
            html = page.content()
            self.assertNotIn("claim_score", html.split("<script")[0])
        finally:
            ctx.close()

    def test_diagnostics_button_disabled_until_saved(self):
        ctx, page = self._page()
        try:
            self.assertTrue(page.is_disabled("#bdiag"))
            page.keyboard.press("1")   # referent = yes
            page.keyboard.press("w")   # same_event
            page.keyboard.press("a")   # confidence high -> autosave + advance
            page.wait_for_timeout(400)
            page.keyboard.press("ArrowLeft")  # повертаємось на збережену пару
            page.wait_for_timeout(200)
            self.assertFalse(page.is_disabled("#bdiag"))
        finally:
            ctx.close()

    def test_diagnostics_revealed_only_after_save(self):
        ctx, page = self._page()
        try:
            page.keyboard.press("1")
            page.keyboard.press("w")
            page.keyboard.press("a")
            page.wait_for_timeout(400)
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(200)
            page.keyboard.press("i")
            page.wait_for_timeout(150)
            diag = page.inner_text("#diag")
            self.assertIn("stratum", diag)
            self.assertIn("claim_score", diag)
        finally:
            ctx.close()

    def test_referent_gate_blocks_same_fact_when_referent_no(self):
        """Дзеркалить validate_annotation: при референті 'ні' сильні labels
        мають бути недоступні."""
        ctx, page = self._page()
        try:
            page.keyboard.press("2")   # referent = no
            page.wait_for_timeout(120)
            for key, value in (("q", "same_fact"), ("w", "same_event"), ("e", "contradiction")):
                page.keyboard.press(key)
                page.wait_for_timeout(80)
                on = page.eval_on_selector_all(
                    ".opt[data-g='rel'].on", "els => els.map(e => e.dataset.v)")
                self.assertNotIn(value, on, f"{value} must be blocked when referent=no")
            page.keyboard.press("t")   # unrelated дозволено
            page.wait_for_timeout(80)
            on = page.eval_on_selector_all(
                ".opt[data-g='rel'].on", "els => els.map(e => e.dataset.v)")
            self.assertEqual(on, ["unrelated"])
        finally:
            ctx.close()

    def test_negative_requires_conflict_type_before_save(self):
        ctx, page = self._page()
        try:
            before = page.inner_text("#prog")
            page.keyboard.press("2")   # no
            page.keyboard.press("t")   # unrelated
            page.keyboard.press("a")   # confidence -> має НЕ зберегти (немає conflict_type)
            page.wait_for_timeout(400)
            self.assertEqual(page.inner_text("#prog"), before,
                             "saved without conflict_type on a negative pair")
            page.keyboard.press("l")   # location
            page.keyboard.press("a")
            page.wait_for_timeout(400)
            self.assertNotEqual(page.inner_text("#prog"), before)
        finally:
            ctx.close()

    def test_hidden_repeats_are_indistinguishable(self):
        """У DOM не має бути жодної ознаки повтору."""
        repeat_positions = [i for i, r in enumerate(self.rows) if r["is_repeat"]]
        self.assertTrue(repeat_positions, "fixture has no repeats")
        ctx, page = self._page()
        try:
            target = repeat_positions[0]
            for _ in range(target):
                page.keyboard.press("ArrowRight")
            page.wait_for_timeout(250)
            body = page.inner_text("body")
            for token in ("repeat", "повтор", "is_repeat", "swapped"):
                self.assertNotIn(token.lower(), body.lower())
        finally:
            ctx.close()

    def test_export_produces_valid_contract_jsonl(self):
        ctx, page = self._page()
        try:
            # розмічаємо 3 пари різними шляхами
            page.keyboard.press("1"); page.keyboard.press("q"); page.keyboard.press("a")
            page.wait_for_timeout(350)
            page.keyboard.press("2"); page.keyboard.press("l")
            page.keyboard.press("t"); page.keyboard.press("s")
            page.wait_for_timeout(350)
            page.keyboard.press("3"); page.keyboard.press("r"); page.keyboard.press("d")
            page.wait_for_timeout(350)

            with page.expect_download() as dl:
                page.click("#bexport")
            path = dl.value.path()
            content = Path(path).read_text(encoding="utf-8")
            records = [json.loads(l) for l in content.splitlines() if l.strip()]
            self.assertEqual(len(records), 3)
            ids = {r["interaction_id"] for r in records}
            self.assertEqual(len(ids), 3)
            for rec in records:
                self.assertEqual(common.validate_annotation(rec), [],
                                 f"exported record violates contract: {rec}")
        finally:
            ctx.close()

    def test_progress_survives_page_reload(self):
        """Найдорожчий сценарій відмови: анотатор закрив вкладку після двох
        годин розмітки. Перевіряємо, що стан реально відновлюється."""
        ctx = self.browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(LABELER.resolve().as_uri())
            page.set_input_files("#fdata", str(self.dataset_path))
            page.wait_for_selector("#work", state="visible", timeout=8000)
            page.keyboard.press("1"); page.keyboard.press("w"); page.keyboard.press("a")
            page.wait_for_timeout(400)
            page.keyboard.press("1"); page.keyboard.press("r"); page.keyboard.press("s")
            page.wait_for_timeout(400)
            self.assertIn("розмічено 2", page.inner_text("#prog"))

            page.reload()
            page.set_input_files("#fdata", str(self.dataset_path))
            page.wait_for_selector("#work", state="visible", timeout=8000)
            page.wait_for_timeout(300)
            prog = page.inner_text("#prog")
            self.assertIn("розмічено 2", prog,
                          f"annotations lost after reload (prog={prog!r}); "
                          "labeler must survive a closed tab")
        finally:
            ctx.close()

    def test_resume_from_exported_file(self):
        """Другий рубіж оборони, якщо localStorage недоступний (file:// у
        деяких браузерах): відновлення з експортованого JSONL."""
        ctx = self.browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(LABELER.resolve().as_uri())
            page.set_input_files("#fdata", str(self.dataset_path))
            page.wait_for_selector("#work", state="visible", timeout=8000)
            page.evaluate("localStorage.clear()")
            page.keyboard.press("1"); page.keyboard.press("q"); page.keyboard.press("a")
            page.wait_for_timeout(400)
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
            page2.wait_for_timeout(400)
            self.assertIn("розмічено 1", page2.inner_text("#prog"))
            ctx2.close()
        finally:
            ctx.close()

    def test_progress_and_back_navigation(self):
        ctx, page = self._page()
        try:
            page.keyboard.press("1"); page.keyboard.press("r"); page.keyboard.press("a")
            page.wait_for_timeout(350)
            self.assertIn("розмічено 1", page.inner_text("#prog"))
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(200)
            on = page.eval_on_selector_all(
                ".opt[data-g='ref'].on", "els => els.map(e => e.dataset.v)")
            self.assertEqual(on, ["yes"], "previous answer not restored on back navigation")
        finally:
            ctx.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
