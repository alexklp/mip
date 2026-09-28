"""Спільна підготовка даних для експорту вибраного сигналу."""

from __future__ import annotations

from typing import Any

from web.signals import safe_link


def find_signal_candidate(
    snapshot: dict[str, Any],
    candidate_id: str,
) -> dict[str, Any]:
    """Знайти рівно один сигнал у вже сформованому snapshot."""
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("Некоректний candidate_id")

    candidates = snapshot.get("candidates")

    if not isinstance(candidates, list):
        raise ValueError("Snapshot не містить candidates")

    matches = [
        candidate
        for candidate in candidates
        if candidate.get("candidate_id") == candidate_id
    ]

    if len(matches) != 1:
        raise KeyError(candidate_id)

    return matches[0]


def fetch_signal_publications(
    conn,
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    """Дочитати повні тексти лише для публікацій вибраного сигналу.

    Склад сигналу визначає snapshot. БД використовується тільки для
    збагачення вже відібраних occurrence повним текстом та метаданими.
    """
    chronology = candidate.get("chronology")

    if not isinstance(chronology, list) or not chronology:
        raise ValueError("Сигнал не містить chronology")

    occurrence_ids = [
        row.get("occurrence_id")
        for row in chronology
    ]

    if (
        any(
            not isinstance(value, str) or not value
            for value in occurrence_ids
        )
        or len(set(occurrence_ids)) != len(occurrence_ids)
    ):
        raise ValueError("Некоректні occurrence_id сигналу")

    content_ids = candidate.get("content_ids")

    if (
        not isinstance(content_ids, list)
        or not content_ids
        or any(
            not isinstance(value, str) or not value
            for value in content_ids
        )
    ):
        raise ValueError("Некоректні content_ids сигналу")

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                io.occurrence_id::text AS occurrence_id,
                io.content_id::text AS content_id,
                io.source_id::text AS source_id,
                s.name AS source_name,
                s.source_type::text AS source_type,
                s.source_group::text AS source_group,
                ci.title,
                ci.text_content,
                io.external_ref,
                io.published_at,
                io.collected_at
            FROM item_occurrences io
            JOIN content_items ci USING (content_id)
            JOIN sources s USING (source_id)
            WHERE io.occurrence_id = ANY(%s::uuid[])
            """,
            (occurrence_ids,),
        )
        fetched = cursor.fetchall()

    if len(fetched) != len(occurrence_ids):
        raise ValueError(
            "Snapshot signal не відповідає поточним даним БД"
        )

    by_occurrence = {
        str(row["occurrence_id"]): row
        for row in fetched
    }

    if set(by_occurrence) != set(occurrence_ids):
        raise ValueError(
            "Snapshot signal не відповідає поточним occurrence"
        )

    allowed_contents = set(content_ids)

    result: list[dict[str, Any]] = []

    for snapshot_row in chronology:
        occurrence_id = snapshot_row["occurrence_id"]
        row = by_occurrence[occurrence_id]
        content_id = str(row["content_id"])

        if content_id not in allowed_contents:
            raise ValueError(
                "Публікація не належить content_ids сигналу"
            )

        result.append(
            {
                "occurrence_id": occurrence_id,
                "content_id": content_id,
                "source_id": str(row["source_id"]),
                "source_name": row["source_name"] or "",
                "source_type": row["source_type"] or "",
                "source_group": row["source_group"] or "",
                "title": row["title"] or "",
                "text": row["text_content"] or "",
                "url": safe_link(row["external_ref"] or ""),
                "published_at": row["published_at"],
                "collected_at": row["collected_at"],
            }
        )

    return result



def fetch_global_signal_publications(
    conn,
    candidate: dict[str, Any],
    *,
    window_start,
    window_end,
    history_start,
) -> list[dict[str, Any]]:
    """Дочитати exact-історію глобального сигналу.

    Поточне membership сигналу визначає snapshot. Після його
    перевірки до звіту додаються старіші occurrence тих самих
    content_ids у спостережуваних UA/RU джерелах.
    """
    content_ids = candidate.get("content_ids")
    source_groups = candidate.get("source_groups")
    expected_total = candidate.get("occurrence_count")
    expected_sources = candidate.get("source_count")

    if (
        not isinstance(content_ids, list)
        or not content_ids
        or any(
            not isinstance(value, str) or not value
            for value in content_ids
        )
    ):
        raise ValueError(
            "Некоректні content_ids глобального сигналу"
        )

    if (
        not isinstance(source_groups, list)
        or not source_groups
        or any(
            value not in {"ua_space", "ru_space"}
            for value in source_groups
        )
    ):
        raise ValueError(
            "Некоректні source_groups глобального сигналу"
        )

    if (
        type(expected_total) is not int
        or expected_total < 1
        or type(expected_sources) is not int
        or expected_sources < 1
    ):
        raise ValueError(
            "Некоректні лічильники глобального сигналу"
        )

    if not (
        history_start
        < window_start
        < window_end
    ):
        raise ValueError(
            "Некоректні часові межі exact-history"
        )

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                io.occurrence_id::text AS occurrence_id,
                io.content_id::text AS content_id,
                io.source_id::text AS source_id,
                s.name AS source_name,
                s.source_type::text AS source_type,
                s.source_group::text AS source_group,
                ci.title,
                ci.text_content,
                io.external_ref,
                io.published_at,
                io.collected_at
            FROM item_occurrences io
            JOIN content_items ci USING (content_id)
            JOIN sources s USING (source_id)
            WHERE io.content_id = ANY(%s::uuid[])
              AND s.source_group::text
                  = ANY(%s::text[])
              AND io.collected_at >= %s
              AND io.collected_at < %s
            ORDER BY
                COALESCE(
                    io.published_at,
                    io.collected_at
                ),
                io.collected_at,
                io.occurrence_id
            """,
            (
                content_ids,
                ["ua_space", "ru_space"],
                history_start,
                window_end,
            ),
        )

        fetched = cursor.fetchall()

    # Спочатку окремо перевіряємо старий 48h membership.
    current_rows = [
        row
        for row in fetched
        if (
            row["collected_at"] >= window_start
            and row["source_group"] in source_groups
        )
    ]

    if len(current_rows) != expected_total:
        raise ValueError(
            "Snapshot signal не відповідає поточним даним БД"
        )

    if (
        len({
            str(row["source_id"])
            for row in current_rows
        })
        != expected_sources
    ):
        raise ValueError(
            "Snapshot source_count не відповідає поточним даним"
        )

    current_groups = {
        row["source_group"]
        for row in current_rows
    }

    if current_groups != set(source_groups):
        raise ValueError(
            "Snapshot source_groups не відповідають поточним даним"
        )

    allowed_contents = set(content_ids)
    seen_occurrences: set[str] = set()
    result: list[dict[str, Any]] = []

    for row in fetched:
        occurrence_id = str(
            row["occurrence_id"]
        )
        content_id = str(
            row["content_id"]
        )

        if occurrence_id in seen_occurrences:
            raise ValueError(
                "Дублікат occurrence у global exact-history"
            )

        if content_id not in allowed_contents:
            raise ValueError(
                "Occurrence не належить content_ids сигналу"
            )

        seen_occurrences.add(
            occurrence_id
        )

        result.append(
            {
                "occurrence_id": occurrence_id,
                "content_id": content_id,
                "source_id": str(
                    row["source_id"]
                ),
                "source_name": (
                    row["source_name"]
                    or ""
                ),
                "source_type": (
                    row["source_type"]
                    or ""
                ),
                "source_group": (
                    row["source_group"]
                    or ""
                ),
                "title": (
                    row["title"]
                    or ""
                ),
                "text": (
                    row["text_content"]
                    or ""
                ),
                "url": safe_link(
                    row["external_ref"]
                    or ""
                ),
                "published_at": row[
                    "published_at"
                ],
                "collected_at": row[
                    "collected_at"
                ],
            }
        )

    return result


