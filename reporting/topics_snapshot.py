#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from wordcloud import WordCloud

from demo_topics import (
    DEMO_GENERIC_UNIGRAMS,
    analysis_tokens_with_surfaces,
    load_noise,
    visible_term,
)
from morphology import detect_language
from nlp_prepare import (
    DB_DSN,
    clean_text,
    document_terms,
    load_rules,
    load_stopwords,
)
from temporal_shift import log_odds


HERE = Path(__file__).resolve().parent

DEFAULT_OUTPUT = HERE / "topics.latest.json"

SCHEMA_VERSION = "topics/1"
ALGORITHM_VERSION = "topics-lexical-monitored-cache/2"

SOURCE_GROUPS = ("ru_space", "ua_space")
VIEWS = ("all",) + SOURCE_GROUPS
UNITS = ("phrases", "words")

THEME_LIMIT = 120
CHANGE_LIMIT = 40

THEME_MIN_PUBLICATIONS = 2
CHANGE_MIN_PUBLICATIONS = 3

SOURCE_LIMIT = 20

CLOUD_PHRASES = 52
CLOUD_WORDS = 64
CLOUD_CHANGES = 42

CLOUD_WIDTH = 1200
CLOUD_HEIGHT = 680

FONT_PATH = Path(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
)

PREVIEW_CHARS = 700


SQL = """
WITH latest AS (
    SELECT DISTINCT ON (content_id)
        content_id,
        routing_version,
        decision
    FROM content_routing_decisions
    WHERE created_at < %s
    ORDER BY
        content_id,
        routing_version DESC,
        created_at DESC
)
SELECT
    io.occurrence_id::text,
    io.content_id::text,
    io.source_id::text,
    s.name,
    s.source_type,
    s.url_or_handle,
    s.source_group,
    io.external_ref,
    io.published_at,
    io.collected_at,
    COALESCE(io.published_at, io.collected_at) AS observed_at,
    ci.text_content,
    r.routing_version
FROM item_occurrences io
JOIN content_items ci
  ON ci.content_id = io.content_id
JOIN sources s
  ON s.source_id = io.source_id
JOIN latest r
  ON r.content_id = io.content_id
WHERE r.decision IN ('analyze', 'maybe')
  AND s.source_group IN ('ua_space', 'ru_space')
  AND COALESCE(io.published_at, io.collected_at) >= %s
  AND COALESCE(io.published_at, io.collected_at) < %s
  AND io.collected_at < %s
ORDER BY
    COALESCE(io.published_at, io.collected_at),
    io.occurrence_id
"""


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def compact_text(value: str | None, limit: int = PREVIEW_CHARS) -> str:
    text = " ".join((value or "").split())

    if len(text) <= limit:
        return text

    return text[: limit - 1].rstrip() + "…"


def marker_id(unit: str, label_key: str) -> str:
    digest = hashlib.sha1(
        f"{unit}\0{label_key}".encode("utf-8")
    ).hexdigest()[:14]

    return f"{unit}:{digest}"


def current_or_previous(
    observed_at: datetime,
    current_start: datetime,
) -> str:
    return (
        "current"
        if observed_at >= current_start
        else "previous"
    )


def pct(value: int, total: int) -> float:
    if not total:
        return 0.0

    return 100.0 * value / total


def hour_bin(
    observed_at: datetime,
    start_at: datetime,
) -> int:
    value = int(
        (observed_at - start_at).total_seconds()
        // 3600
    )

    return min(23, max(0, value))


def fetch_rows() -> tuple[datetime, list[dict[str, Any]]]:
    with psycopg.connect(DB_DSN) as conn:
        conn.execute(
            "SET TRANSACTION ISOLATION LEVEL "
            "REPEATABLE READ, READ ONLY"
        )

        observed_now = conn.execute(
            "SELECT now()"
        ).fetchone()[0]

        as_of = observed_now.replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        start_at = as_of - timedelta(hours=48)

        raw = conn.execute(
            SQL,
            (
                as_of,
                start_at,
                as_of,
                as_of,
            ),
        ).fetchall()

    rows = []

    for row in raw:
        (
            occurrence_id,
            content_id,
            source_id,
            source_name,
            source_type,
            source_url_or_handle,
            source_group,
            external_ref,
            published_at,
            collected_at,
            observed_at,
            text_content,
            routing_version,
        ) = row

        rows.append(
            {
                "occurrence_id": occurrence_id,
                "content_id": content_id,
                "source_id": source_id,
                "source_name": source_name,
                "source_type": source_type,
                "source_url_or_handle": source_url_or_handle,
                "source_group": source_group,
                "external_ref": external_ref,
                "published_at": published_at,
                "collected_at": collected_at,
                "observed_at": observed_at,
                "text_content": text_content or "",
                "routing_version": routing_version,
            }
        )

    return as_of, rows



