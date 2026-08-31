#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from demo_examples import build_metadata, compact
from demo_topics import (
    DEMO_GENERIC_UNIGRAMS,
    GROUPS,
    NEW_END,
    NEW_START,
    OLD_END,
    OLD_START,
    load_noise,
    process_slice,
    visible_term,
)
from nlp_prepare import DB_DSN
from temporal_shift import log_odds


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"
OUTPUT_FILE = OUTPUT_DIR / "topic_explorer_data.json"

THEME_WORDS = 28
THEME_PHRASES = 24
CHANGE_UP = 10
CHANGE_DOWN = 10
SHIFT_MIN_DOCS = 3
EVIDENCE_MODE = "all_matching"


def keys(kind: str) -> tuple[str, str, str]:
    if kind == "words":
        return "unigram_df", "unigram_labels", "unigram_docs"

    if kind == "phrases":
        return "bigram_df", "bigram_labels", "bigram_docs"

    raise ValueError(kind)


def label_for(data: dict, kind: str, term: str) -> str:
    _, labels_key, _ = keys(kind)

    # Для слів лишаємо нормалізовану лему.
    if kind == "words":
        return term

    labels = data[labels_key].get(term)

    if labels:
        return labels.most_common(1)[0][0]

    return term


def top_terms(
    data: dict,
    kind: str,
    noise: set[str],
    limit: int,
) -> list[dict]:
    df_key, _, docs_key = keys(kind)

    df = data[df_key]
    total = data["documents"]

    terms = [
        term
        for term in df
        if visible_term(term, noise)
    ]

    terms.sort(
        key=lambda term: (
            -df[term],
            term,
        )
    )

    rows = []

    for term in terms[:limit]:
        documents = df[term]

        rows.append(
            {
                "term": term,
                "label": label_for(data, kind, term),
                "documents": documents,
                "doc_pct": (
                    round(100.0 * documents / total, 3)
                    if total
                    else 0.0
                ),
                "content_ids": data[docs_key].get(term, []),
            }
        )

    return rows


