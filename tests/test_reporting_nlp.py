import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTING = ROOT / "reporting"

sys.path.insert(
    0,
    str(REPORTING),
)

from nlp_prepare import clean_text, load_rules


class ReportingNoiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_platform_footer_is_removed(self):
        text = (
            "Вночі ворог атакував Київ. "
            "Сайт | Facebook | YouTube | TikTok"
        )

        cleaned = clean_text(
            text,
            ["ТСН"],
            self.rules,
        )

        self.assertEqual(
            cleaned,
            "Вночі ворог атакував Київ.",
        )

    def test_telegram_promo_footer_is_removed(self):
        text = (
            'Троє загиблих. '
            '"Телеграф" – всюди! '
            "Обирайте улюблений формат: "
            "Telegram | Сайт | Facebook | "
            "YouTube | Viber"
        )

        cleaned = clean_text(
            text,
            ["Телеграф"],
            self.rules,
        )

        self.assertNotIn(
            "facebook",
            cleaned.lower(),
        )
        self.assertNotIn(
            "youtube",
            cleaned.lower(),
        )
        self.assertIn(
            "Троє загиблих",
            cleaned,
        )

    def test_real_platform_reference_is_preserved(self):
        text = (
            "Facebook повідомив про нові "
            "правила для YouTube."
        )

        cleaned = clean_text(
            text,
            ["Інше джерело"],
            self.rules,
        )

        self.assertIn(
            "Facebook",
            cleaned,
        )
        self.assertIn(
            "YouTube",
            cleaned,
        )


if __name__ == "__main__":
    unittest.main()
