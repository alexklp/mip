#!/usr/bin/env python3
"""
C1 recall / alias-gap calibration experiment.

READ ONLY.

Цель:
- исключить уже CONFIRMED exact-anchor content;
- найти потенциально пропущенные упоминания объектов C1;
- semantic/lexical/structural сигналы используются ТОЛЬКО для candidate generation;
- никакой semantic score сам по себе не подтверждает объект.

Reference objects/aliases/embeddings читаются непосредственно из
Content Contours v2 registry в PostgreSQL.
"""

from __future__ import annotations

import re

import numpy as np
import psycopg
from pgvector.psycopg import register_vector


DB_DSN = "dbname=mip_dev"
EMBEDDING_MODEL_ID = 1

TOP_SEMANTIC_CANDIDATES = 120
MAX_REVIEW_OUT = 100
PREVIEW_LEN = 240


def norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def alias_pattern(alias: str) -> re.Pattern:
    a = re.escape(norm_text(alias))
    return re.compile(rf"(?<!\w){a}(?!\w)", re.UNICODE)


def preview(text: str) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= PREVIEW_LEN else t[:PREVIEW_LEN].rstrip() + "…"


LEXICAL_PATTERNS = [
    ("dshv", re.compile(r"(?<!\w)дшв(?!\w)", re.UNICODE)),
    ("air_assault", re.compile(r"десантно[-\s]?штурмов", re.UNICODE)),
    ("airmobile_ua", re.compile(r"аеромобільн", re.UNICODE)),
    ("airmobile_ru", re.compile(r"аэромобильн", re.UNICODE)),
    ("odshbr", re.compile(r"(?<!\w)одшбр(?!\w)", re.UNICODE)),
    ("oaembr", re.compile(r"(?<!\w)(?:оаембр|оаэмбр)(?!\w)", re.UNICODE)),
]

UNIT_TERM = (
    r"(?:"
    r"бригад\w*|"
    r"батальйон\w*|"
    r"батальон\w*|"
    r"полк\w*|"
    r"корпус\w*|"
    r"одшбр|"
    r"оаембр|"
    r"оаэмбр"
    r")"
)