FEED_BOILERPLATE_RE = re.compile(
    r"\s*(?:<p>\s*)?The post\b.*?\bfirst appeared on\b.*?(?:</p>\s*)?$",
    re.IGNORECASE | re.DOTALL,
)

MARKER_FAMILIES = {
    "володимир путін": {
        "words": {
            "путін",
            "путин",
        },
        "phrases": {
            "володимир путін",
            "владимир путин",
        },
    },
    "володимир зеленський": {
        "words": {
            "зеленський",
            "зеленский",
        },
        "phrases": {
            "володимир зеленський",
            "владимир зеленский",
        },
    },
    "ліндсі грем": {
        "words": set(),
        "phrases": {
            "ліндсі грем",
            "ліндсі грема",
            "линдси грэм",
        },
    },
    "пекельний санкція": {
        "words": set(),
        "phrases": {
            "пекельний санкція",
            "адский санкция",
        },
    },
}


def strip_feed_boilerplate(text: str) -> str:
    """Remove known RSS/CMS footer noise from Topics input only."""
    return FEED_BOILERPLATE_RE.sub("", text)


def canonicalize_marker_families(
    words: set[str],
    phrases: set[str],
) -> tuple[set[str], set[str]]:
    """
    Collapse selected cross-language unigram/bigram aliases into one
    canonical marker family.

    The canonical marker is inserted once per content, so publication
    counts remain document-distinct rather than alias-additive.
    """
    words = set(words)
    phrases = set(phrases)

    for canonical, aliases in MARKER_FAMILIES.items():
        matched = bool(
            words.intersection(aliases["words"])
            or phrases.intersection(aliases["phrases"])
        )

        if not matched:
            continue

        words.difference_update(aliases["words"])
        phrases.difference_update(aliases["phrases"])
        phrases.add(canonical)

    return words, phrases



TOPICS_NLP_CACHE_VERSION = "topics-nlp-cache/1"

TOPICS_NLP_CACHE_PATH = (
    Path.home()
    / ".local"
    / "state"
    / "mip"
    / "topics_nlp_cache_v1.jsonl"
)

TOPICS_MIN_NLP_COVERAGE_PCT = 95.0


def topics_nlp_cache_input_hash(
    text: str,
    source_names: list[str],
) -> str:
    payload = json.dumps(
        {
            "cache_version": TOPICS_NLP_CACHE_VERSION,
            "text": text,
            "source_names": sorted(source_names),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


def load_topics_nlp_cache() -> dict[
    tuple[str, str],
    dict[str, Any],
]:
    result = {}

    if not TOPICS_NLP_CACHE_PATH.is_file():
        return result

    with TOPICS_NLP_CACHE_PATH.open(
        encoding="utf-8"
    ) as handle:
        for line in handle:
            if not line.strip():
                continue

            try:
                row = json.loads(line)
            except Exception:
                continue

            if (
                row.get("cache_version")
                != TOPICS_NLP_CACHE_VERSION
            ):
                continue

            content_id = row.get("content_id")
            fingerprint = row.get("input_hash")
            tokens = row.get("tokens", [])
            surfaces = row.get("surfaces", [])

            if (
                not content_id
                or not fingerprint
                or row.get("lang") == "error"
                or not isinstance(tokens, list)
                or not isinstance(surfaces, list)
                or len(tokens) != len(surfaces)
            ):
                continue

            result[
                (
                    str(content_id),
                    fingerprint,
                )
            ] = row

    return result


def process_contents(
    rows: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, set[str]]],
    dict[str, Counter],
    Counter,
]:
    stopwords = load_stopwords()
    rules = load_rules()
    noise = load_noise()

    contents: dict[str, str] = {}
    content_sources: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        content_id = row["content_id"]

        contents[content_id] = row["text_content"]
        content_sources[content_id].add(
            row["source_name"]
        )

    terms_by_content: dict[
        str,
        dict[str, set[str]],
    ] = {}

    phrase_labels: dict[str, Counter] = defaultdict(Counter)
    language_counts = Counter()

    nlp_cache = load_topics_nlp_cache()
    cache_hits = 0
    cache_misses = 0

    total = len(contents)
    started = time.perf_counter()

    for index, (content_id, text) in enumerate(
        contents.items(),
        1,
    ):
        source_names = sorted(
            content_sources[content_id]
        )

        fingerprint = (
            topics_nlp_cache_input_hash(
                text,
                source_names,
            )
        )

        cached = nlp_cache.get(
            (
                str(content_id),
                fingerprint,
            )
        )

        if cached is None:
            cache_misses += 1
            continue

        cache_hits += 1

        lang = cached["lang"]
        tokens = cached["tokens"]
        surfaces = cached["surfaces"]

        language_counts[lang] += 1

        words = document_terms(
            tokens,
            ngram=1,
            stopwords=stopwords,
        )

        phrases = document_terms(
            tokens,
            ngram=2,
            stopwords=stopwords,
        )

        words, phrases = canonicalize_marker_families(
            words,
            phrases,
        )

        visible_words = {
            term
            for term in words
            if visible_term(term, noise)
            and term not in DEMO_GENERIC_UNIGRAMS
        }

        visible_phrases = {
            term
            for term in phrases
            if visible_term(term, noise)
        }

        terms_by_content[content_id] = {
            "words": visible_words,
            "phrases": visible_phrases,
        }

        for i in range(len(tokens) - 1):
            term = f"{tokens[i]} {tokens[i + 1]}"

            if term not in visible_phrases:
                continue

            surface = (
                f"{surfaces[i]} {surfaces[i + 1]}"
            )

            phrase_labels[term][surface] += 1

        if index % 100 == 0 or index == total:
            elapsed = time.perf_counter() - started
            rate = index / elapsed if elapsed else 0.0

            print(
                f"NLP {index}/{total} "
                f"{elapsed:.1f}s "
                f"{rate:.1f} docs/s",
                flush=True,
            )

    print(
        "Topics NLP cache: "
        f"hits={cache_hits} "
        f"misses={cache_misses}",
        flush=True,
    )

    cache_status = {
        "hits": cache_hits,
        "misses": cache_misses,
        "total": total,
    }

    return (
        terms_by_content,
        phrase_labels,
        language_counts,
        cache_status,
    )


