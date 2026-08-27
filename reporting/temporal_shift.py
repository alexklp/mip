#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
from collections import Counter
from pathlib import Path

import psycopg

from morphology import detect_language, normalized_tokens
from nlp_prepare import (
    DB_DSN,
    clean_text,
    document_terms,
    fetch_documents,
    load_rules,
    load_stopwords,
    tokenize,
)


def log_odds(new_df: int, new_n: int, old_df: int, old_n: int) -> float:
    # Haldane-Anscombe smoothing.
    p_new = (new_df + 0.5) / (new_n + 1.0)
    p_old = (old_df + 0.5) / (old_n + 1.0)

    return (
        math.log(p_new / (1.0 - p_new))
        - math.log(p_old / (1.0 - p_old))
    )


def collect_df(
    conn,
    source_group: str,
    start_at: str,
    end_at: str,
    ngram: int,
    stopwords: set[str],
    rules: list[dict],
) -> tuple[Counter, int]:
    rows = fetch_documents(
        conn,
        source_group=source_group,
        since_hours=None,
        start_at=start_at,
        end_at=end_at,
    )

    df = Counter()

    for content_id, group, text, source_names in rows:
        cleaned = clean_text(
            text,
            list(source_names or []),
            rules,
        )

        lang = detect_language(cleaned)

        if lang in ("uk", "ru"):
            tokens = normalized_tokens(
                cleaned,
                lang,
                stopwords,
            )
        else:
            tokens = tokenize(cleaned)

        terms = document_terms(
            tokens,
            ngram,
            stopwords,
        )

        df.update(terms.keys())

    return df, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Temporal document-frequency shift between two observed MIP slices"
    )
    parser.add_argument(
        "--source-group",
        required=True,
        choices=("ua_space", "ru_space"),
    )
    parser.add_argument("--old-start", required=True)
    parser.add_argument("--old-end", required=True)
    parser.add_argument("--new-start", required=True)
    parser.add_argument("--new-end", required=True)
    parser.add_argument(
        "--ngram",
        type=int,
        choices=(1, 2, 3),
        default=1,
    )
    parser.add_argument("--min-docs", type=int, default=10)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument(
        "--filter-noise",
        action="store_true",
        help="remove reporting-only visual noise and standalone numeric terms",
    )
    args = parser.parse_args()

    stopwords = load_stopwords()
    rules = load_rules()

    noise = set()
    if args.filter_noise:
        noise_file = Path(__file__).resolve().parent / "wordcloud_noise_v1.txt"
        noise = {
            line.strip().lower()
            for line in noise_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    with psycopg.connect(DB_DSN) as conn:
        old_df, old_n = collect_df(
            conn,
            args.source_group,
            args.old_start,
            args.old_end,
            args.ngram,
            stopwords,
            rules,
        )

        new_df, new_n = collect_df(
            conn,
            args.source_group,
            args.new_start,
            args.new_end,
            args.ngram,
            stopwords,
            rules,
        )

    rows = []

    for term in set(old_df) | set(new_df):
        old = old_df[term]
        new = new_df[term]

        if old + new < args.min_docs:
            continue

        if args.filter_noise:
            term_tokens = term.lower().split()

            if any(token.isdigit() for token in term_tokens):
                continue

            if any(token in noise for token in term_tokens):
                continue

        old_pct = 100.0 * old / old_n if old_n else 0.0
        new_pct = 100.0 * new / new_n if new_n else 0.0

        score = log_odds(
            new,
            new_n,
            old,
            old_n,
        )

        rows.append(
            (
                term,
                score,
                old,
                new,
                old_pct,
                new_pct,
                new_pct - old_pct,
            )
        )

    print(
        f"source_group={args.source_group} "
        f"old_docs={old_n} "
        f"new_docs={new_n} "
        f"ngram={args.ngram}"
    )

    print("\n===== EMERGING =====")
    print(
        f"{'term':35} {'score':>8} "
        f"{'old%':>8} {'new%':>8} {'delta':>8} "
        f"{'old_n':>7} {'new_n':>7}"
    )

    emerging = sorted(
        rows,
        key=lambda x: (-x[1], -x[6], -x[3], x[0]),
    )

    for term, score, old, new, old_pct, new_pct, delta in emerging[: args.top]:
        print(
            f"{term[:35]:35} {score:8.3f} "
            f"{old_pct:7.2f}% {new_pct:7.2f}% {delta:+7.2f} "
            f"{old:7d} {new:7d}"
        )

    print("\n===== DECLINING =====")
    print(
        f"{'term':35} {'score':>8} "
        f"{'old%':>8} {'new%':>8} {'delta':>8} "
        f"{'old_n':>7} {'new_n':>7}"
    )

    declining = sorted(
        rows,
        key=lambda x: (x[1], x[6], -x[2], x[0]),
    )

    for term, score, old, new, old_pct, new_pct, delta in declining[: args.top]:
        print(
            f"{term[:35]:35} {-score:8.3f} "
            f"{old_pct:7.2f}% {new_pct:7.2f}% {delta:+7.2f} "
            f"{old:7d} {new:7d}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
