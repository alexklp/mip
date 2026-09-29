from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import unittest
from xml.etree import ElementTree
from zipfile import ZipFile

from web.signal_export import build_signal_xlsx


MAIN_NS = (
    "http://schemas.openxmlformats.org/"
    "spreadsheetml/2006/main"
)


def fixture_candidate(
    *,
    with_theses: bool,
) -> dict:
    candidate = {
        "representative_title":
            "Тестовий сигнал",
        "occurrence_count": 1,
        "source_count": 1,
        "source_groups": [
            "ru_space",
        ],
    }

    if with_theses:
        candidate["ai_theses"] = {
            "summary":
                "Коротке узагальнення сигналу.",
            "theses": [
                {
                    "text":
                        "Перша основна теза.",
                    "evidence_material_ids":
                        [1],
                },
                {
                    "text":
                        "Друга основна теза.",
                    "evidence_material_ids":
                        [1],
                },
            ],
            "material_count": 1,
            "generated_at":
                "2026-09-29T12:00:00+00:00",
        }
    else:
        candidate["ai_theses"] = None

    return candidate


def fixture_publications() -> list[dict]:
    observed_at = datetime(
        2026,
        9,
        29,
        7,
        21,
        tzinfo=timezone.utc,
    )

    return [
        {
            "occurrence_id": "o1",
            "content_id": "c1",
            "source_id": "s1",
            "source_name": "Взгляд",
            "source_type": "rss",
            "source_group": "ru_space",
            "title": "Матеріал",
            "text": "Повний текст матеріалу.",
            "url": None,
            "published_at": observed_at,
            "collected_at": observed_at,
        }
    ]


def workbook_cells(
    payload: bytes,
) -> dict[str, str]:
    with ZipFile(BytesIO(payload)) as archive:
        shared_xml = archive.read(
            "xl/sharedStrings.xml"
        )
        sheet_xml = archive.read(
            "xl/worksheets/sheet1.xml"
        )

    shared_root = ElementTree.fromstring(
        shared_xml
    )

    strings = []

    for item in shared_root.findall(
        f"{{{MAIN_NS}}}si"
    ):
        strings.append(
            "".join(
                node.text or ""
                for node in item.iter(
                    f"{{{MAIN_NS}}}t"
                )
            )
        )

    sheet_root = ElementTree.fromstring(
        sheet_xml
    )

    cells = {}

    for cell in sheet_root.iter(
        f"{{{MAIN_NS}}}c"
    ):
        if cell.get("t") != "s":
            continue

        value = cell.find(
            f"{{{MAIN_NS}}}v"
        )

        if value is None or value.text is None:
            continue

        cells[cell.get("r")] = strings[
            int(value.text)
        ]

    return cells


class SignalExportTests(unittest.TestCase):
    def build(
        self,
        *,
        with_theses: bool,
    ) -> dict[str, str]:
        payload = build_signal_xlsx(
            snapshot={
                "as_of":
                    "2026-09-29T14:42:28+00:00",
            },
            candidate=fixture_candidate(
                with_theses=with_theses,
            ),
            publications=fixture_publications(),
            scope_label="C1 · Об'єкти ДШВ",
        )

        self.assertTrue(
            payload.startswith(b"PK")
        )

        return workbook_cells(payload)

    def test_ready_theses_are_added_to_summary(self):
        cells = self.build(
            with_theses=True
        )

        self.assertEqual(
            cells["A20"],
            "Тези",
        )
        self.assertEqual(
            cells["A21"],
            "Коротко",
        )
        self.assertEqual(
            cells["C21"],
            "Коротке узагальнення сигналу.",
        )
        self.assertEqual(
            cells["A24"],
            "Основні тези",
        )
        self.assertEqual(
            cells["C24"],
            "1. Перша основна теза.",
        )
        self.assertEqual(
            cells["C25"],
            "2. Друга основна теза.",
        )
        self.assertEqual(
            cells["A27"],
            "Динаміка поширення",
        )

    def test_missing_theses_keeps_existing_layout(self):
        cells = self.build(
            with_theses=False
        )

        self.assertNotIn(
            "Тези",
            cells.values(),
        )
        self.assertEqual(
            cells["A20"],
            "Динаміка поширення",
        )


if __name__ == "__main__":
    unittest.main()