def display_label(
    unit: str,
    term: str,
    phrase_labels: dict[str, Counter],
) -> str:
    if unit == "words":
        return term

    labels = phrase_labels.get(term)

    if labels:
        return labels.most_common(1)[0][0]

    return term


def canonicalize_markers(
    terms_by_content: dict[
        str,
        dict[str, set[str]],
    ],
    phrase_labels: dict[str, Counter],
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, set[str]]],
    dict[str, dict[str, Any]],
]:
    term_to_marker: dict[
        str,
        dict[str, str],
    ] = {
        unit: {}
        for unit in UNITS
    }

    marker_terms: dict[
        str,
        list[str],
    ] = defaultdict(list)

    marker_label: dict[
        str,
        str,
    ] = {}

    all_terms: dict[
        str,
        set[str],
    ] = {
        unit: set()
        for unit in UNITS
    }

    for units in terms_by_content.values():
        for unit in UNITS:
            all_terms[unit].update(
                units[unit]
            )

    for unit in UNITS:
        buckets: dict[
            str,
            list[tuple[str, str]],
        ] = defaultdict(list)

        for term in sorted(all_terms[unit]):
            label = display_label(
                unit,
                term,
                phrase_labels,
            ).strip()

            label_key = label.casefold()

            buckets[label_key].append(
                (term, label)
            )

        for label_key, rows in buckets.items():
            mid = marker_id(
                unit,
                label_key,
            )

            marker_label[mid] = rows[0][1]

            for term, _label in rows:
                term_to_marker[unit][term] = mid
                marker_terms[mid].append(term)

    content_markers: dict[
        str,
        dict[str, set[str]],
    ] = {}

    for content_id, units in terms_by_content.items():
        content_markers[content_id] = {}

        for unit in UNITS:
            content_markers[content_id][unit] = {
                term_to_marker[unit][term]
                for term in units[unit]
            }

    marker_meta = {}

    for mid, terms in marker_terms.items():
        unit = mid.split(":", 1)[0]

        marker_meta[mid] = {
            "marker_id": mid,
            "unit": unit,
            "label": marker_label[mid],
            "normalized_terms": sorted(set(terms)),
        }

    return (
        term_to_marker,
        content_markers,
        marker_meta,
    )


def summarize_occurrences(
    rows: list[dict[str, Any]],
    *,
    current_start: datetime,
) -> dict[str, dict[str, dict[str, int]]]:
    result = {}

    for view in VIEWS:
        result[view] = {}

        for slice_name in ("previous", "current"):
            selected = [
                row
                for row in rows
                if current_or_previous(
                    row["observed_at"],
                    current_start,
                )
                == slice_name
                and (
                    view == "all"
                    or row["source_group"] == view
                )
            ]

            result[view][slice_name] = {
                "publications": len(selected),
                "materials": len(
                    {
                        row["content_id"]
                        for row in selected
                    }
                ),
                "sources": len(
                    {
                        row["source_id"]
                        for row in selected
                    }
                ),
            }

    return result


def aggregate_marker_stats(
    rows: list[dict[str, Any]],
    content_markers: dict[
        str,
        dict[str, set[str]],
    ],
    *,
    current_start: datetime,
) -> dict[
    tuple[str, str, str, str],
    dict[str, Any],
]:
    stats: dict[
        tuple[str, str, str, str],
        dict[str, Any],
    ] = {}

    for row in rows:
        slice_name = current_or_previous(
            row["observed_at"],
            current_start,
        )

        views = (
            "all",
            row["source_group"],
        )

        for unit in UNITS:
            mids = content_markers.get(
                row["content_id"],
                {},
            ).get(unit, set())

            for mid in mids:
                for view in views:
                    key = (
                        slice_name,
                        view,
                        unit,
                        mid,
                    )

                    bucket = stats.setdefault(
                        key,
                        {
                            "publications": 0,
                            "contents": set(),
                            "sources": set(),
                        },
                    )

                    bucket["publications"] += 1
                    bucket["contents"].add(
                        row["content_id"]
                    )
                    bucket["sources"].add(
                        row["source_id"]
                    )

    return stats


