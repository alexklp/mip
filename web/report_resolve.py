"""Read-only збагачення чернетки звіту живими даними МІП."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from web.analyst_annotations import signal_fingerprint
from web.db import read_connection
from web.signal_export import (
    find_signal_candidate,
)
from web.signals import (
    DEFAULT_SNAPSHOT,
    load_signals,
    safe_link,
)
from web.signal_theses import (
    load_ready_theses_index,
)


EXAMPLE_CHARS = 1200


def _require_uuid_text(
    value: Any,
    *,
    field: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"{field}: значення відсутнє"
        )

    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError(
            f"{field}: некоректний UUID"
        ) from exc


def _event_time(row: dict[str, Any]):
    return (
        row.get("published_at")
        or row.get("collected_at")
    )


def _excerpt(
    value: str,
    limit: int = EXAMPLE_CHARS,
) -> str:
    text = " ".join(
        (value or "").split()
    )

    if len(text) <= limit:
        return text

    clipped = text[: limit + 1]
    boundary = clipped.rfind(" ")

    if boundary >= int(limit * 0.7):
        clipped = clipped[:boundary]
    else:
        clipped = clipped[:limit]

    return clipped.rstrip() + "…"


def strip_repeated_title(
    text: str,
    title: str,
) -> str:
    """Прибрати точне повторення заголовка на початку тексту."""
    body = (text or "").strip()
    heading = (title or "").strip()

    if not body or not heading:
        return body

    if body == heading:
        return ""

    if body.startswith(heading):
        rest = body[len(heading):]

        if (
            not rest
            or rest[0].isspace()
            or rest[0] in "—–-:."
        ):
            cleaned = rest.lstrip(
                " \t\r\n—–-:."
            )

            if cleaned:
                return cleaned

    return body


def choose_example_publication(
    publications: list[dict[str, Any]],
    *,
    representative_content_id: str | None,
) -> dict[str, Any]:
    if not publications:
        raise ValueError(
            "Сигнал не містить доступних публікацій"
        )

    def useful(row):
        return bool(
            (row.get("text") or "").strip()
            or (row.get("title") or "").strip()
        )

    def linked(row):
        return bool(
            row.get("url")
        )

    if representative_content_id:
        representative = [
            row
            for row in publications
            if str(row.get("content_id"))
            == representative_content_id
        ]

        for predicate in (
            lambda row: linked(row) and useful(row),
            useful,
            linked,
            lambda row: True,
        ):
            match = next(
                (
                    row
                    for row in representative
                    if predicate(row)
                ),
                None,
            )

            if match is not None:
                return match

    for predicate in (
        lambda row: linked(row) and useful(row),
        useful,
        linked,
        lambda row: True,
    ):
        match = next(
            (
                row
                for row in publications
                if predicate(row)
            ),
            None,
        )

        if match is not None:
            return match

    raise ValueError(
        "Не вдалося вибрати приклад публікації"
    )


def _publication_view(
    row: dict[str, Any],
) -> dict[str, Any]:
    event_time = _event_time(row)

    return {
        "source_name":
            row.get("source_name") or "",
        "source_type":
            row.get("source_type") or "",
        "source_group":
            row.get("source_group") or "",
        "title":
            row.get("title") or "",
        "text":
            _excerpt(
                row.get("text") or ""
            ),
        "url":
            row.get("url") or "",
        "observed_at":
            (
                event_time.isoformat()
                if event_time is not None
                else None
            ),
    }


def _fetch_material(
    conn,
    occurrence_id: str,
) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                io.occurrence_id::text
                    AS occurrence_id,
                io.content_id::text
                    AS content_id,
                io.source_id::text
                    AS source_id,
                s.name
                    AS source_name,
                s.source_type::text
                    AS source_type,
                s.source_group::text
                    AS source_group,
                ci.title,
                ci.text_content
                    AS text,
                io.external_ref,
                io.published_at,
                io.collected_at
            FROM item_occurrences io
            JOIN content_items ci
                USING (content_id)
            JOIN sources s
                USING (source_id)
            WHERE io.occurrence_id =
                %s::uuid
            """,
            (occurrence_id,),
        )

        row = cursor.fetchone()

    if row is None:
        raise ValueError(
            "Матеріал більше недоступний у базі"
        )

    return {
        **row,
        "url": safe_link(
            row["external_ref"] or ""
        ),
    }