def change_terms(
    previous: dict,
    current: dict,
    kind: str,
    noise: set[str],
) -> dict:
    df_key, labels_key, docs_key = keys(kind)

    old_df = previous[df_key]
    new_df = current[df_key]

    old_n = previous["documents"]
    new_n = current["documents"]

    rows = []

    for term in set(old_df) | set(new_df):
        old_documents = old_df[term]
        new_documents = new_df[term]

        if old_documents + new_documents < SHIFT_MIN_DOCS:
            continue

        if not visible_term(term, noise):
            continue

        old_pct = (
            100.0 * old_documents / old_n
            if old_n
            else 0.0
        )

        new_pct = (
            100.0 * new_documents / new_n
            if new_n
            else 0.0
        )

        delta_pct = new_pct - old_pct

        if delta_pct == 0:
            continue

        score = log_odds(
            new_documents,
            new_n,
            old_documents,
            old_n,
        )

        labels = (
            current[labels_key].get(term)
            or previous[labels_key].get(term)
        )

        if kind == "words":
            label = term
        elif labels:
            label = labels.most_common(1)[0][0]
        else:
            label = term

        rows.append(
            {
                "term": term,
                "label": label,
                "old_documents": old_documents,
                "new_documents": new_documents,
                "old_pct": round(old_pct, 3),
                "new_pct": round(new_pct, 3),
                "delta_pct": round(delta_pct, 3),
                "score": round(score, 6),
                "old_content_ids": previous[docs_key].get(term, []),
                "new_content_ids": current[docs_key].get(term, []),
            }
        )

    # Морфологічні моделі іноді дають різні нормалізовані
    # терміни для однакової поверхневої фрази.
    # Об'єднуємо такі колізії за display label через union content_ids,
    # щоб не подвоювати document frequency.
    merged: dict[str, dict] = {}

    for row in rows:
        key = row["label"].strip().casefold()

        if key not in merged:
            merged[key] = {
                "label": row["label"],
                "terms": {row["term"]},
                "old_content_ids": set(row["old_content_ids"]),
                "new_content_ids": set(row["new_content_ids"]),
            }
            continue

        merged[key]["terms"].add(row["term"])
        merged[key]["old_content_ids"].update(
            row["old_content_ids"]
        )
        merged[key]["new_content_ids"].update(
            row["new_content_ids"]
        )

    merged_rows = []

    for bucket in merged.values():
        old_ids = sorted(bucket["old_content_ids"])
        new_ids = sorted(bucket["new_content_ids"])

        old_documents = len(old_ids)
        new_documents = len(new_ids)

        old_pct = (
            100.0 * old_documents / old_n
            if old_n
            else 0.0
        )

        new_pct = (
            100.0 * new_documents / new_n
            if new_n
            else 0.0
        )

        delta_pct = new_pct - old_pct

        if delta_pct == 0:
            continue

        terms = sorted(bucket["terms"])

        # Для колізії використовуємо display label як стабільний
        # UI-ідентифікатор; пошук preview все одно має fallback
        # за surface label.
        term = (
            terms[0]
            if len(terms) == 1
            else bucket["label"].strip().casefold()
        )

        merged_rows.append(
            {
                "term": term,
                "label": bucket["label"],
                "old_documents": old_documents,
                "new_documents": new_documents,
                "old_pct": round(old_pct, 3),
                "new_pct": round(new_pct, 3),
                "delta_pct": round(delta_pct, 3),
                "score": round(
                    log_odds(
                        new_documents,
                        new_n,
                        old_documents,
                        old_n,
                    ),
                    6,
                ),
                "old_content_ids": old_ids,
                "new_content_ids": new_ids,
                "normalized_terms": terms,
            }
        )

    rows = merged_rows

    growing = [
        row
        for row in rows
        if row["delta_pct"] > 0
    ]

    declining = [
        row
        for row in rows
        if row["delta_pct"] < 0
    ]

    growing.sort(
        key=lambda row: (
            -row["score"],
            -row["delta_pct"],
            -row["new_documents"],
            row["term"],
        )
    )

    declining.sort(
        key=lambda row: (
            row["score"],
            row["delta_pct"],
            -row["old_documents"],
            row["term"],
        )
    )

    return {
        "growing": growing[:CHANGE_UP],
        "declining": declining[:CHANGE_DOWN],
    }


def collect_candidate_ids(data: dict) -> set[str]:
    ids = set()

    for group in data["groups"].values():
        for unit in ("words", "phrases"):

            # Тематичний профіль:
            # усі документи поточного зрізу.
            for row in group["themes"][unit]:
                ids.update(
                    str(content_id)
                    for content_id
                    in row.get("content_ids", [])
                )

            # Зміни:
            # для drill-down потрібні ОБИДВА зрізи
            # незалежно від напрямку зміни.
            for direction in ("growing", "declining"):
                for row in group["changes"][unit][direction]:
                    ids.update(
                        str(content_id)
                        for content_id
                        in row.get("old_content_ids", [])
                    )

                    ids.update(
                        str(content_id)
                        for content_id
                        in row.get("new_content_ids", [])
                    )

    return ids



def normalize_content_ids(values) -> list[str]:
    """Нормалізує content_id до унікального списку str."""
    result = []
    seen = set()

    for value in values or []:
        content_id = str(value)

        if content_id in seen:
            continue

        seen.add(content_id)
        result.append(content_id)

    return result