def get_stat(
    stats: dict,
    slice_name: str,
    view: str,
    unit: str,
    mid: str,
) -> dict[str, Any]:
    return stats.get(
        (
            slice_name,
            view,
            unit,
            mid,
        ),
        {
            "publications": 0,
            "contents": set(),
            "sources": set(),
        },
    )


def theme_rows(
    *,
    view: str,
    unit: str,
    marker_meta: dict[str, dict[str, Any]],
    stats: dict,
    summary: dict,
) -> list[dict[str, Any]]:
    total = summary[view]["current"]["publications"]

    rows = []

    for mid, meta in marker_meta.items():
        if meta["unit"] != unit:
            continue

        current = get_stat(
            stats,
            "current",
            view,
            unit,
            mid,
        )

        publications = current["publications"]

        if publications < THEME_MIN_PUBLICATIONS:
            continue

        rows.append(
            {
                "marker_id": mid,
                "label": meta["label"],
                "publications": publications,
                "materials": len(
                    current["contents"]
                ),
                "sources": len(
                    current["sources"]
                ),
                "share": round(
                    pct(publications, total),
                    3,
                ),
            }
        )

    rows.sort(
        key=lambda row: (
            -row["publications"],
            -row["sources"],
            row["label"].casefold(),
        )
    )

    return rows[:THEME_LIMIT]


def change_rows(
    *,
    view: str,
    unit: str,
    marker_meta: dict[str, dict[str, Any]],
    stats: dict,
    summary: dict,
) -> dict[str, list[dict[str, Any]]]:
    current_total = summary[
        view
    ]["current"]["publications"]

    previous_total = summary[
        view
    ]["previous"]["publications"]

    rows = []

    for mid, meta in marker_meta.items():
        if meta["unit"] != unit:
            continue

        current = get_stat(
            stats,
            "current",
            view,
            unit,
            mid,
        )

        previous = get_stat(
            stats,
            "previous",
            view,
            unit,
            mid,
        )

        c = current["publications"]
        p = previous["publications"]

        if c + p < CHANGE_MIN_PUBLICATIONS:
            continue

        current_share = pct(
            c,
            current_total,
        )

        previous_share = pct(
            p,
            previous_total,
        )

        delta = (
            current_share
            - previous_share
        )

        if math.isclose(
            delta,
            0.0,
            abs_tol=1e-12,
        ):
            continue

        score = log_odds(
            c,
            current_total,
            p,
            previous_total,
        )

        rows.append(
            {
                "marker_id": mid,
                "label": meta["label"],
                "previous_publications": p,
                "current_publications": c,
                "previous_share": round(
                    previous_share,
                    3,
                ),
                "current_share": round(
                    current_share,
                    3,
                ),
                "delta_pp": round(
                    delta,
                    3,
                ),
                "score": round(
                    score,
                    6,
                ),
            }
        )

    growing = [
        row
        for row in rows
        if row["delta_pp"] > 0
    ]

    declining = [
        row
        for row in rows
        if row["delta_pp"] < 0
    ]

    growing.sort(
        key=lambda row: (
            -row["score"],
            -row["delta_pp"],
            -row["current_publications"],
            row["label"].casefold(),
        )
    )

    declining.sort(
        key=lambda row: (
            row["score"],
            row["delta_pp"],
            -row["previous_publications"],
            row["label"].casefold(),
        )
    )

    return {
        "growing": growing[:CHANGE_LIMIT],
        "declining": declining[:CHANGE_LIMIT],
    }


