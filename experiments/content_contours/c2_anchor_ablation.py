#!/usr/bin/env python3

import re
import numpy as np
import psycopg
from pgvector.psycopg import register_vector

DB_DSN = "dbname=mip_dev"
MODEL_ID = 1
TOP_N = 100
SHOW_N = 15

RULES = {
    "ramstein": [
        re.compile(r"\bрамштайн\w*\b", re.I),
        re.compile(r"\bramstein\b", re.I),
        re.compile(r"контактн\w+\s+груп\w+.{0,60}оборон\w+.{0,60}україн", re.I),
        re.compile(r"ukraine defense contact group", re.I),
    ],

    "sanctions": [
        re.compile(
            r"(санкц\w+|sanction\w*).{0,160}"
            r"(росі\w+|росс\w+|\bрф\b|кремл\w+|russia\w*|russian\w*)",
            re.I,
        ),
        re.compile(
            r"(росі\w+|росс\w+|\bрф\b|кремл\w+|russia\w*|russian\w*).{0,160}"
            r"(санкц\w+|sanction\w*)",
            re.I,
        ),
    ],

    "military_aid": [
        re.compile(
            r"(україн\w+|украин\w+|\bзсу\b|\bвсу\b|kyiv|kiev|ukrain\w*).{0,200}"
            r"(військов\w+\s+допомог\w+|военн\w+\s+помощ\w+|military aid|"
            r"постав\w+|передач\w+|партнер\w+|озброєн\w+|вооружен\w+|ammunition|weapons)",
            re.I,
        ),
        re.compile(
            r"(військов\w+\s+допомог\w+|военн\w+\s+помощ\w+|military aid|"
            r"постав\w+|передач\w+|партнер\w+|озброєн\w+|вооружен\w+|ammunition|weapons).{0,200}"
            r"(україн\w+|украин\w+|\bзсу\b|\bвсу\b|kyiv|kiev|ukrain\w*)",
            re.I,
        ),
    ],

    "nato_statements": [
        re.compile(
            r"(нато|nato).{0,180}"
            r"(заяв\w+|сказ\w+|повідом\w+|підтверд\w+|запевн\w+|statement|said|confirmed|assured)",
            re.I,
        ),
        re.compile(
            r"(заяв\w+|сказ\w+|повідом\w+|підтверд\w+|запевн\w+|statement|said|confirmed|assured).{0,180}"
            r"(нато|nato)",
            re.I,
        ),
    ],

    "defence_industry": [
        re.compile(
            r"(впк|оборонн\w+\s+промисл\w+|военн\w+\s+промышлен\w+|"
            r"defen[cs]e industry|military industr\w+|"
            r"оборонн\w+\s+(?:завод|підприємств)|"
            r"военн\w+\s+(?:завод|предприяти)|"
            r"виробництв\w+.{0,80}(ракет|дрон|боєприпас|збро)|"
            r"производств\w+.{0,80}(ракет|дрон|боеприпас|оруж))",
            re.I,
        ),
    ],
}


def norm(title, text):
    return " ".join(((title or "") + " " + (text or "")).split())


def preview(text, n=240):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rstrip() + "…"


with psycopg.connect(DB_DSN) as conn:
    register_vector(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT ci.content_id, ci.title, ci.text_content, e.embedding
            FROM content_items ci
            JOIN embeddings e
              ON e.content_id = ci.content_id
             AND e.embedding_model_id = %s
            ORDER BY ci.content_id
        """, (MODEL_ID,))
        corpus = cur.fetchall()

        cur.execute("""
            SELECT e.facet_code, e.reference_text, re.embedding
            FROM contour_reference_entries e
            JOIN contour_reference_embeddings re
              ON re.reference_id = e.reference_id
             AND re.embedding_model_id = %s
            WHERE e.monitoring_contour_id = 2
              AND e.entry_type = 'semantic_prototype'
              AND e.active
              AND e.match_mode IN ('semantic', 'both')
            ORDER BY e.facet_code
        """, (MODEL_ID,))
        refs = cur.fetchall()

vectors = np.vstack([
    r[3].to_numpy().astype(np.float32)
    for r in corpus
])

for facet, reference, emb in refs:
    scores = vectors @ emb.to_numpy().astype(np.float32)
    idxs = np.argsort(scores)[::-1][:TOP_N]

    passed = []
    failed = []

    for i in idxs:
        cid, title, text, _ = corpus[int(i)]
        full = norm(title, text)

        hit = any(p.search(full) for p in RULES[facet])

        row = (
            str(cid),
            float(scores[i]),
            title,
            text,
        )

        (passed if hit else failed).append(row)

    print(f"\n{'='*70}")
    print(f"C2 FACET: {facet}")
    print(f"reference: {reference}")
    print(
        f"semantic_top={TOP_N} "
        f"anchor_pass={len(passed)} "
        f"anchor_fail={len(failed)}"
    )

    print(f"\n--- ANCHOR PASS TOP {min(SHOW_N, len(passed))} ---")
    for cid, score, title, text in passed[:SHOW_N]:
        print(f"\n[{cid}] score={score:.3f}")
        print(f"title: {title or '(no title)'}")
        print(f"    {preview(text)}")

    print(f"\n--- ANCHOR FAIL TOP {min(SHOW_N, len(failed))} ---")
    for cid, score, title, text in failed[:SHOW_N]:
        print(f"\n[{cid}] score={score:.3f}")
        print(f"title: {title or '(no title)'}")
        print(f"    {preview(text)}")