def load_data(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ci.content_id, ci.title, ci.text_content
            FROM content_items ci
            ORDER BY ci.content_id
            """
        )
        all_content = cur.fetchall()

        cur.execute(
            """
            SELECT ci.content_id, ci.title, ci.text_content, e.embedding
            FROM content_items ci
            JOIN embeddings e
              ON e.content_id = ci.content_id
             AND e.embedding_model_id = %s
            ORDER BY ci.content_id
            """,
            (EMBEDDING_MODEL_ID,),
        )
        corpus = cur.fetchall()

        cur.execute(
            """
            SELECT
                e.reference_id,
                e.object_id,
                e.reference_text,
                o.canonical_name
            FROM contour_reference_entries e
            JOIN contour_reference_objects o
              ON o.object_id = e.object_id
            WHERE e.monitoring_contour_id = 1
              AND e.entry_type = 'alias'
              AND e.active
              AND e.match_mode IN ('exact', 'both')
            ORDER BY e.reference_id
            """
        )
        aliases = cur.fetchall()

        cur.execute(
            """
            SELECT
                e.reference_id,
                e.object_id,
                e.reference_text,
                o.canonical_name,
                re.embedding
            FROM contour_reference_entries e
            JOIN contour_reference_objects o
              ON o.object_id = e.object_id
            JOIN contour_reference_embeddings re
              ON re.reference_id = e.reference_id
             AND re.embedding_model_id = %s
            WHERE e.monitoring_contour_id = 1
              AND e.object_id IS NOT NULL
              AND e.active
              AND e.match_mode IN ('semantic', 'both')
            ORDER BY e.reference_id
            """,
            (EMBEDDING_MODEL_ID,),
        )
        object_refs = cur.fetchall()

    return all_content, corpus, aliases, object_refs


def build_exact_ids(all_content, aliases):
    compiled = [
        (
            reference_id,
            object_id,
            alias,
            canonical_name,
            alias_pattern(alias),
        )
        for reference_id, object_id, alias, canonical_name in aliases
    ]

    exact_ids = set()

    for content_id, title, text_content in all_content:
        text = norm_text((title or "") + " " + (text_content or ""))

        for _, _, _, _, pattern in compiled:
            if pattern.search(text):
                exact_ids.add(content_id)
                break

    return exact_ids


def build_lexical_hits(all_content):
    hits = {}

    for content_id, title, text_content in all_content:
        text = norm_text((title or "") + " " + (text_content or ""))

        matched = [
            label
            for label, pattern in LEXICAL_PATTERNS
            if pattern.search(text)
        ]

        if matched:
            hits[content_id] = matched

    return hits


def build_structural_hits(all_content, object_refs):
    """
    Diagnostic signal:
    номер з canonical object name + nearby military-unit term.

    Це НЕ object confirmation.
    Воно навмисно може ловити однойменні/одномерні російські частини:
    саме такі випадки нам і треба побачити в recall-gap review.
    """
    object_numbers = {}

    for _, object_id, _, canonical_name, _ in object_refs:
        m = re.match(r"^\s*(\d{1,4})\b", canonical_name or "")
        if not m:
            continue

        number = m.group(1)
        object_numbers.setdefault(
            (object_id, canonical_name, number),
            None,
        )

    compiled = []

    for object_id, canonical_name, number in object_numbers:
        n = re.escape(number)

        pattern = re.compile(
            rf"(?:"
            rf"(?<!\w){n}(?!\w).{{0,80}}{UNIT_TERM}"
            rf"|"
            rf"{UNIT_TERM}.{{0,80}}(?<!\w){n}(?!\w)"
            rf")",
            re.UNICODE,
        )

        compiled.append(
            (object_id, canonical_name, number, pattern)
        )

    hits = {}

    for content_id, title, text_content in all_content:
        text = norm_text((title or "") + " " + (text_content or ""))

        matched = []

        for object_id, canonical_name, number, pattern in compiled:
            if pattern.search(text):
                matched.append(
                    (object_id, canonical_name, number)
                )

        if matched:
            hits[content_id] = matched

    return hits


def build_semantic_hits(corpus, object_refs):
    if not corpus:
        return {}

    if not object_refs:
        raise RuntimeError("C1 semantic object references not found")

    content_vectors = np.vstack(
        [
            row[3].to_numpy().astype(np.float32)
            for row in corpus
        ]
    )

    ref_vectors = np.vstack(
        [
            row[4].to_numpy().astype(np.float32)
            for row in object_refs
        ]
    )

    scores = content_vectors @ ref_vectors.T

    results = {}

    for row_idx, row in enumerate(corpus):
        content_id = row[0]

        best_idx = int(np.argmax(scores[row_idx]))
        best_score = float(scores[row_idx, best_idx])

        ref = object_refs[best_idx]

        results[content_id] = {
            "reference_id": ref[0],
            "object_id": ref[1],
            "reference_text": ref[2],
            "canonical_name": ref[3],
            "score": best_score,
        }

    return results


def main() -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)

        all_content, corpus, aliases, object_refs = load_data(conn)

    print(
        f"content_total={len(all_content)} "
        f"semantic_corpus={len(corpus)} "
        f"missing_embeddings={len(all_content) - len(corpus)}"
    )
    print(
        f"C1 exact_aliases={len(aliases)} "
        f"semantic_object_refs={len(object_refs)}"
    )

    exact_ids = build_exact_ids(all_content, aliases)
    lexical_hits = build_lexical_hits(all_content)
    structural_hits = build_structural_hits(
        all_content,
        object_refs,
    )
    semantic_hits = build_semantic_hits(
        corpus,
        object_refs,
    )

    print(f"exact CONFIRMED pool={len(exact_ids)}")

    semantic_ranked = sorted(
        (
            (data["score"], content_id)
            for content_id, data in semantic_hits.items()
            if content_id not in exact_ids
        ),
        reverse=True,
    )

    semantic_top_ids = {
        content_id
        for _, content_id
        in semantic_ranked[:TOP_SEMANTIC_CANDIDATES]
    }

    candidate_ids = (
        set(lexical_hits)
        | set(structural_hits)
        | semantic_top_ids
    ) - exact_ids

    all_by_id = {
        row[0]: (row[1], row[2])
        for row in all_content
    }

    def signal_count(content_id):
        return (
            int(content_id in lexical_hits)
            + int(content_id in structural_hits)
            + int(content_id in semantic_top_ids)
        )

    ranked = sorted(
        candidate_ids,
        key=lambda cid: (
            signal_count(cid),
            semantic_hits.get(cid, {}).get("score", -1.0),
        ),
        reverse=True,
    )

    print()
    print("=== C1 RECALL / ALIAS-GAP CANDIDATE POOL ===")
    print("DIAGNOSTIC ONLY. NONE of these signals confirms C1.")
    print(f"candidate_union={len(candidate_ids)}")
    print(
        f"lexical_nonexact="
        f"{len(set(lexical_hits) - exact_ids)}"
    )
    print(
        f"structural_nonexact="
        f"{len(set(structural_hits) - exact_ids)}"
    )
    print(
        f"semantic_top_nonexact="
        f"{len(semantic_top_ids)}"
    )

    print()
    print(
        f"=== REVIEW SAMPLE TOP "
        f"{min(MAX_REVIEW_OUT, len(ranked))} ==="
    )

    for content_id in ranked[:MAX_REVIEW_OUT]:
        title, text_content = all_by_id[content_id]

        sem = semantic_hits.get(content_id)
        lex = lexical_hits.get(content_id, [])
        structural = structural_hits.get(content_id, [])

        print()
        print(
            f"[{content_id}] "
            f"signals={signal_count(content_id)}"
        )

        if sem:
            print(
                f"semantic: score={sem['score']:.3f} "
                f"candidate_object={sem['canonical_name']}"
            )
            print(
                f"semantic_reference={sem['reference_text']}"
            )
        else:
            print("semantic: no content embedding")

        print(
            "lexical: "
            + (", ".join(lex) if lex else "-")
        )

        if structural:
            print(
                "structural: "
                + "; ".join(
                    f"{canonical_name} [number+unit-term]"
                    for _, canonical_name, _ in structural
                )
            )
        else:
            print("structural: -")

        print(f"title: {title or '(no title)'}")
        print(f"    {preview(text_content)}")

    print()
    print("=== SUMMARY ===")
    print(f"exact_confirmed={len(exact_ids)}")
    print(f"review_candidates={len(candidate_ids)}")
    print(
        "Manual review target: real C1 mentions missed by "
        "the exact alias registry vs diagnostic noise."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