def cloud_layout(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    unit: str,
) -> list[dict[str, Any]]:
    if not rows:
        return []

    if not FONT_PATH.exists():
        raise RuntimeError(
            f"font missing: {FONT_PATH}"
        )

    selected = rows[
        : (
            CLOUD_PHRASES
            if unit == "phrases"
            else CLOUD_WORDS
        )
    ]

    if mode == "themes":
        frequencies = {
            row["label"]: max(
                float(
                    row["publications"]
                ),
                0.1,
            )
            for row in selected
        }

        max_font_size = (
            68
            if unit == "phrases"
            else 88
        )

        min_font_size = 13

        relative_scaling = 0.32

    else:
        selected = selected[:CLOUD_CHANGES]

        frequencies = {
            row["label"]: max(
                abs(
                    float(
                        row["delta_pp"]
                    )
                ),
                0.1,
            )
            for row in selected
        }

        max_font_size = (
            54
            if unit == "phrases"
            else 60
        )

        min_font_size = 13
        relative_scaling = 0.24

    lookup = {
        row["label"]: row
        for row in selected
    }

    if len(lookup) != len(selected):
        raise RuntimeError(
            "Duplicate display labels in cloud selection"
        )

    cloud = WordCloud(
        width=CLOUD_WIDTH,
        height=CLOUD_HEIGHT,
        background_color="white",
        font_path=str(FONT_PATH),
        max_words=len(selected),
        collocations=False,
        prefer_horizontal=1.0,
        random_state=42,
        margin=8,
        max_font_size=max_font_size,
        min_font_size=min_font_size,
        relative_scaling=relative_scaling,
    ).generate_from_frequencies(
        frequencies
    )

    result = []

    for rank, item in enumerate(
        cloud.layout_
    ):
        (
            (label, _frequency),
            font_size,
            position,
            _orientation,
            _colour,
        ) = item

        y, x = map(int, position)
        row = lookup[label]
        mid = row["marker_id"]

        payload = {
            "marker_id": mid,
            "label": label,
            "rank": rank + 1,
            "x": x,
            "y": y,
            "font_size": int(
                font_size
            ),
        }

        if mode == "changes":
            payload["direction"] = (
                "up"
                if row["delta_pp"] > 0
                else "down"
            )

        result.append(payload)

    return result


def build_views(
    marker_meta: dict[str, dict[str, Any]],
    stats: dict,
    summary: dict,
) -> tuple[
    dict[str, Any],
    set[str],
]:
    views = {}
    selected_marker_ids = set()

    for view in VIEWS:
        block = {
            "themes": {},
            "changes": {},
            "clouds": {
                "themes": {},
                "changes": {},
            },
        }

        for unit in UNITS:
            themes = theme_rows(
                view=view,
                unit=unit,
                marker_meta=marker_meta,
                stats=stats,
                summary=summary,
            )

            changes = change_rows(
                view=view,
                unit=unit,
                marker_meta=marker_meta,
                stats=stats,
                summary=summary,
            )

            block["themes"][unit] = themes
            block["changes"][unit] = changes

            block["clouds"]["themes"][unit] = (
                cloud_layout(
                    themes,
                    mode="themes",
                    unit=unit,
                )
            )

            change_cloud_rows = (
                changes["growing"]
                + changes["declining"]
            )

            change_cloud_rows.sort(
                key=lambda row: (
                    -abs(
                        row["delta_pp"]
                    ),
                    row["label"].casefold(),
                )
            )

            block["clouds"]["changes"][unit] = (
                cloud_layout(
                    change_cloud_rows,
                    mode="changes",
                    unit=unit,
                )
            )

            selected_marker_ids.update(
                row["marker_id"]
                for row in themes
            )

            for direction in (
                "growing",
                "declining",
            ):
                selected_marker_ids.update(
                    row["marker_id"]
                    for row
                    in changes[direction]
                )

        views[view] = block

    return (
        views,
        selected_marker_ids,
    )