MAX_SIGNAL_PUBLICATIONS = 1000


def _canonical_content_ids(
    value: Any,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
    ):
        raise ValueError(
            "Сигнал не містить content_ids"
        )

    result = [
        _require_uuid_text(
            item,
            field="content_id",
        )
        for item in value
    ]

    if len(result) != len(set(result)):
        raise ValueError(
            "Сигнал містить повторені content_ids"
        )

    return sorted(result)


def _source_groups(
    value: Any,
) -> list[str]:
    if value is None:
        return []

    if (
        not isinstance(value, list)
        or any(
            item not in {
                "ua_space",
                "ru_space",
            }
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise ValueError(
            "Некоректні source_groups сигналу"
        )

    return sorted(value)


def _optional_member_id(
    value: Any,
    *,
    content_ids: list[str],
) -> str | None:
    if not value:
        return None

    result = _require_uuid_text(
        value,
        field="representative_content_id",
    )

    if result not in set(content_ids):
        return None

    return result


def _candidate_display_title(
    candidate: dict[str, Any],
) -> str:
    title = (
        candidate.get(
            "representative_title"
        )
        or (
            candidate.get("chronology")
            or [{}]
        )[0].get("title")
        or "Пов’язані публікації"
    )

    title = str(title)

    if len(title) <= 180:
        return title

    return title[:179] + "…"


def _resolve_legacy_candidate(
    snapshot: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Відновити старий draft без fuzzy matching."""
    candidates = snapshot.get(
        "candidates"
    )

    if not isinstance(
        candidates,
        list,
    ):
        raise ValueError(
            "Некоректний snapshot сигналів"
        )

    candidate_id = raw.get(
        "candidate_id"
    )

    normalized_candidate_id = None

    if isinstance(candidate_id, str):
        try:
            normalized_candidate_id = (
                str(UUID(candidate_id))
            )
        except ValueError:
            normalized_candidate_id = None

    if normalized_candidate_id:
        try:
            return find_signal_candidate(
                snapshot,
                normalized_candidate_id,
            )
        except KeyError:
            pass

        anchor_matches = [
            candidate
            for candidate in candidates
            if normalized_candidate_id
            in set(
                candidate.get(
                    "content_ids"
                )
                or []
            )
        ]

        if len(anchor_matches) == 1:
            return anchor_matches[0]

        if len(anchor_matches) > 1:
            raise ValueError(
                "Старий сигнал неоднозначно "
                "відновлюється за content-anchor"
            )

    title = (
        raw.get("title")
        if isinstance(
            raw.get("title"),
            str,
        )
        else ""
    ).strip()

    summary = (
        raw.get("factual_summary")
        if isinstance(
            raw.get(
                "factual_summary"
            ),
            str,
        )
        else ""
    ).strip()

    matches = {}

    for candidate in candidates:
        candidate_key = candidate.get(
            "candidate_id"
        )

        if not candidate_key:
            continue

        title_match = bool(
            title
            and (
                title
                == _candidate_display_title(
                    candidate
                )
                or title
                == (
                    candidate.get(
                        "representative_title"
                    )
                    or ""
                ).strip()
            )
        )

        ai = candidate.get(
            "ai_theses"
        )

        ai_summary = (
            ai.get("summary", "").strip()
            if isinstance(ai, dict)
            and isinstance(
                ai.get("summary"),
                str,
            )
            else ""
        )

        summary_match = bool(
            summary
            and ai_summary
            and summary == ai_summary
        )

        if title_match or summary_match:
            matches[
                candidate_key
            ] = candidate

    if len(matches) == 1:
        return next(
            iter(
                matches.values()
            )
        )

    if len(matches) > 1:
        raise ValueError(
            "Старий сигнал неоднозначно "
            "відновлюється за точними ознаками"
        )

    raise ValueError(
        "Старий сигнал більше неможливо "
        "однозначно відновити. "
        "Відкрий сторінку «Сигнали» "
        "та додай його до звіту повторно."
    )


def _fetch_report_signal_publications(
    conn,
    *,
    content_ids: list[str],
    source_groups: list[str],
) -> list[dict[str, Any]]:
    query = """
        SELECT
            io.occurrence_id::text
                AS occurrence_id,
            io.content_id::text
                AS content_id,
            io.source_id::text
                AS source_id,
            s.name
                AS source_name,
            s.source_type::text
                AS source_type,
            s.source_group::text
                AS source_group,
            ci.title,
            ci.text_content
                AS text,
            io.external_ref,
            io.published_at,
            io.collected_at
        FROM item_occurrences io
        JOIN content_items ci
            USING (content_id)
        JOIN sources s
            USING (source_id)
        WHERE io.content_id =
            ANY(%s::uuid[])
    """

    params: list[Any] = [
        content_ids
    ]

    if source_groups:
        query += """
          AND s.source_group::text =
              ANY(%s::text[])
        """
        params.append(
            source_groups
        )

    query += """
        ORDER BY
            COALESCE(
                io.published_at,
                io.collected_at
            ),
            io.collected_at,
            io.occurrence_id
        LIMIT %s
    """

    params.append(
        MAX_SIGNAL_PUBLICATIONS + 1
    )

    with conn.cursor() as cursor:
        cursor.execute(
            query,
            tuple(params),
        )
        rows = cursor.fetchall()

    if len(rows) > MAX_SIGNAL_PUBLICATIONS:
        raise ValueError(
            "Сигнал має забагато публікацій "
            "для поточного формату звіту"
        )

    if not rows:
        raise ValueError(
            "Публікації сигналу більше "
            "не доступні у базі"
        )

    fetched_content_ids = {
        str(row["content_id"])
        for row in rows
    }

    missing = (
        set(content_ids)
        - fetched_content_ids
    )

    if missing:
        raise ValueError(
            "Частина матеріалів сигналу "
            "більше недоступна у базі"
        )

    return [
        {
            "occurrence_id":
                str(
                    row[
                        "occurrence_id"
                    ]
                ),
            "content_id":
                str(
                    row[
                        "content_id"
                    ]
                ),
            "source_id":
                str(
                    row[
                        "source_id"
                    ]
                ),
            "source_name":
                row[
                    "source_name"
                ]
                or "",
            "source_type":
                row[
                    "source_type"
                ]
                or "",
            "source_group":
                row[
                    "source_group"
                ]
                or "",
            "title":
                row["title"]
                or "",
            "text":
                row["text"]
                or "",
            "url":
                safe_link(
                    row[
                        "external_ref"
                    ]
                    or ""
                ),
            "published_at":
                row[
                    "published_at"
                ],
            "collected_at":
                row[
                    "collected_at"
                ],
        }
        for row in rows
    ]


def _fetch_excluded_content_ids(
    conn,
    *,
    content_ids: list[str],
) -> set[str]:
    fingerprint = signal_fingerprint(
        content_ids
    )

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT signal_context_id
            FROM analyst_signal_contexts
            WHERE fingerprint_version = 1
              AND signal_fingerprint = %s
            """,
            (fingerprint,),
        )

        context = cursor.fetchone()

        if context is None:
            return set()

        cursor.execute(
            """
            SELECT content_id::text
                AS content_id
            FROM analyst_signal_membership_annotations
            WHERE signal_context_id = %s
              AND included = FALSE
            """,
            (
                context[
                    "signal_context_id"
                ],
            ),
        )

        return {
            row["content_id"]
            for row in cursor.fetchall()
        }




def _select_signal_summary(
    *,
    durable_membership: bool,
    factual_summary: str,
    factual_summary_override: str | None,
    ai_summary: str,
) -> str:
    """Обрати summary без змішування auto snapshot і ручної правки."""
    if durable_membership:
        if factual_summary_override is None:
            return ai_summary

        return factual_summary_override

    # Legacy draft: зберігаємо стару поведінку.
    return factual_summary or ai_summary



def resolve_report_draft(
    *,
    payload: dict[str, Any],
    validated_draft: dict[str, Any],
    snapshot_path: Path = DEFAULT_SNAPSHOT,
    stale_seconds: int = 7200,
) -> dict[str, Any]:
    """Дочитати джерела, тези та приклади без запису в БД."""
    raw_items = payload.get("items")
    items = validated_draft.get("items")

    if (
        not isinstance(raw_items, list)
        or not isinstance(items, list)
        or len(raw_items) != len(items)
    ):
        raise ValueError(
            "Некоректний склад чернетки звіту"
        )

    needs_signals = any(
        item.get("type") == "signal"
        for item in items
    )

    needs_legacy_snapshot = any(
        item.get("type") == "signal"
        and not raw.get("content_ids")
        for raw, item in zip(
            raw_items,
            items,
            strict=True,
        )
    )

    snapshot = None

    if needs_legacy_snapshot:
        view = load_signals(
            snapshot_path,
            stale_seconds=stale_seconds,
        )

        snapshot = view.get(
            "snapshot"
        )

        if snapshot is None:
            raise ValueError(
                "Поточний знімок сигналів "
                "недоступний для відновлення "
                "старої чернетки"
            )

    theses_index = (
        load_ready_theses_index()
        if needs_signals
        else {}
    )

    result = {
        "schema_version":
            validated_draft[
                "schema_version"
            ],
        "items": [],
    }

    with read_connection() as conn:
        for raw, item in zip(
            raw_items,
            items,
            strict=True,
        ):
            resolved = dict(item)

            if item["type"] == "signal":
                durable_membership = bool(
                    raw.get("content_ids")
                )

                if durable_membership:
                    content_ids = (
                        _canonical_content_ids(
                            raw[
                                "content_ids"
                            ]
                        )
                    )

                    source_groups = (
                        _source_groups(
                            raw.get(
                                "source_groups"
                            )
                        )
                    )

                    representative_content_id = (
                        _optional_member_id(
                            raw.get(
                                "representative_content_id"
                            ),
                            content_ids=
                                content_ids,
                        )
                    )

                else:
                    candidate = (
                        _resolve_legacy_candidate(
                            snapshot,
                            raw,
                        )
                    )

                    content_ids = (
                        _canonical_content_ids(
                            candidate[
                                "content_ids"
                            ]
                        )
                    )

                    source_groups = (
                        _source_groups(
                            candidate.get(
                                "source_groups"
                            )
                        )
                    )

                    representative_content_id = (
                        _optional_member_id(
                            candidate.get(
                                "representative_content_id"
                            ),
                            content_ids=
                                content_ids,
                        )
                    )

                publications = (
                    _fetch_report_signal_publications(
                        conn,
                        content_ids=
                            content_ids,
                        source_groups=
                            source_groups,
                    )
                )

                excluded_ids = (
                    _fetch_excluded_content_ids(
                        conn,
                        content_ids=
                            content_ids,
                    )
                )

                effective_publications = [
                    row
                    for row in publications
                    if row["content_id"]
                    not in excluded_ids
                ]

                if not effective_publications:
                    raise ValueError(
                        "У сигналі не залишилося "
                        "матеріалів після "
                        "аналітичного виключення"
                    )

                cache_record = (
                    theses_index.get(
                        tuple(
                            sorted(
                                content_ids
                            )
                        )
                    )
                )

                ai_summary = ""
                ai_theses = []

                if isinstance(
                    cache_record,
                    dict,
                ):
                    if isinstance(
                        cache_record.get(
                            "summary"
                        ),
                        str,
                    ):
                        ai_summary = (
                            cache_record[
                                "summary"
                            ].strip()
                        )

                    theses = (
                        cache_record.get(
                            "theses"
                        )
                    )

                    if isinstance(
                        theses,
                        list,
                    ):
                        ai_theses = [
                            thesis[
                                "text"
                            ].strip()
                            for thesis
                            in theses
                            if (
                                isinstance(
                                    thesis,
                                    dict,
                                )
                                and isinstance(
                                    thesis.get(
                                        "text"
                                    ),
                                    str,
                                )
                                and thesis[
                                    "text"
                                ].strip()
                            )
                        ]

                draft_summary = (
                    item[
                        "factual_summary"
                    ].strip()
                )

                summary_override = item.get(
                    "factual_summary_override"
                )

                # Після зміни membership auto-summary і тези
                # вже не описують exact-набір матеріалів.
                # Ручний override аналітика зберігаємо.
                if excluded_ids:
                    if (
                        not durable_membership
                        and ai_summary
                        and draft_summary
                        == ai_summary
                    ):
                        draft_summary = ""

                    ai_summary = ""
                    ai_theses = []

                summary = _select_signal_summary(
                    durable_membership=
                        durable_membership,
                    factual_summary=
                        draft_summary,
                    factual_summary_override=
                        summary_override,
                    ai_summary=
                        ai_summary,
                )

                example = (
                    choose_example_publication(
                        effective_publications,
                        representative_content_id=(
                            representative_content_id
                            if representative_content_id
                            not in excluded_ids
                            else None
                        ),
                    )
                )

                resolved[
                    "content_ids"
                ] = content_ids

                resolved[
                    "source_groups"
                ] = source_groups

                resolved[
                    "signal_summary"
                ] = summary

                resolved[
                    "signal_theses"
                ] = ai_theses

                resolved[
                    "example_publication"
                ] = _publication_view(
                    example
                )

                resolved[
                    "source_count"
                ] = len({
                    row["source_id"]
                    for row
                    in effective_publications
                })

                resolved[
                    "occurrence_count"
                ] = len(
                    effective_publications
                )

                resolved[
                    "source_name"
                ] = (
                    example.get(
                        "source_name"
                    )
                    or ""
                )

                resolved[
                    "source_url"
                ] = (
                    example.get("url")
                    or ""
                )

            else:
                occurrence_id = (
                    _require_uuid_text(
                        raw.get(
                            "occurrence_id"
                        ),
                        field="occurrence_id",
                    )
                )

                content_id = (
                    _require_uuid_text(
                        raw.get(
                            "content_id"
                        ),
                        field="content_id",
                    )
                )

                publication = (
                    _fetch_material(
                        conn,
                        occurrence_id,
                    )
                )

                if (
                    str(
                        publication[
                            "content_id"
                        ]
                    )
                    != content_id
                ):
                    raise ValueError(
                        "Матеріал змінився: "
                        "content_id не збігається"
                    )

                source_text = (
                    item[
                        "factual_summary"
                    ].strip()
                    or publication[
                        "text"
                    ]
                    or ""
                )

                resolved[
                    "factual_summary"
                ] = strip_repeated_title(
                    source_text,
                    item["title"],
                )

                resolved[
                    "source_name"
                ] = (
                    publication[
                        "source_name"
                    ]
                    or ""
                )

                resolved[
                    "source_url"
                ] = (
                    publication["url"]
                    or ""
                )

                observed = (
                    publication[
                        "published_at"
                    ]
                    or publication[
                        "collected_at"
                    ]
                )

                resolved[
                    "observed_at"
                ] = (
                    observed.isoformat()
                    if observed is not None
                    else None
                )

                resolved[
                    "source_type"
                ] = (
                    publication[
                        "source_type"
                    ]
                    or ""
                )

                resolved[
                    "source_group"
                ] = (
                    publication[
                        "source_group"
                    ]
                    or ""
                )

            result["items"].append(
                resolved
            )

    return result