def build_signal_xlsx(
    *,
    snapshot: dict[str, Any],
    candidate: dict[str, Any],
    publications: list[dict[str, Any]],
    scope_label: str = "C1",
    strict_candidate_totals: bool = True,
) -> bytes:
    """Сформувати робочий XLSX-звіт за одним сигналом."""
    import io
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import xlsxwriter

    if not publications:
        raise ValueError("Сигнал не містить публікацій")

    publication_source_count = len({
        row["source_id"]
        for row in publications
    })

    if strict_candidate_totals:
        if (
            len(publications)
            != candidate.get("occurrence_count")
        ):
            raise ValueError(
                "Кількість публікацій не відповідає snapshot"
            )

        if (
            publication_source_count
            != candidate.get("source_count")
        ):
            raise ValueError(
                "Кількість джерел не відповідає snapshot"
            )

    else:
        if (
            len(publications)
            < candidate.get("occurrence_count", 0)
        ):
            raise ValueError(
                "Розширена історія втратила публікації snapshot"
            )

        if (
            publication_source_count
            < candidate.get("source_count", 0)
        ):
            raise ValueError(
                "Розширена історія втратила джерела snapshot"
            )

    for row in publications:
        if len(row["text"]) > 32767:
            raise ValueError(
                "Текст публікації перевищує ліміт Excel 32767 символів"
            )

    kyiv = ZoneInfo("Europe/Kyiv")

    def aware_datetime(value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)

        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
        ):
            raise ValueError(
                "Очікується timezone-aware datetime"
            )

        return value

    def kyiv_naive(value: Any) -> datetime:
        return (
            aware_datetime(value)
            .astimezone(kyiv)
            .replace(tzinfo=None)
        )

    def event_time(row: dict[str, Any]) -> datetime:
        return aware_datetime(
            row["published_at"]
            or row["collected_at"]
        )

    def space_name(value: str) -> str:
        return {
            "ua_space": "Український",
            "ru_space": "Російський",
        }.get(value, value)

    def source_type_name(value: str) -> str:
        return {
            "telegram": "Telegram",
            "rss": "RSS",
            "web": "Сайт",
        }.get(value.lower(), value) if value else ""

    ordered = sorted(
        publications,
        key=lambda row: (
            event_time(row),
            aware_datetime(row["collected_at"]),
            row["occurrence_id"],
        ),
    )

    first_time = event_time(ordered[0])
    last_time = event_time(ordered[-1])
    duration_seconds = (
        last_time - first_time
    ).total_seconds()

    duration_total = max(
        0,
        int(duration_seconds),
    )
    duration_hours, remainder = divmod(
        duration_total,
        3600,
    )
    duration_minutes, duration_secs = divmod(
        remainder,
        60,
    )
    duration_text = (
        f"{duration_hours} год "
        f"{duration_minutes:02d} хв "
        f"{duration_secs:02d} с"
    )

    source_groups = candidate.get(
        "source_groups",
        [],
    )

    spaces_text = " + ".join(
        space_name(group)
        for group in source_groups
    )

    title = (
        candidate.get("representative_title")
        or "Сигнал без заголовка"
    )

    output = io.BytesIO()

    workbook = xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )

    workbook.set_properties({
        "title": f"МІП — звіт за сигналом: {title}",
        "subject": "Аналітичний звіт за вибраним сигналом",
        "author": "МІП",
        "company": "МІП",
    })

    # -----------------------------
    # Формати
    # -----------------------------

    fmt_title = workbook.add_format({
        "bold": True,
        "font_size": 18,
        "font_color": "#FFFFFF",
        "bg_color": "#1F2937",
        "valign": "vcenter",
        "text_wrap": True,
    })

    fmt_subtitle = workbook.add_format({
        "bold": True,
        "font_size": 11,
        "font_color": "#374151",
    })

    fmt_label = workbook.add_format({
        "bold": True,
        "font_color": "#4B5563",
        "bg_color": "#F3F4F6",
        "border": 1,
        "border_color": "#D1D5DB",
        "valign": "top",
    })

    fmt_value = workbook.add_format({
        "border": 1,
        "border_color": "#D1D5DB",
        "valign": "top",
        "text_wrap": True,
    })

    fmt_value_number = workbook.add_format({
        "border": 1,
        "border_color": "#D1D5DB",
        "valign": "top",
        "align": "right",
        "num_format": "0",
    })

    fmt_value_date = workbook.add_format({
        "border": 1,
        "border_color": "#D1D5DB",
        "valign": "top",
        "num_format": "dd.mm.yyyy hh:mm:ss",
    })

    fmt_value_duration = workbook.add_format({
        "border": 1,
        "border_color": "#D1D5DB",
        "valign": "top",
        "num_format": "[h]:mm:ss",
    })

    fmt_metric_label = workbook.add_format({
        "bold": True,
        "font_color": "#6B7280",
        "bg_color": "#F3F4F6",
        "border": 1,
        "border_color": "#D1D5DB",
        "align": "center",
        "valign": "vcenter",
    })

    fmt_metric_value = workbook.add_format({
        "bold": True,
        "font_size": 15,
        "font_color": "#111827",
        "border": 1,
        "border_color": "#D1D5DB",
        "align": "center",
        "valign": "vcenter",
    })

    fmt_metric_text = workbook.add_format({
        "bold": True,
        "font_size": 12,
        "font_color": "#111827",
        "border": 1,
        "border_color": "#D1D5DB",
        "align": "center",
        "valign": "vcenter",
        "text_wrap": True,
    })

    fmt_section = workbook.add_format({
        "bold": True,
        "font_size": 12,
        "font_color": "#111827",
        "bottom": 2,
        "bottom_color": "#9CA3AF",
    })

    fmt_header = workbook.add_format({
        "bold": True,
        "font_color": "#FFFFFF",
        "bg_color": "#374151",
        "border": 1,
        "border_color": "#4B5563",
        "align": "center",
        "valign": "vcenter",
        "text_wrap": True,
    })

    fmt_cell = workbook.add_format({
        "border": 1,
        "border_color": "#E5E7EB",
        "valign": "top",
    })

    fmt_cell_wrap = workbook.add_format({
        "border": 1,
        "border_color": "#E5E7EB",
        "valign": "top",
        "text_wrap": True,
    })

    fmt_date = workbook.add_format({
        "border": 1,
        "border_color": "#E5E7EB",
        "valign": "top",
        "num_format": "dd.mm.yyyy hh:mm:ss",
    })

    fmt_tplus = workbook.add_format({
        "border": 1,
        "border_color": "#E5E7EB",
        "valign": "top",
        "num_format": "[h]:mm:ss",
    })

    fmt_link = workbook.add_format({
        "font_color": "#0563C1",
        "underline": 1,
        "border": 1,
        "border_color": "#E5E7EB",
        "valign": "top",
    })

    fmt_note = workbook.add_format({
        "font_color": "#6B7280",
        "italic": True,
        "font_size": 9,
    })

    # =========================================================
    # 01. ЗВЕДЕННЯ
    # =========================================================

    summary = workbook.add_worksheet("Зведення")
    summary.hide_gridlines(2)
    summary.set_tab_color("#374151")

    summary.set_column("A:A", 25)
    summary.set_column("B:B", 26)
    summary.set_column("C:H", 16)

    summary.set_row(0, 42)

    summary.merge_range(
        "A1:H1",
        "МІП · ЗВІТ ЗА СИГНАЛОМ",
        fmt_title,
    )

    summary.write(
        "A3",
        "Сигнал",
        fmt_label,
    )
    summary.merge_range(
        "B3:H5",
        title,
        fmt_value,
    )

    as_of = snapshot.get("as_of")
    report_generated_at = (
        datetime.now(kyiv)
        .replace(tzinfo=None)
    )

    summary.write("A7", "Контур", fmt_label)
    summary.merge_range(
        "B7:H7",
        scope_label,
        fmt_value,
    )

    summary.write(
        "A8",
        "Дані станом на",
        fmt_label,
    )
    summary.merge_range(
        "B8:H8",
        kyiv_naive(as_of),
        fmt_value_date,
    )

    summary.write(
        "A9",
        "Звіт сформовано",
        fmt_label,
    )
    summary.merge_range(
        "B9:H9",
        report_generated_at,
        fmt_value_date,
    )

    # Ключові показники — окремо від хронології.
    summary.merge_range(
        "A11:B11",
        "Публікації",
        fmt_metric_label,
    )
    summary.merge_range(
        "A12:B12",
        len(ordered),
        fmt_metric_value,
    )

    summary.merge_range(
        "C11:D11",
        "Джерела",
        fmt_metric_label,
    )
    summary.merge_range(
        "C12:D12",
        len({
            row["source_id"]
            for row in ordered
        }),
        fmt_metric_value,
    )

    summary.merge_range(
        "E11:H11",
        "Простір",
        fmt_metric_label,
    )
    summary.merge_range(
        "E12:H12",
        spaces_text,
        fmt_metric_text,
    )

    summary.merge_range(
        "A14:H14",
        "Поширення",
        fmt_section,
    )

    summary.merge_range(
        "A15:B15",
        "Перша публікація",
        fmt_label,
    )
    summary.merge_range(
        "C15:D15",
        kyiv_naive(first_time),
        fmt_value_date,
    )
    summary.merge_range(
        "E15:H15",
        ordered[0]["source_name"],
        fmt_value,
    )

    summary.merge_range(
        "A16:B16",
        "Остання публікація",
        fmt_label,
    )
    summary.merge_range(
        "C16:D16",
        kyiv_naive(last_time),
        fmt_value_date,
    )
    summary.merge_range(
        "E16:H16",
        ordered[-1]["source_name"],
        fmt_value,
    )

    summary.merge_range(
        "A17:B17",
        "Тривалість",
        fmt_label,
    )
    summary.merge_range(
        "C17:H17",
        duration_text,
        fmt_value,
    )

    summary.merge_range(
        "A20:H20",
        "Динаміка поширення",
        fmt_section,
    )


    # =========================================================
    # 02. ПОШИРЕННЯ
    # =========================================================

    spread = workbook.add_worksheet("Поширення")
    spread.hide_gridlines(2)
    spread.freeze_panes(1, 0)
    spread.set_tab_color("#6B7280")

    spread_headers = [
        "№",
        "Час",
        "T+",
        "Простір",
        "Джерело",
        "Тип джерела",
        "Заголовок",
        "Посилання",
    ]

    for col, header in enumerate(
        spread_headers
    ):
        spread.write(
            0,
            col,
            header,
            fmt_header,
        )

    ua_count = 0
    ru_count = 0

    for index, row in enumerate(
        ordered,
        start=1,
    ):
        excel_row = index

        observed_at = event_time(row)
        elapsed = (
            observed_at - first_time
        ).total_seconds()

        if row["source_group"] == "ua_space":
            ua_count += 1
        elif row["source_group"] == "ru_space":
            ru_count += 1

        spread.write_number(
            excel_row,
            0,
            index,
            fmt_cell,
        )
        spread.write_datetime(
            excel_row,
            1,
            kyiv_naive(observed_at),
            fmt_date,
        )
        spread.write_number(
            excel_row,
            2,
            elapsed / 86400,
            fmt_tplus,
        )
        spread.write_string(
            excel_row,
            3,
            space_name(
                row["source_group"]
            ),
            fmt_cell,
        )
        spread.write_string(
            excel_row,
            4,
            row["source_name"],
            fmt_cell,
        )
        spread.write_string(
            excel_row,
            5,
            source_type_name(
                row["source_type"]
            ),
            fmt_cell,
        )
        spread.write_string(
            excel_row,
            6,
            row["title"],
            fmt_cell_wrap,
        )

        if row["url"]:
            spread.write_url(
                excel_row,
                7,
                row["url"],
                fmt_link,
                "Відкрити",
            )
        else:
            spread.write_string(
                excel_row,
                7,
                "",
                fmt_cell,
            )

        # Технічні колонки тільки для графіка.
        spread.write_number(
            excel_row,
            8,
            index,
        )
        spread.write_number(
            excel_row,
            9,
            ua_count,
        )
        spread.write_number(
            excel_row,
            10,
            ru_count,
        )

    last_spread_row = len(ordered)

    spread.autofilter(
        0,
        0,
        last_spread_row,
        7,
    )

    spread.set_column("A:A", 6)
    spread.set_column("B:B", 20)
    spread.set_column("C:C", 12)
    spread.set_column("D:D", 15)
    spread.set_column("E:E", 28)
    spread.set_column("F:F", 14)
    spread.set_column("G:G", 55)
    spread.set_column("H:H", 13)

    spread.set_column(
        "I:K",
        None,
        None,
        {"hidden": True},
    )

    # =========================================================
    # ГРАФІК
    # =========================================================

    chart = workbook.add_chart({
        "type": "scatter",
        "subtype": "straight_with_markers",
    })

    chart.add_series({
        "name": "Публікації",
        "categories": [
            "Поширення",
            1,
            1,
            last_spread_row,
            1,
        ],
        "values": [
            "Поширення",
            1,
            8,
            last_spread_row,
            8,
        ],
        "marker": {
            "type": "circle",
            "size": 6,
        },
        "line": {
            "width": 1.5,
        },
    })

    chart.set_title({
        "name": "Хронологія поширення",
    })

    chart.set_x_axis({
        "name": "Час",
        "date_axis": True,
        "num_format": "dd.mm hh:mm",
        "label_position": "low",
        "major_gridlines": {
            "visible": True,
        },
    })

    chart.set_y_axis({
        "name": "№ публікації",
        "major_unit": 1,
        "min": 1,
        "max": len(ordered) + 1,
    })

    chart.set_legend({
        "position": "bottom",
    })

    # Дані графіка зберігаються у прихованих технічних
    # колонках I:K, але мають залишатися видимими на графіку.
    chart.show_hidden_data()

    chart.set_size({
        "width": 800,
        "height": 320,
    })

    summary.insert_chart(
        "A21",
        chart,
    )

    # =========================================================
    # 03. ПУБЛІКАЦІЇ
    # =========================================================

    pubs = workbook.add_worksheet(
        "Публікації"
    )
    pubs.hide_gridlines(2)
    pubs.freeze_panes(1, 0)
    pubs.set_tab_color("#9CA3AF")

    publication_headers = [
        "№",
        "Час",
        "T+",
        "Простір",
        "Джерело",
        "Тип джерела",
        "Заголовок",
        "Текст",
        "Посилання",
    ]

    for col, header in enumerate(
        publication_headers
    ):
        pubs.write(
            0,
            col,
            header,
            fmt_header,
        )

    for index, row in enumerate(
        ordered,
        start=1,
    ):
        excel_row = index

        observed_at = event_time(row)
        elapsed = (
            observed_at - first_time
        ).total_seconds()

        pubs.write_number(
            excel_row,
            0,
            index,
            fmt_cell,
        )
        pubs.write_datetime(
            excel_row,
            1,
            kyiv_naive(observed_at),
            fmt_date,
        )
        pubs.write_number(
            excel_row,
            2,
            elapsed / 86400,
            fmt_tplus,
        )
        pubs.write_string(
            excel_row,
            3,
            space_name(
                row["source_group"]
            ),
            fmt_cell,
        )
        pubs.write_string(
            excel_row,
            4,
            row["source_name"],
            fmt_cell,
        )
        pubs.write_string(
            excel_row,
            5,
            source_type_name(
                row["source_type"]
            ),
            fmt_cell,
        )
        pubs.write_string(
            excel_row,
            6,
            row["title"],
            fmt_cell_wrap,
        )
        pubs.write_string(
            excel_row,
            7,
            row["text"],
            fmt_cell_wrap,
        )

        if row["url"]:
            pubs.write_url(
                excel_row,
                8,
                row["url"],
                fmt_link,
                "Відкрити",
            )
        else:
            pubs.write_string(
                excel_row,
                8,
                "",
                fmt_cell,
            )

    last_publication_row = len(ordered)

    pubs.autofilter(
        0,
        0,
        last_publication_row,
        8,
    )

    pubs.set_column("A:A", 6)
    pubs.set_column("B:B", 20)
    pubs.set_column("C:C", 12)
    pubs.set_column("D:D", 15)
    pubs.set_column("E:E", 28)
    pubs.set_column("F:F", 14)
    pubs.set_column("G:G", 45)
    pubs.set_column("H:H", 80)
    pubs.set_column("I:I", 13)

    workbook.close()

    return output.getvalue()