def source_rank(
    selected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts = Counter(
        row["source_id"]
        for row in selected
    )

    metadata = {}

    for row in selected:
        metadata[row["source_id"]] = {
            "source_id": row["source_id"],
            "name": row["source_name"],
            "source_type": row["source_type"],
            "url_or_handle": (
                row["source_url_or_handle"]
            ),
        }

    result = []

    for source_id, publications in counts.most_common(
        SOURCE_LIMIT
    ):
        result.append(
            {
                **metadata[source_id],
                "publications": publications,
            }
        )

    return result


def build_marker_details(
    *,
    rows: list[dict[str, Any]],
    selected_marker_ids: set[str],
    marker_meta: dict[str, dict[str, Any]],
    content_markers: dict[
        str,
        dict[str, set[str]],
    ],
    summary: dict,
    previous_start: datetime,
    current_start: datetime,
    as_of: datetime,
) -> tuple[
    dict[str, Any],
    set[str],
]:
    details = {}
    referenced_occurrences = set()

    for mid in sorted(
        selected_marker_ids
    ):
        meta = marker_meta[mid]
        unit = meta["unit"]

        detail = {
            **meta,
            "views": {},
        }

        for view in VIEWS:
            view_block = {}

            for slice_name, slice_start in (
                (
                    "previous",
                    previous_start,
                ),
                (
                    "current",
                    current_start,
                ),
            ):
                selected = [
                    row
                    for row in rows
                    if current_or_previous(
                        row["observed_at"],
                        current_start,
                    )
                    == slice_name
                    and (
                        view == "all"
                        or row["source_group"]
                        == view
                    )
                    and mid
                    in content_markers.get(
                        row["content_id"],
                        {},
                    ).get(
                        unit,
                        set(),
                    )
                ]

                selected.sort(
                    key=lambda row: (
                        row["observed_at"],
                        row["occurrence_id"],
                    ),
                    reverse=True,
                )

                total = summary[
                    view
                ][slice_name][
                    "publications"
                ]

                hourly = [0] * 24
                hourly_source_counts = [
                    Counter()
                    for _ in range(24)
                ]

                for row in selected:
                    bucket = hour_bin(
                        row["observed_at"],
                        slice_start,
                    )

                    hourly[bucket] += 1

                    hourly_source_counts[
                        bucket
                    ][
                        row["source_name"]
                    ] += 1

                hourly_sources = []

                for counts in hourly_source_counts:
                    ordered = sorted(
                        counts.items(),
                        key=lambda item: (
                            -item[1],
                            item[0].casefold(),
                        ),
                    )

                    hourly_sources.append(
                        [
                            {
                                "name": name,
                                "publications": publications,
                            }
                            for name, publications
                            in ordered
                        ]
                    )

                source_ranking = source_rank(
                    selected
                )

                occurrence_refs = [
                    row["occurrence_id"]
                    for row in selected
                ]

                referenced_occurrences.update(
                    occurrence_refs
                )

                view_block[slice_name] = {
                    "publications": len(
                        selected
                    ),
                    "materials": len(
                        {
                            row["content_id"]
                            for row in selected
                        }
                    ),
                    "sources": len(
                        {
                            row["source_id"]
                            for row in selected
                        }
                    ),
                    "share": round(
                        pct(
                            len(selected),
                            total,
                        ),
                        3,
                    ),
                    "hourly": hourly,
                    "hourly_sources": hourly_sources,
                    "source_ranking": (
                        source_ranking
                    ),
                    "evidence_total": len(
                        selected
                    ),
                    "occurrence_refs": (
                        occurrence_refs
                    ),
                    "evidence_limit_reached": False,
                }

            detail["views"][view] = (
                view_block
            )

        details[mid] = detail

    return (
        details,
        referenced_occurrences,
    )


def occurrence_payload(
    row: dict[str, Any],
) -> dict[str, Any]:
    return {
        "occurrence_id": row["occurrence_id"],
        "content_id": row["content_id"],
        "source_id": row["source_id"],
        "source_name": row["source_name"],
        "source_type": row["source_type"],
        "source_url_or_handle": (
            row["source_url_or_handle"]
        ),
        "source_group": row["source_group"],
        "external_ref": row["external_ref"],
        "published_at": (
            iso(row["published_at"])
            if row["published_at"]
            else None
        ),
        "collected_at": iso(
            row["collected_at"]
        ),
        "observed_at": iso(
            row["observed_at"]
        ),
        "text_preview": compact_text(
            row["text_content"]
        ),
    }


def validate_snapshot(
    snapshot: dict[str, Any],
) -> None:
    if (
        snapshot.get("schema_version")
        != SCHEMA_VERSION
    ):
        raise ValueError(
            "schema_version mismatch"
        )

    if (
        snapshot.get(
            "algorithm_version"
        )
        != ALGORITHM_VERSION
    ):
        raise ValueError(
            "algorithm_version mismatch"
        )

    current = snapshot[
        "windows"
    ]["current"]

    previous = snapshot[
        "windows"
    ]["previous"]

    current_start = datetime.fromisoformat(
        current["start"]
    )

    current_end = datetime.fromisoformat(
        current["end"]
    )

    previous_start = datetime.fromisoformat(
        previous["start"]
    )

    previous_end = datetime.fromisoformat(
        previous["end"]
    )

    if (
        current_end - current_start
        != timedelta(hours=24)
    ):
        raise ValueError(
            "current window is not 24h"
        )

    if (
        previous_end - previous_start
        != timedelta(hours=24)
    ):
        raise ValueError(
            "previous window is not 24h"
        )

    if previous_end != current_start:
        raise ValueError(
            "windows are not contiguous"
        )

    summary = snapshot["summary"]

    for slice_name in (
        "previous",
        "current",
    ):
        all_publications = summary[
            "all"
        ][slice_name][
            "publications"
        ]

        grouped_publications = sum(
            summary[group][slice_name][
                "publications"
            ]
            for group in SOURCE_GROUPS
        )

        if (
            all_publications
            != grouped_publications
        ):
            raise ValueError(
                f"{slice_name}: "
                "all != ru + ua"
            )

    current_publications = summary[
        "all"
    ]["current"]["publications"]

    if current_publications <= 0:
        raise ValueError(
            "current corpus is empty"
        )

    markers = snapshot["markers"]
    occurrences = snapshot["occurrences"]

    for view in VIEWS:
        for unit in UNITS:
            for row in snapshot[
                "views"
            ][view]["themes"][unit]:
                if (
                    row["marker_id"]
                    not in markers
                ):
                    raise ValueError(
                        "theme references "
                        "missing marker"
                    )

            for direction in (
                "growing",
                "declining",
            ):
                for row in snapshot[
                    "views"
                ][view]["changes"][
                    unit
                ][direction]:
                    if (
                        row["marker_id"]
                        not in markers
                    ):
                        raise ValueError(
                            "change references "
                            "missing marker"
                        )

    for marker in markers.values():
        for view in marker[
            "views"
        ].values():
            for slice_data in (
                view["previous"],
                view["current"],
            ):
                if len(
                    slice_data["hourly"]
                ) != 24:
                    raise ValueError(
                        "hourly series "
                        "must have 24 bins"
                    )

                if len(
                    slice_data["hourly_sources"]
                ) != 24:
                    raise ValueError(
                        "hourly source series "
                        "must have 24 bins"
                    )

                for publications, sources in zip(
                    slice_data["hourly"],
                    slice_data["hourly_sources"],
                ):
                    source_publications = sum(
                        item["publications"]
                        for item in sources
                    )

                    if (
                        source_publications
                        != publications
                    ):
                        raise ValueError(
                            "hourly source counts "
                            "do not match hourly total"
                        )

                for occurrence_id in (
                    slice_data[
                        "occurrence_refs"
                    ]
                ):
                    if (
                        occurrence_id
                        not in occurrences
                    ):
                        raise ValueError(
                            "missing evidence "
                            "occurrence"
                        )


def atomic_write_json(
    path: Path,
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )

            handle.write("\n")
            handle.flush()
            os.fsync(
                handle.fileno()
            )

        os.replace(
            temp_name,
            path,
        )

    except Exception:
        try:
            os.unlink(
                temp_name
            )
        except FileNotFoundError:
            pass

        raise


def build_snapshot() -> dict[str, Any]:
    started = time.perf_counter()

    as_of, rows = fetch_rows()
    db_finished = time.perf_counter()

    current_start = (
        as_of
        - timedelta(hours=24)
    )

    previous_start = (
        as_of
        - timedelta(hours=48)
    )

    if not rows:
        raise RuntimeError(
            "No monitored occurrences "
            "in the last 48 hours"
        )

    (
        terms_by_content,
        phrase_labels,
        language_counts,
        cache_status,
    ) = process_contents(rows)

    nlp_finished = time.perf_counter()

    covered_content_ids = set(
        terms_by_content
    )

    analyzed_publications = sum(
        row["content_id"] in covered_content_ids
        for row in rows
    )

    total_publications = len(rows)

    publication_coverage_pct = (
        100.0
        * analyzed_publications
        / total_publications
        if total_publications
        else 0.0
    )

    material_coverage_pct = (
        100.0
        * cache_status["hits"]
        / cache_status["total"]
        if cache_status["total"]
        else 0.0
    )

    nlp_coverage = {
        "input_publications": total_publications,
        "analyzed_publications": analyzed_publications,
        "publication_coverage_pct": round(
            publication_coverage_pct,
            3,
        ),
        "input_materials": cache_status["total"],
        "analyzed_materials": cache_status["hits"],
        "missing_materials": cache_status["misses"],
        "material_coverage_pct": round(
            material_coverage_pct,
            3,
        ),
        "minimum_required_pct": (
            TOPICS_MIN_NLP_COVERAGE_PCT
        ),
    }

    if (
        publication_coverage_pct
        < TOPICS_MIN_NLP_COVERAGE_PCT
    ):
        raise RuntimeError(
            "Topics NLP cache coverage too low: "
            f"{publication_coverage_pct:.2f}% "
            f"< {TOPICS_MIN_NLP_COVERAGE_PCT:.2f}%"
        )

    (
        _term_to_marker,
        content_markers,
        marker_meta,
    ) = canonicalize_markers(
        terms_by_content,
        phrase_labels,
    )

    summary = summarize_occurrences(
        rows,
        current_start=current_start,
    )

    stats = aggregate_marker_stats(
        rows,
        content_markers,
        current_start=current_start,
    )

    views, selected_marker_ids = (
        build_views(
            marker_meta,
            stats,
            summary,
        )
    )

    markers, referenced_occurrences = (
        build_marker_details(
            rows=rows,
            selected_marker_ids=(
                selected_marker_ids
            ),
            marker_meta=marker_meta,
            content_markers=(
                content_markers
            ),
            summary=summary,
            previous_start=(
                previous_start
            ),
            current_start=current_start,
            as_of=as_of,
        )
    )

    occurrences = {
        row["occurrence_id"]: (
            occurrence_payload(row)
        )
        for row in rows
        if row["occurrence_id"]
        in referenced_occurrences
    }

    published_fallbacks = sum(
        row["published_at"] is None
        for row in rows
    )

    warnings = []

    if published_fallbacks:
        warnings.append(
            f"{published_fallbacks} "
            "публікацій використовують "
            "collected_at як fallback часу."
        )

    if cache_status["misses"]:
        warnings.append(
            "NLP cache coverage: "
            f"{publication_coverage_pct:.2f}% "
            f"публікацій; "
            f"{cache_status['misses']} "
            "матеріалів ще не мають "
            "актуального NLP cache."
        )

    routing_versions = sorted(
        {
            row["routing_version"]
            for row in rows
        }
    )

    aggregation_finished = (
        time.perf_counter()
    )

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": (
            ALGORITHM_VERSION
        ),
        "generated_at": iso(
            datetime.now(
                timezone.utc
            )
        ),
        "as_of": iso(as_of),
        "windows": {
            "previous": {
                "start": iso(
                    previous_start
                ),
                "end": iso(
                    current_start
                ),
            },
            "current": {
                "start": iso(
                    current_start
                ),
                "end": iso(as_of),
            },
        },
        "selection": {
            "routing": {
                "resolution": (
                    "latest decision "
                    "per content_id "
                    "with created_at < as_of"
                ),
                "decision": "analyze_or_maybe",
                "decisions": [
                    "analyze",
                    "maybe",
                ],
                "routing_versions": (
                    routing_versions
                ),
            },
            "source_groups": list(
                SOURCE_GROUPS
            ),
            "time_membership": (
                "COALESCE("
                "item_occurrences."
                "published_at,"
                "item_occurrences."
                "collected_at)"
            ),
            "popularity_unit": (
                "item_occurrence"
            ),
        },
        "summary": summary,
        "nlp_coverage": nlp_coverage,
        "languages": dict(
            language_counts
        ),
        "views": views,
        "markers": markers,
        "occurrences": occurrences,
        "warnings": warnings,
        "incomplete": bool(
            cache_status["misses"]
        ),
        "critical_incomplete": False,
        "timings": {
            "db_read_seconds": round(
                db_finished
                - started,
                3,
            ),
            "nlp_seconds": round(
                nlp_finished
                - db_finished,
                3,
            ),
            "aggregate_seconds": round(
                aggregation_finished
                - nlp_finished,
                3,
            ),
            "total_seconds": round(
                aggregation_finished
                - started,
                3,
            ),
        },
    }

    validate_snapshot(
        snapshot
    )

    return snapshot