def prepare_examples(
    content_ids,
    metadata: dict[str, dict],
    term: str,
    label: str,
    slice_membership: dict[str, set[str]],
) -> list[dict]:
    """
    Формує повний evidence drill-down без втрати документів.

    Різноманітність джерел впливає лише на порядок показу,
    але не на склад evidence.
    """
    ids = normalize_content_ids(content_ids)

    missing = [
        content_id
        for content_id in ids
        if content_id not in metadata
    ]

    if missing:
        raise RuntimeError(
            f"metadata missing for {len(missing)} content_ids "
            f"term={term!r}: {missing[:5]}"
        )

    candidates = [
        metadata[content_id]
        for content_id in ids
    ]

    candidates.sort(
        key=lambda row: (
            -float(row["routing_score"]),
            str(row["first_seen_at"]),
            row["content_id"],
        )
    )

    # Спочатку по одному сильному документу з різних джерел.
    diverse = []
    remaining = []
    used_sources = set()

    for row in candidates:
        sources = tuple(
            row.get("source_names") or []
        )

        primary_source = (
            sources[0]
            if sources
            else "unknown"
        )

        if primary_source in used_sources:
            remaining.append(row)
            continue

        diverse.append(row)
        used_sources.add(primary_source)

    # Потім усі інші документи.
    # Нічого не відсікаємо.
    ordered = diverse + remaining

    normalized_membership = {
        slice_name: {
            str(content_id)
            for content_id in members
        }
        for slice_name, members
        in slice_membership.items()
    }

    result = []

    for row in ordered:
        item = compact(
            row,
            term,
            label,
        )

        content_id = str(
            item["content_id"]
        )

        slices = [
            slice_name
            for slice_name, members
            in normalized_membership.items()
            if content_id in members
        ]

        item["evidence_slices"] = slices

        if len(slices) == 1:
            item["evidence_slice"] = slices[0]
        elif len(slices) > 1:
            item["evidence_slice"] = "both"
        else:
            item["evidence_slice"] = "unknown"

        result.append(item)

    return result


def attach_examples(data: dict, metadata: dict[str, dict]) -> int:
    total = 0

    for group in data["groups"].values():
        for unit in ("words", "phrases"):

            for row in group["themes"][unit]:
                ids = normalize_content_ids(
                    row.get("content_ids", [])
                )

                current_members = set(ids)

                row["examples"] = prepare_examples(
                    ids,
                    metadata,
                    row["term"],
                    row["label"],
                    {
                        "current": current_members,
                    },
                )

                row["evidence_documents"] = len(ids)
                row["evidence_current_documents"] = len(ids)
                row["evidence_previous_documents"] = 0
                row["evidence_overlap_documents"] = 0

                total += len(
                    row["examples"]
                )

                row.pop("content_ids", None)

            for direction in ("growing", "declining"):
                for row in group["changes"][unit][direction]:

                    old_ids = normalize_content_ids(
                        row.get(
                            "old_content_ids",
                            [],
                        )
                    )

                    new_ids = normalize_content_ids(
                        row.get(
                            "new_content_ids",
                            [],
                        )
                    )

                    old_members = set(old_ids)
                    new_members = set(new_ids)

                    all_ids = normalize_content_ids(
                        new_ids + old_ids
                    )

                    overlap = (
                        old_members
                        & new_members
                    )

                    row["examples"] = prepare_examples(
                        all_ids,
                        metadata,
                        row["term"],
                        row["label"],
                        {
                            "current": new_members,
                            "previous": old_members,
                        },
                    )

                    row["evidence_documents"] = len(
                        all_ids
                    )

                    row["evidence_current_documents"] = len(
                        new_members
                    )

                    row["evidence_previous_documents"] = len(
                        old_members
                    )

                    row["evidence_overlap_documents"] = len(
                        overlap
                    )

                    total += len(
                        row["examples"]
                    )

                    row.pop(
                        "old_content_ids",
                        None,
                    )

                    row.pop(
                        "new_content_ids",
                        None,
                    )

    return total


