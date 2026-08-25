#!/usr/bin/env python3
"""Content Contours v2 — READ-ONLY reference-registry calibration scan."""

from __future__ import annotations

import re
from collections import Counter

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

DB_DSN = "dbname=mip_dev"
EMBEDDING_MODEL_ID = 1

TOP_N = 20
PREVIEW_LEN = 220


def norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def alias_pattern(alias: str) -> re.Pattern:
    a = re.escape(norm_text(alias))
    return re.compile(rf"(?<!\w){a}(?!\w)", re.UNICODE)


def preview(text: str) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= PREVIEW_LEN else t[:PREVIEW_LEN].rstrip() + "…"


def pct(vals: np.ndarray, p: float) -> float:
    return float(np.quantile(vals, p))


def main() -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)

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
                    e.monitoring_contour_id,
                    e.object_id,
                    e.entry_type,
                    e.match_mode,
                    e.facet_code,
                    e.reference_text,
                    o.canonical_name,
                    re.embedding
                FROM contour_reference_entries e
                LEFT JOIN contour_reference_objects o
                  ON o.object_id = e.object_id
                JOIN contour_reference_embeddings re
                  ON re.reference_id = e.reference_id
                 AND re.embedding_model_id = %s
                WHERE e.active
                  AND e.match_mode IN ('semantic', 'both')
                ORDER BY e.reference_id
                """,
                (EMBEDDING_MODEL_ID,),
            )
            refs = cur.fetchall()

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
                SELECT DISTINCT io.content_id
                FROM item_occurrences io
                JOIN sources s ON s.source_id = io.source_id
                WHERE s.contour_id = 4
                """
            )
            source_group_4 = {row[0] for row in cur.fetchall()}

    print(
        f"content_total={len(all_content)} "
        f"semantic_corpus={len(corpus)} "
        f"missing_embeddings={len(all_content) - len(corpus)}"
    )
    print(f"semantic_refs={len(refs)} exact_aliases={len(aliases)}")

    content_vectors = np.vstack(
        [row[3].to_numpy().astype(np.float32) for row in corpus]
    )

    ref_vectors = np.vstack(
        [row[8].to_numpy().astype(np.float32) for row in refs]
    )

    scores_matrix = content_vectors @ ref_vectors.T

    # ------------------------------------------------------------------
    # Reference groups
    # ------------------------------------------------------------------

    c1_obj_idx = [
        i for i, r in enumerate(refs)
        if r[1] == 1 and r[2] is not None
    ]

    c1_facet_idx = [
        i for i, r in enumerate(refs)
        if r[1] == 1 and r[2] is None and r[3] == "facet"
    ]

    contour_idx = {
        contour_id: [
            i for i, r in enumerate(refs)
            if r[1] == contour_id
        ]
        for contour_id in (2, 3, 4)
    }

    # ------------------------------------------------------------------
    # Exact object anchors
    # ------------------------------------------------------------------

    compiled_aliases = [
        (reference_id, object_id, alias, canonical_name, alias_pattern(alias))
        for reference_id, object_id, alias, canonical_name in aliases
    ]

    def find_exact_hits(rows):
        exact_hits_local: dict[int, list[tuple]] = {}
        counter = Counter()

        for row_idx, row in enumerate(rows):
            content_id, title, text_content = row[:3]
            text = norm_text((title or "") + " " + text_content)
            hits = []

            for reference_id, object_id, alias, canonical_name, pattern in compiled_aliases:
                if pattern.search(text):
                    hits.append((object_id, canonical_name, alias))

            if hits:
                dedup = {}
                for object_id, canonical_name, alias in hits:
                    dedup.setdefault(object_id, (canonical_name, []))
                    dedup[object_id][1].append(alias)

                exact_hits_local[row_idx] = [
                    (object_id, canonical_name, sorted(set(hit_aliases)))
                    for object_id, (canonical_name, hit_aliases) in dedup.items()
                ]

                for object_id, (canonical_name, hit_aliases) in dedup.items():
                    counter[canonical_name] += 1

        return exact_hits_local, counter

    exact_hits_all, object_hit_counter = find_exact_hits(all_content)
    exact_hits, _ = find_exact_hits(corpus)

    all_anchor_ids = {
        all_content[row_idx][0]
        for row_idx in exact_hits_all
    }
    semantic_anchor_ids = {
        corpus[row_idx][0]
        for row_idx in exact_hits
    }

    print("\n=== C1 EXACT OBJECT ANCHORS ===")
    print(f"unique_content={len(exact_hits_all)}")
    print(
        f"without_embedding="
        f"{len(all_anchor_ids - semantic_anchor_ids)}"
    )
    for name, count in object_hit_counter.most_common():
        print(f"{count:4d}  {name}")

    print("\n=== C1 EXACT-ANCHORED CONTENT + BEST FACET ===")

    anchored_rows = []
    for row_idx, hits in exact_hits.items():
        facet_scores = scores_matrix[row_idx, c1_facet_idx]
        best_local = int(np.argmax(facet_scores))
        best_idx = c1_facet_idx[best_local]
        best_ref = refs[best_idx]
        facet_score = float(facet_scores[best_local])

        anchored_rows.append((facet_score, row_idx, hits, best_ref))

    anchored_rows.sort(reverse=True, key=lambda x: x[0])

    for facet_score, row_idx, hits, best_ref in anchored_rows[:30]:
        content_id, title, text_content, _ = corpus[row_idx]
        objects = "; ".join(
            f"{name} [{', '.join(hit_aliases)}]"
            for _, name, hit_aliases in hits
        )
        print(
            f"\n[{content_id}] facet={best_ref[5]} score={facet_score:.3f}"
        )
        print(f"objects: {objects}")
        print(f"title: {title or '(no title)'}")
        print(f"    {preview(text_content)}")

    # ------------------------------------------------------------------
    # C1 semantic object candidates WITHOUT exact anchor
    # ------------------------------------------------------------------

    c1_obj_scores = scores_matrix[:, c1_obj_idx]
    c1_best_local = np.argmax(c1_obj_scores, axis=1)
    c1_best_scores = c1_obj_scores[
        np.arange(len(corpus)),
        c1_best_local
    ]

    candidates = []
    for row_idx in range(len(corpus)):
        if row_idx in exact_hits:
            continue

        best_idx = c1_obj_idx[int(c1_best_local[row_idx])]
        candidates.append(
            (
                float(c1_best_scores[row_idx]),
                row_idx,
                refs[best_idx],
            )
        )

    candidates.sort(reverse=True, key=lambda x: x[0])

    print("\n=== C1 TOP SEMANTIC OBJECT CANDIDATES WITHOUT EXACT ANCHOR ===")
    print("DIAGNOSTIC ONLY — semantic similarity is NOT an object confirmation.")

    for score, row_idx, ref in candidates[:30]:
        content_id, title, text_content, _ = corpus[row_idx]
        print(
            f"\n[{content_id}] score={score:.3f} "
            f"candidate_object={ref[7]}"
        )
        print(f"reference={ref[6]}")
        print(f"title: {title or '(no title)'}")
        print(f"    {preview(text_content)}")

    # ------------------------------------------------------------------
    # Semantic distributions + TOP-N for contours 2-4
    # ------------------------------------------------------------------

    for contour_id in (2, 3, 4):
        idxs = contour_idx[contour_id]
        sub = scores_matrix[:, idxs]

        best_local = np.argmax(sub, axis=1)
        best_scores = sub[np.arange(len(corpus)), best_local]

        print(f"\n=== CONTOUR {contour_id} SCORE DISTRIBUTION ===")
        print(
            f"min={best_scores.min():.3f} "
            f"p25={pct(best_scores, 0.25):.3f} "
            f"median={pct(best_scores, 0.50):.3f} "
            f"p75={pct(best_scores, 0.75):.3f} "
            f"p90={pct(best_scores, 0.90):.3f} "
            f"max={best_scores.max():.3f}"
        )

        ranked = np.argsort(best_scores)[::-1][:TOP_N]

        print(f"\n=== CONTOUR {contour_id} TOP-{TOP_N} ===")

        for row_idx in ranked:
            ref_idx = idxs[int(best_local[row_idx])]
            ref = refs[ref_idx]
            content_id, title, text_content, _ = corpus[row_idx]

            source_flag = (
                " source_group_4"
                if contour_id == 4 and content_id in source_group_4
                else ""
            )

            print(
                f"\n[{content_id}] score={best_scores[row_idx]:.3f}"
                f"{source_flag} facet={ref[5]}"
            )
            print(f"reference={ref[6]}")
            print(f"title: {title or '(no title)'}")
            print(f"    {preview(text_content)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