def print_summary(
    snapshot: dict[str, Any],
    output: Path,
) -> None:
    print()
    print("===== TOPICS SNAPSHOT =====")
    print(
        "schema       =",
        snapshot["schema_version"],
    )
    print(
        "algorithm    =",
        snapshot["algorithm_version"],
    )
    print(
        "as_of        =",
        snapshot["as_of"],
    )
    print(
        "routing      =",
        snapshot["selection"][
            "routing"
        ]["routing_versions"],
    )

    print()

    for view in VIEWS:
        current = snapshot[
            "summary"
        ][view]["current"]

        previous = snapshot[
            "summary"
        ][view]["previous"]

        print(
            f"{view:8s} "
            f"current="
            f"{current['publications']:4d} "
            f"prev="
            f"{previous['publications']:4d} "
            f"sources="
            f"{current['sources']:2d}"
        )

    print()
    print(
        "markers      =",
        len(snapshot["markers"]),
    )
    print(
        "evidence     =",
        len(snapshot["occurrences"]),
    )
    print(
        "languages    =",
        snapshot["languages"],
    )
    print(
        "warnings     =",
        snapshot["warnings"],
    )
    print(
        "timings      =",
        snapshot["timings"],
    )
    print(
        "output       =",
        output,
    )

    print()
    print(
        "===== TOP 12 PHRASES / ALL ====="
    )

    for index, row in enumerate(
        snapshot[
            "views"
        ]["all"]["themes"]["phrases"][:12],
        1,
    ):
        print(
            f"{index:2d}. "
            f"{row['label'][:42]:42s} "
            f"pub={row['publications']:3d} "
            f"src={row['sources']:2d} "
            f"share={row['share']:5.1f}%"
        )

    print()
    print(
        "===== TOP 8 GROWING / ALL ====="
    )

    for row in snapshot[
        "views"
    ]["all"]["changes"]["phrases"][
        "growing"
    ][:8]:
        print(
            f"{row['label'][:40]:40s} "
            f"{row['previous_publications']:3d}"
            f"->{row['current_publications']:3d} "
            f"{row['delta_pp']:+6.2f} pp"
        )

    print()
    print(
        "===== TOP 8 DECLINING / ALL ====="
    )

    for row in snapshot[
        "views"
    ]["all"]["changes"]["phrases"][
        "declining"
    ][:8]:
        print(
            f"{row['label'][:40]:40s} "
            f"{row['previous_publications']:3d}"
            f"->{row['current_publications']:3d} "
            f"{row['delta_pp']:+6.2f} pp"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build read-only live MIP "
            "topic-space snapshot"
        )
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
    )

    args = parser.parse_args()

    snapshot = build_snapshot()

    atomic_write_json(
        args.output,
        snapshot,
    )

    print_summary(
        snapshot,
        args.output,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