def validate_example_coverage(data: dict) -> None:
    issues = []

    for group_name, group in data["groups"].items():
        for unit in ("words", "phrases"):

            for row in group["themes"][unit]:
                examples = row.get(
                    "examples",
                    [],
                )

                if (
                    len(examples)
                    != row["documents"]
                ):
                    issues.append(
                        (
                            group_name,
                            "themes_total",
                            unit,
                            row["term"],
                            row["documents"],
                            len(examples),
                        )
                    )

            for direction in ("growing", "declining"):
                for row in group["changes"][unit][direction]:

                    examples = row.get(
                        "examples",
                        [],
                    )

                    unique_count = len(
                        examples
                    )

                    current_count = sum(
                        "current"
                        in example.get(
                            "evidence_slices",
                            [],
                        )
                        for example in examples
                    )

                    previous_count = sum(
                        "previous"
                        in example.get(
                            "evidence_slices",
                            [],
                        )
                        for example in examples
                    )

                    if (
                        unique_count
                        != row[
                            "evidence_documents"
                        ]
                    ):
                        issues.append(
                            (
                                group_name,
                                "unique_total",
                                unit,
                                row["term"],
                                row[
                                    "evidence_documents"
                                ],
                                unique_count,
                            )
                        )

                    if (
                        current_count
                        != row["new_documents"]
                    ):
                        issues.append(
                            (
                                group_name,
                                "current_slice",
                                unit,
                                row["term"],
                                row["new_documents"],
                                current_count,
                            )
                        )

                    if (
                        previous_count
                        != row["old_documents"]
                    ):
                        issues.append(
                            (
                                group_name,
                                "previous_slice",
                                unit,
                                row["term"],
                                row["old_documents"],
                                previous_count,
                            )
                        )

    if issues:
        for issue in issues:
            print(
                "EVIDENCE COVERAGE ERROR:",
                issue,
            )

        raise RuntimeError(
            f"evidence coverage errors: "
            f"{len(issues)}"
        )

    print("evidence_coverage=OK")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    noise = load_noise()
    word_noise = noise | DEMO_GENERIC_UNIGRAMS

    result = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "current": {
                "start": NEW_START,
                "end": NEW_END,
            },
            "previous": {
                "start": OLD_START,
                "end": OLD_END,
            },
            "evidence_mode": EVIDENCE_MODE,
        },
        "groups": {},
    }

    with psycopg.connect(DB_DSN) as conn:
        for group in GROUPS:
            print(f"\n===== {group} =====", flush=True)

            current = process_slice(
                conn,
                group,
                NEW_START,
                NEW_END,
                keep_doc_ids=True,
            )

            previous = process_slice(
                conn,
                group,
                OLD_START,
                OLD_END,
                keep_doc_ids=True,
            )

            result["groups"][group] = {
                "sample": {
                    "current_documents": current["documents"],
                    "previous_documents": previous["documents"],
                },
                "themes": {
                    "words": top_terms(
                        current,
                        "words",
                        word_noise,
                        THEME_WORDS,
                    ),
                    "phrases": top_terms(
                        current,
                        "phrases",
                        noise,
                        THEME_PHRASES,
                    ),
                },
                "changes": {
                    "words": change_terms(
                        previous,
                        current,
                        "words",
                        word_noise,
                    ),
                    "phrases": change_terms(
                        previous,
                        current,
                        "phrases",
                        noise,
                    ),
                },
            }

        candidate_ids = collect_candidate_ids(result)

        print(
            f"\ncandidate_content_ids={len(candidate_ids)}",
            flush=True,
        )

        metadata = build_metadata(
            conn,
            candidate_ids,
        )

    print(f"metadata_rows={len(metadata)}")

    example_slots = attach_examples(
        result,
        metadata,
    )

    validate_example_coverage(result)

    OUTPUT_FILE.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=str,
        ) + "\n",
        encoding="utf-8",
    )

    print(f"saved={OUTPUT_FILE}")
    print(f"bytes={OUTPUT_FILE.stat().st_size}")
    print(f"example_slots={example_slots}")

    for group in GROUPS:
        block = result["groups"][group]

        print(f"\n{group}:")
        print(
            "  themes:"
            f" words={len(block['themes']['words'])}"
            f" phrases={len(block['themes']['phrases'])}"
        )
        print(
            "  changes words:"
            f" +{len(block['changes']['words']['growing'])}"
            f" / -{len(block['changes']['words']['declining'])}"
        )
        print(
            "  changes phrases:"
            f" +{len(block['changes']['phrases']['growing'])}"
            f" / -{len(block['changes']['phrases']['declining'])}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
