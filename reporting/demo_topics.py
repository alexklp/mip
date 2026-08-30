#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from morphology import detect_language, lemmatize_words, normalized_tokens
from nlp_prepare import (
    DB_DSN,
    clean_text,
    document_terms,
    fetch_documents,
    load_rules,
    load_stopwords,
    tokenize,
)
from temporal_shift import log_odds


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"
OUTPUT_FILE = OUTPUT_DIR / "demo_topics.json"
NOISE_FILE = HERE / "wordcloud_noise_v1.txt"

OLD_START = "2026-08-22T00:00:00+00:00"
OLD_END   = "2026-08-23T00:00:00+00:00"
NEW_START = "2026-08-26T07:00:00+00:00"
NEW_END   = "2026-08-26T08:00:00+00:00"

GROUPS = ("ua_space", "ru_space")

TOP_UNIGRAMS = 80
TOP_SHIFT = 15
SHIFT_MIN_DOCS = 3

DEMO_GENERIC_UNIGRAMS = {
    "час", "день", "рік", "год",
    "людина", "человек",
    "область",
    "глава",
    "країна", "страна",
    "держава",
    "росія", "россия",
    "україна", "украина",
    "російський", "российский",
    "український", "украинский",
    "військовий", "военный",
    "військовослужбовець", "военнослужащий",
    "рф",
    "повідомити", "сообщить",
    "заявити", "заявить",
    "провести",
    "питання", "вопрос",
}


