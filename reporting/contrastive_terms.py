#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
from collections import Counter

import psycopg

from nlp_prepare import (
    DB_DSN,
    clean_text,
    document_terms,
    fetch_documents,
    load_rules,
    load_stopwords,
    tokenize,
)


GROUPS = ("ua_space", "ru_space")


def log_odds(a: int, n_a: int, b: int, n_b: int) -> float:
    # Haldane-Anscombe smoothing for stable finite odds.
    pa = (a + 0.5) / (n_a + 1.0)
    pb = (b + 0.5) / (n_b + 1.0)

    return math.log(pa / (1.0 - pa)) - math.log(pb / (1.0 - pb))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Contrastive document-frequency terms for MIP source groups"
    )
    parser.add_argument("--ngram", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--min-docs", type=int, default=20)
    parser.add_argument(
        "--since-hours",
        type=int,
        default=None,
        help="MIP first_seen_at window; observation time, not origin/publication time",
    )
    parser.add_argument(
        "--lemmatize",
        action="store_true",
        help="detect document language and use RU/UA Stanza lemmas; other languages keep surface tokens",
    )
    parser.add_argument(
        "--start-at",
        default=None,
        help="fixed MIP first_seen_at lower bound, inclusive",
    )
    parser.add_argument(
        "--end-at",
        default=None,
        help="fixed MIP first_seen_at upper bound, exclusive",
    )
    args = parser.parse_args()

    if args.since_hours is not None and (args.start_at or args.end_at):
        parser.error("--since-hours cannot be combined with --start-at/--end-at")

    stopwords = load_stopwords()
    rules = load_rules()

    if args.lemmatize:
        from morphology import detect_language, normalized_tokens

    df = {group: Counter() for group in GROUPS}
    doc_counts = Counter()

    with psycopg.connect(DB_DSN) as conn:
        rows = fetch_documents(
            conn,
            source_group=None,
            since_hours=args.since_hours,
            start_at=args.start_at,
            end_at=args.end_at,
        )

    for content_id, source_group, text, source_names in rows:
        if source_group not in GROUPS:
            continue

        cleaned = clean_text(
            text,
            list(source_names or []),
            rules,
        )
        if args.lemmatize:
            lang = detect_language(cleaned)

            if lang in ("uk", "ru"):
                tokens = normalized_tokens(
                    cleaned,
                    lang,
                    stopwords,
                )
            else:
                tokens = tokenize(cleaned)
        else:
            tokens = tokenize(cleaned)

        terms = document_terms(
            tokens,
            args.ngram,
            stopwords,
        )

        doc_counts[source_group] += 1
        df[source_group].update(terms.keys())

    all_terms = set(df["ua_space"]) | set(df["ru_space"])

    rows_out = []
    for term in all_terms:
        ua = df["ua_space"][term]
        ru = df["ru_space"][term]

        if ua + ru < args.min_docs:
            continue

        score = log_odds(
            ua,
            doc_counts["ua_space"],
            ru,
            doc_counts["ru_space"],
        )

        rows_out.append(
            (
                term,
                score,
                ua,
                ru,
                100.0 * ua / doc_counts["ua_space"],
                100.0 * ru / doc_counts["ru_space"],
            )
        )

    print(
        f"documents: ua_space={doc_counts['ua_space']} "
        f"ru_space={doc_counts['ru_space']} "
        f"ngram={args.ngram} "
        f"min_docs={args.min_docs} "
        f"lemmatize={args.lemmatize} "
        f"since_hours={args.since_hours} "
        f"start_at={args.start_at} "
        f"end_at={args.end_at}"
    )

    print("\n===== DISTINCTIVE UA_SPACE =====")
    print(
        f"{'term':40} {'score':>8} "
        f"{'ua_docs':>8} {'ru_docs':>8} "
        f"{'ua_pct':>8} {'ru_pct':>8}"
    )

    for term, score, ua, ru, ua_pct, ru_pct in sorted(
        rows_out,
        key=lambda x: (-x[1], -x[2], x[0]),
    )[: args.top]:
        print(
            f"{term[:40]:40} {score:8.3f} "
            f"{ua:8d} {ru:8d} "
            f"{ua_pct:7.2f}% {ru_pct:7.2f}%"
        )

    print("\n===== DISTINCTIVE RU_SPACE =====")
    print(
        f"{'term':40} {'score':>8} "
        f"{'ru_docs':>8} {'ua_docs':>8} "
        f"{'ru_pct':>8} {'ua_pct':>8}"
    )

    for term, score, ua, ru, ua_pct, ru_pct in sorted(
        rows_out,
        key=lambda x: (x[1], -x[3], x[0]),
    )[: args.top]:
        print(
            f"{term[:40]:40} {-score:8.3f} "
            f"{ru:8d} {ua:8d} "
            f"{ru_pct:7.2f}% {ua_pct:7.2f}%"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