def load_noise() -> set[str]:
    return {
        line.strip().lower()
        for line in NOISE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def visible_term(term: str, noise: set[str]) -> bool:
    parts = term.lower().split()

    if any(
        part.replace(",", "").replace(".", "").replace("%", "").isdigit()
        for part in parts
    ):
        return False

    if any(part in noise for part in parts):
        return False

    return True


def analysis_tokens_with_surfaces(
    text: str,
    lang: str,
    stopwords: set[str],
) -> tuple[list[str], list[str]]:
    if lang not in ("uk", "ru"):
        tokens = tokenize(text)
        return tokens, tokens

    normalized = []
    surfaces = []

    for surface, lemma, pos in lemmatize_words(text, lang):
        if pos == "PUNCT":
            continue

        normalized.append(
            surface if surface in stopwords else lemma
        )
        surfaces.append(surface)

    return normalized, surfaces


def process_slice(
    conn,
    source_group: str,
    start_at: str,
    end_at: str,
    *,
    keep_doc_ids: bool,
) -> dict:
    stopwords = load_stopwords()
    rules = load_rules()

    rows = fetch_documents(
        conn,
        source_group=source_group,
        since_hours=None,
        start_at=start_at,
        end_at=end_at,
    )

    # Корпус для аналітичної звітності:
    # лише високовпевнена гілка "analyze" з routing v1.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ci.content_id::text
            FROM content_items ci
            JOIN content_routing_decisions crd
              ON crd.content_id = ci.content_id
             AND crd.routing_version = 1
             AND crd.decision = 'analyze'
            JOIN item_occurrences io
              ON io.content_id = ci.content_id
            JOIN sources src
              ON src.source_id = io.source_id
            WHERE src.source_group = %s
              AND ci.first_seen_at >= %s::timestamptz
              AND ci.first_seen_at <  %s::timestamptz
            """,
            (
                source_group,
                start_at,
                end_at,
            ),
        )

        analyze_ids = {
            row[0]
            for row in cur.fetchall()
        }

    observed_count = len(rows)

    rows = [
        row
        for row in rows
        if str(row[0]) in analyze_ids
    ]

    print(
        f"{source_group} "
        f"{start_at[:10]} "
        f"analyze_selected={len(rows)}/{observed_count}",
        flush=True,
    )

    unigram_df = Counter()
    bigram_df = Counter()

    unigram_labels: dict[str, Counter] = defaultdict(Counter)
    bigram_labels: dict[str, Counter] = defaultdict(Counter)

    unigram_docs: dict[str, list[str]] = defaultdict(list)
    bigram_docs: dict[str, list[str]] = defaultdict(list)

    total = len(rows)

    for i, (content_id, group, text, source_names) in enumerate(rows, 1):
        cleaned = clean_text(
            text,
            list(source_names or []),
            rules,
        )

        lang = detect_language(cleaned)

        tokens, surfaces = analysis_tokens_with_surfaces(
            cleaned,
            lang,
            stopwords,
        )

        unigrams = document_terms(
            tokens,
            ngram=1,
            stopwords=stopwords,
        )

        bigrams = document_terms(
            tokens,
            ngram=2,
            stopwords=stopwords,
        )

        unigram_df.update(unigrams.keys())
        bigram_df.update(bigrams.keys())

        for token, surface in zip(tokens, surfaces):
            if token in unigrams:
                unigram_labels[token][surface] += 1

        for j in range(len(tokens) - 1):
            term = f"{tokens[j]} {tokens[j + 1]}"
            if term in bigrams:
                label = f"{surfaces[j]} {surfaces[j + 1]}"
                bigram_labels[term][label] += 1

        if keep_doc_ids:
            cid = str(content_id)

            for term in unigrams:
                unigram_docs[term].append(cid)

            for term in bigrams:
                bigram_docs[term].append(cid)

        if i % 100 == 0 or i == total:
            print(
                f"{source_group} "
                f"{start_at[:10]} "
                f"processed={i}/{total}",
                flush=True,
            )

    return {
        "documents": total,
        "unigram_df": unigram_df,
        "bigram_df": bigram_df,
        "unigram_labels": unigram_labels,
        "bigram_labels": bigram_labels,
        "unigram_docs": unigram_docs,
        "bigram_docs": bigram_docs,
    }


def top_unigrams(
    data: dict,
    noise: set[str],
) -> list[dict]:
    n = data["documents"]
    df = data["unigram_df"]

    terms = [
        term
        for term, docs in df.items()
        if visible_term(term, noise)
    ]

    terms.sort(
        key=lambda term: (
            -df[term],
            term,
        )
    )

    result = []

    for term in terms[:TOP_UNIGRAMS]:
        docs = df[term]

        result.append(
            {
                "term": term,
                "label": term,
                "documents": docs,
                "doc_pct": round(100.0 * docs / n, 3) if n else 0.0,
                "content_ids": data["unigram_docs"][term],
            }
        )

    return result


def temporal_shift(
    old: dict,
    new: dict,
    noise: set[str],
) -> dict:
    old_n = old["documents"]
    new_n = new["documents"]

    old_df = old["bigram_df"]
    new_df = new["bigram_df"]

    rows = []

    for term in set(old_df) | set(new_df):
        old_docs = old_df[term]
        new_docs = new_df[term]

        if old_docs + new_docs < SHIFT_MIN_DOCS:
            continue

        if not visible_term(term, noise):
            continue

        old_pct = 100.0 * old_docs / old_n if old_n else 0.0
        new_pct = 100.0 * new_docs / new_n if new_n else 0.0
        delta = new_pct - old_pct

        score = log_odds(
            new_docs,
            new_n,
            old_docs,
            old_n,
        )

        labels = (
            new["bigram_labels"].get(term)
            or old["bigram_labels"].get(term)
        )
        label = (
            labels.most_common(1)[0][0]
            if labels
            else term
        )

        rows.append(
            {
                "term": term,
                "label": label,
                "score": round(score, 6),
                "old_documents": old_docs,
                "new_documents": new_docs,
                "old_pct": round(old_pct, 3),
                "new_pct": round(new_pct, 3),
                "delta_pct": round(delta, 3),
                "new_content_ids": new["bigram_docs"].get(term, []),
            }
        )

    emerging = sorted(
        rows,
        key=lambda row: (
            -row["score"],
            -row["delta_pct"],
            -row["new_documents"],
            row["term"],
        ),
    )[:TOP_SHIFT]

    declining = sorted(
        rows,
        key=lambda row: (
            row["score"],
            row["delta_pct"],
            -row["old_documents"],
            row["term"],
        ),
    )[:TOP_SHIFT]

    return {
        "old_documents": old_n,
        "new_documents": new_n,
        "emerging": emerging,
        "declining": declining,
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    noise = load_noise()
    unigram_noise = noise | DEMO_GENERIC_UNIGRAMS

    result = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "git_baseline": "8991054",
            "old_window": {
                "start": OLD_START,
                "end": OLD_END,
            },
            "new_window": {
                "start": NEW_START,
                "end": NEW_END,
            },
        },
        "groups": {},
    }

    with psycopg.connect(DB_DSN) as conn:
        for group in GROUPS:
            print(f"\n===== {group}: CURRENT =====", flush=True)

            current = process_slice(
                conn,
                group,
                NEW_START,
                NEW_END,
                keep_doc_ids=True,
            )

            print(f"\n===== {group}: PREVIOUS =====", flush=True)

            previous = process_slice(
                conn,
                group,
                OLD_START,
                OLD_END,
                keep_doc_ids=False,
            )

            result["groups"][group] = {
                "current_documents": current["documents"],
                "top_unigrams": top_unigrams(
                    current,
                    unigram_noise,
                ),
                "bigram_shift": temporal_shift(
                    previous,
                    current,
                    noise,
                ),
            }

    OUTPUT_FILE.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print(f"\nsaved={OUTPUT_FILE}")
    print(f"bytes={OUTPUT_FILE.stat().st_size}")

    for group in GROUPS:
        block = result["groups"][group]

        print(f"\n===== {group} TOP THEMES =====")
        for row in block["top_unigrams"][:10]:
            print(
                f"{row.get('label', row['term']):24} "
                f"{row['documents']:4d} "
                f"{row['doc_pct']:6.2f}%"
            )

        print(f"\n===== {group} EMERGING BIGRAMS =====")
        for row in block["bigram_shift"]["emerging"][:10]:
            print(
                f"{row.get('label', row['term']):30} "
                f"{row['old_pct']:6.2f}% -> "
                f"{row['new_pct']:6.2f}% "
                f"({row['delta_pct']:+6.2f})"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
